"""Model-availability discovery + selection for judge pins (task 012).

Backs `tasks models check` and `tasks models select`. Model ids pinned in
`.agent/models.json` rot as providers ship/retire models; this module answers
"which judges CAN run on this machine right now" and guides the user through
refreshing the pins. `check_pins` is also reused by the panel/single-judge
hard-stop path (probe-confirming failed specs) and by doctor (probe=False).

Per-provider discovery surfaces (probed live, 2026-07-13):
- codex: `~/.codex/models_cache.json` lists slugs + per-model
  supported_reasoning_levels + the writing CLI's client_version. The cache is
  a catalog, NOT this account's entitlements — a listed model can still 400
  ("not supported when using Codex with a ChatGPT account") and an installed
  CLI older than the cache writer can 400 with "requires a newer version of
  Codex". So pins are live-probed by default; cache-only evidence gets the
  weaker LISTED verdict.
- claude: no list command exists; availability is probe-only (`claude
  --model X -p` → exit 0, or exit 1 + "There's an issue with the selected
  model"). Probes MUST scrub the Claude-session env vars and run from a cwd
  outside any playbook project — a nested claude session inside the project
  clobbers the active task's session state (live incident). Probe timeouts
  are UNKNOWN, never GONE. New Claude models can't be discovered, only
  candidate ids supplied via pins/aliases/--claude-candidates.
- agy: `agy models` lists display names, but `--model` is inert in --print
  mode (silently runs the UI-selected model), so pins are unverifiable and
  agy can never raise a model-unavailable error.
- pi: no discovery surface known; adapter availability check only.

Verdicts:
  OK                verified available (live probe, or provider-default pin)
  LISTED            in codex cache but not live-verified (--no-probe)
  GONE              verified NOT available (probe/cache says so)
  BAD_EFFORT        codex model exists but the :effort suffix isn't supported
  NEEDS_CLI_UPGRADE model needs a newer provider CLI (codex 400 signature)
  UNVERIFIABLE      provider offers no way to check (agy, pi)
  UNPROBED          claude pin under --no-probe
  UNKNOWN           provider CLI missing, or probe indeterminate (timeout)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CODEX_CACHE_PATH = Path.home() / ".codex" / "models_cache.json"
CACHE_STALE_DAYS = 7
PROBE_TIMEOUT_SECS = 120
CLAUDE_PROBE_BUDGET_USD = "0.5"

OK = "OK"
LISTED = "LISTED"
GONE = "GONE"
BAD_EFFORT = "BAD_EFFORT"
NEEDS_CLI_UPGRADE = "NEEDS_CLI_UPGRADE"
UNVERIFIABLE = "UNVERIFIABLE"
UNPROBED = "UNPROBED"
UNKNOWN = "UNKNOWN"

# Live-captured codex 400 signatures (see task 012 References corpus).
_CODEX_MODEL_GONE = "model is not supported"
_CODEX_CLI_TOO_OLD = "requires a newer version of Codex"

# Failure classification of a judge's post-format_judge_output string.
MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
CLI_UPGRADE_REQUIRED = "CLI_UPGRADE_REQUIRED"
OTHER = "OTHER"

_CLAUDE_MODEL_GONE = "There's an issue with the selected model"
_BUDGET_EXCEEDED = "Error: Exceeded USD budget"


def judge_failed(text: str) -> bool:
    """True when a judge's output string is a failure, not a review.

    Failure markers come from format_judge_output (`(FAILED — exit N)`,
    `(no output)`), the panel's run_judge guards (`(timed out…)`,
    `(error:…)`), and claude's budget-exhaustion message — which claude
    prints to stdout with exit 0, so it can't be caught by returncode and
    is anchored to block START (a review merely QUOTING it must not flag).
    """
    t = (text or "").lstrip()
    return (t.startswith("(FAILED") or t.startswith("(timed out")
            or t.startswith("(error") or t == "(no output)"
            or t.startswith(_BUDGET_EXCEEDED))


def budget_exceeded(text: str) -> bool:
    """True when a judge's output is claude's budget-exhaustion message."""
    return (text or "").lstrip().startswith(_BUDGET_EXCEEDED)


def classify_failure(output_text: str) -> str:
    """Classify a FAILED judge string → MODEL_UNAVAILABLE | CLI_UPGRADE_REQUIRED | OTHER.

    Only failure-marked strings are classified — a successful (rc==0) review
    that quotes these patterns never reaches the pattern checks, which kills
    the self-referential false positive (this repo's task.md contains every
    pattern verbatim and rides in judge context). Branches are mutually
    exclusive, most-specific first. Callers must still probe-confirm a
    MODEL_UNAVAILABLE/CLI_UPGRADE_REQUIRED verdict (probe_claude_model /
    probe_codex_model) before hard-stopping.
    """
    t = output_text or ""
    if not judge_failed(t):
        return OTHER
    if "invalid_request_error" in t and _CODEX_CLI_TOO_OLD in t:
        return CLI_UPGRADE_REQUIRED
    if "invalid_request_error" in t and _CODEX_MODEL_GONE in t:
        return MODEL_UNAVAILABLE
    if _CLAUDE_MODEL_GONE in t:
        return MODEL_UNAVAILABLE
    return OTHER


def _adapter_classes() -> dict:
    from provider.adapters.antigravity import AntigravityAdapter
    from provider.adapters.claude import ClaudeAdapter
    from provider.adapters.codex import CodexAdapter
    from provider.adapters.pi import PiAdapter
    return {"claude": ClaudeAdapter, "codex": CodexAdapter,
            "agy": AntigravityAdapter, "pi": PiAdapter}


# ── codex ────────────────────────────────────────────────────────────────────

def parse_codex_cache(text: str) -> dict:
    """`models_cache.json` content → {fetched_at, client_version, models}.

    `models` maps slug → list of supported effort levels. Hidden entries
    (visibility != "list") are kept — a pin to one still runs.
    """
    raw = json.loads(text)
    models: dict[str, list[str]] = {}
    for m in raw.get("models", []):
        slug = m.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        efforts = [
            lvl.get("effort")
            for lvl in m.get("supported_reasoning_levels", [])
            if isinstance(lvl.get("effort"), str)
        ]
        models[slug] = efforts
    return {
        "fetched_at": raw.get("fetched_at"),
        "client_version": raw.get("client_version"),
        "models": models,
    }


def load_codex_cache(path: Path = CODEX_CACHE_PATH) -> Optional[dict]:
    """Parse the codex models cache; None when absent/unreadable."""
    try:
        return parse_codex_cache(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cache_age_days(fetched_at: Optional[str]) -> Optional[float]:
    """Age of the cache's ISO-8601 fetched_at stamp, in days; None if unparsable."""
    if not fetched_at:
        return None
    try:
        stamp = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - stamp).total_seconds() / 86400


def installed_cli_version(binary: str = "codex") -> Optional[str]:
    """`<binary> --version` → "X.Y.Z", or None when missing/unparsable."""
    if not shutil.which(binary):
        return None
    try:
        result = subprocess.run(
            [binary, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", result.stdout or "")
    return match.group(1) if match else None


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def probe_codex_model(model: str, timeout: int = PROBE_TIMEOUT_SECS) -> tuple[str, str]:
    """Live-probe one codex model id → (verdict, detail).

    The cache is a catalog, not an entitlement list (a listed model can 400
    per-account), so GONE/NEEDS_CLI_UPGRADE come only from the live 400
    signatures; timeouts and unrecognized failures are UNKNOWN.
    """
    with tempfile.TemporaryDirectory(prefix="playbook-models-probe-") as td:
        try:
            result = subprocess.run(
                ["codex", "exec", "-m", model, "--skip-git-repo-check",
                 "reply with exactly: ok"],
                cwd=td, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return UNKNOWN, f"probe timed out after {timeout}s"
        except OSError as e:
            return UNKNOWN, f"probe failed to launch: {e}"
    if result.returncode == 0:
        return OK, "responds"
    combined = (result.stdout or "") + (result.stderr or "")
    if _CODEX_CLI_TOO_OLD in combined:
        return NEEDS_CLI_UPGRADE, "model requires a newer codex CLI — run `codex update`"
    if _CODEX_MODEL_GONE in combined:
        return GONE, "codex rejects this model for this account"
    first = combined.strip().splitlines()[0][:160] if combined.strip() else f"exit {result.returncode}"
    return UNKNOWN, f"probe failed for another reason: {first}"


# ── agy ──────────────────────────────────────────────────────────────────────

def parse_agy_models(text: str) -> list[str]:
    """`agy models` stdout → display-name list (one per non-empty line)."""
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def list_agy_models() -> Optional[list[str]]:
    """Run `agy models`; None when the CLI is missing or errors."""
    if not shutil.which("agy"):
        return None
    try:
        result = subprocess.run(
            ["agy", "models"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=60, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_agy_models(result.stdout or "")


# ── claude ───────────────────────────────────────────────────────────────────

def probe_claude_model(model: str, timeout: int = PROBE_TIMEOUT_SECS) -> tuple[str, str]:
    """Tiny live probe of one claude model id → (verdict, detail).

    Scrubs the same session env vars as ClaudeAdapter.run_headless_judge
    (claude.py:111-116) and runs from a throwaway temp cwd: the env vars —
    not the cwd — are the vector by which a nested claude session attaches
    to (and clobbers) the calling playbook session. Budget-capped so a probe
    can never spend more than pennies; timeouts are UNKNOWN, never GONE.
    """
    env = os.environ.copy()
    env["CLAUDECODE"] = ""
    env.pop("CLAUDE_CODE_SSE_PORT", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["PLAYBOOK_SESSION_ID"] = "models-check"
    with tempfile.TemporaryDirectory(prefix="playbook-models-probe-") as td:
        try:
            result = subprocess.run(
                ["claude", "--model", model, "-p", "reply with exactly: ok",
                 "--max-budget-usd", CLAUDE_PROBE_BUDGET_USD],
                cwd=td, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired:
            return UNKNOWN, f"probe timed out after {timeout}s"
        except OSError as e:
            return UNKNOWN, f"probe failed to launch: {e}"
    if result.returncode == 0:
        return OK, "responds"
    combined = ((result.stdout or "") + (result.stderr or "")).strip()
    if "There's an issue with the selected model" in combined:
        return GONE, "claude rejects this model id"
    first = combined.splitlines()[0][:160] if combined else f"exit {result.returncode}"
    return UNKNOWN, f"probe failed for another reason: {first}"


def claude_candidate_models(panel_specs: list[str], extra: Optional[list[str]] = None) -> list[str]:
    """Claude ids worth probing: pins ∪ shipped alias targets ∪ user-supplied.

    Claude has no list API, so genuinely NEW models can only enter via
    `--claude-candidates` (or a pin) — the report labels this section
    "known candidates only".
    """
    from provider.sandbox import MODEL_ALIASES, resolve_judge_spec
    candidates: list[str] = []
    for nm in panel_specs:
        try:
            provider, variant = resolve_judge_spec(nm)
        except ValueError:
            continue
        if provider == "claude" and variant:
            candidates.append(variant)
    for agent, model, _extras in MODEL_ALIASES.values():
        if agent == "claude" and model:
            candidates.append(model)
    candidates.extend(extra or [])
    seen: set[str] = set()
    return [m for m in candidates if not (m in seen or seen.add(m))]


# ── check ────────────────────────────────────────────────────────────────────

def check_pins(project_root: Path, probe: bool = True,
               extra_specs: Optional[list[str]] = None,
               claude_candidates: Optional[list[str]] = None) -> dict:
    """Verdict for every models.json pin (+ extra_specs) + provider inventories.

    extra_specs lets the hard-stop path include the ACTUAL failed runtime
    specs (`--models`/`--model` overrides), not just configured pins.
    Returns {"entries": [{spec, provider, variant, verdict, detail}],
             "codex": cache|None, "codex_cli_version": str|None,
             "agy_models": [names]|None, "claude_candidates": [ids],
             "warnings": [str]}.
    """
    from provider.adapters.codex import _split_reasoning_effort
    from provider.sandbox import load_judge_config, resolve_judge_spec

    cfg = load_judge_config()
    panel = list(cfg.get("panel") or [])
    default_judge = cfg.get("default_judge")
    specs = list(panel)
    for s in ([default_judge] if default_judge else []) + list(extra_specs or []):
        if s and s not in specs:
            specs.append(s)

    adapters = _adapter_classes()
    codex_cache = load_codex_cache()
    codex_version = installed_cli_version("codex")
    agy_models = list_agy_models() if adapters["agy"].is_available() else None
    warnings: list[str] = []

    if codex_cache:
        age = cache_age_days(codex_cache.get("fetched_at"))
        if age is not None and age > CACHE_STALE_DAYS:
            warnings.append(
                f"codex models cache is {age:.0f} days old — run any codex "
                f"command to refresh it before trusting these verdicts"
            )
        writer = codex_cache.get("client_version")
        if writer and codex_version:
            try:
                if _version_tuple(codex_version) < _version_tuple(writer):
                    warnings.append(
                        f"installed codex CLI {codex_version} is older than the "
                        f"cache writer {writer} — newer models may fail with "
                        f"'requires a newer version of Codex'; run `codex update`"
                    )
            except ValueError:
                pass

    probed: dict[tuple[str, str], tuple[str, str]] = {}

    def _probe(provider: str, model: str) -> tuple[str, str]:
        key = (provider, model)
        if key not in probed:
            fn = probe_claude_model if provider == "claude" else probe_codex_model
            probed[key] = fn(model)
        return probed[key]

    entries = []
    for spec in specs:
        if spec.endswith(":"):
            # resolve_judge_spec accepts "codex:" as variant=None, silently
            # running the provider default — surface it instead (R13).
            warnings.append(f"pin '{spec}' has an empty variant — it would "
                            f"silently run the provider's default model")
        try:
            provider, variant = resolve_judge_spec(spec)
        except ValueError as e:
            entries.append({"spec": spec, "provider": "?", "variant": None,
                            "verdict": GONE, "detail": str(e)})
            continue
        adapter = adapters[provider]
        if not adapter.is_available():
            entries.append({"spec": spec, "provider": provider, "variant": variant,
                            "verdict": UNKNOWN,
                            "detail": f"provider '{provider}' not available on this machine"})
            continue

        if provider == "codex":
            if not variant:
                verdict, detail = OK, "uses the codex default model"
            else:
                try:
                    model_id, effort = _split_reasoning_effort(variant)
                except ValueError as e:
                    entries.append({"spec": spec, "provider": provider, "variant": variant,
                                    "verdict": BAD_EFFORT, "detail": str(e)})
                    continue
                efforts = (codex_cache or {"models": {}})["models"].get(model_id)
                if effort and efforts and effort not in efforts:
                    verdict = BAD_EFFORT
                    detail = f"'{model_id}' supports efforts {', '.join(efforts)} — not '{effort}'"
                elif probe:
                    verdict, detail = _probe("codex", model_id)
                elif codex_cache is None:
                    verdict, detail = UNVERIFIABLE, "no ~/.codex/models_cache.json to check against"
                elif efforts is None:
                    verdict = GONE
                    detail = f"'{model_id}' not in models cache (have: {', '.join(sorted(codex_cache['models']))})"
                else:
                    verdict, detail = LISTED, "in models cache (not live-verified — cache is a catalog, not entitlements)"
        elif provider == "claude":
            if not variant:
                verdict, detail = OK, "uses the claude default model"
            elif not probe:
                verdict, detail = UNPROBED, "claude has no list command; re-run without --no-probe"
            else:
                verdict, detail = _probe("claude", variant)
        elif provider == "agy":
            verdict = UNVERIFIABLE
            detail = "agy always runs the UI-selected model (--model is inert in --print mode)"
        else:  # pi
            verdict, detail = UNVERIFIABLE, "pi has no model-discovery surface"
        entries.append({"spec": spec, "provider": provider, "variant": variant,
                        "verdict": verdict, "detail": detail})

    return {"entries": entries, "codex": codex_cache, "codex_cli_version": codex_version,
            "agy_models": agy_models,
            "claude_candidates": claude_candidate_models(specs, claude_candidates),
            "warnings": warnings}


def render_report(report: dict) -> str:
    """Human-readable availability report for stdout / hard-stop output."""
    lines = ["=== Judge pin verdicts (.agent/models.json ⊕ shipped) ==="]
    width = max((len(e["spec"]) for e in report["entries"]), default=10)
    for e in report["entries"]:
        lines.append(f"  {e['spec']:<{width}}  {e['verdict']:<18} {e['detail']}")
    codex = report.get("codex")
    if codex:
        age = cache_age_days(codex.get("fetched_at"))
        age_s = f", fetched {age:.1f}d ago" if age is not None else ""
        lines.append(f"\n=== codex models (cache writer {codex.get('client_version')}, "
                     f"installed {report.get('codex_cli_version')}{age_s}) ===")
        for slug, efforts in codex["models"].items():
            lines.append(f"  {slug:<22} efforts: {', '.join(efforts) if efforts else '-'}")
    if report.get("agy_models") is not None:
        lines.append("\n=== agy models (pin NOT selectable from CLI — set in the agy UI) ===")
        for name in report["agy_models"]:
            lines.append(f"  {name}")
    if report.get("claude_candidates"):
        lines.append("\n=== claude candidates (known ids only — claude has no list "
                     "command; add new ids with --claude-candidates) ===")
        for model in report["claude_candidates"]:
            lines.append(f"  {model}")
    for w in report.get("warnings", []):
        lines.append(f"\nWARNING: {w}")
    return "\n".join(lines)


def bad_pins(report: dict) -> list[dict]:
    """Entries whose verdict means the judge cannot run as pinned."""
    return [e for e in report["entries"]
            if e["verdict"] in (GONE, BAD_EFFORT, NEEDS_CLI_UPGRADE)]


# ── select ───────────────────────────────────────────────────────────────────

def _project_models_path(project_root: Path) -> Path:
    return project_root / ".agent" / "models.json"


def run_select(project_root: Path, probe: bool = True,
               claude_candidates: Optional[list[str]] = None) -> int:
    """Interactive panel refresh: show availability, take picks, write models.json.

    Creates `.agent/models.json` when absent (the fresh-install path).
    Round-trips the RAW file json — mutating only panel/default_judge/_updated
    — so hand-authored keys (`_doc`, `aliases`, …) are preserved; going
    through load_judge_config would drop them (it extracts two keys only).
    """
    report = check_pins(project_root, probe=probe, claude_candidates=claude_candidates)
    print(render_report(report))

    path = _project_models_path(project_root)
    existing: dict = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"WARNING: existing {path} unreadable ({e}) — starting fresh", file=sys.stderr)
    # Fallback = the effective (shipped) panel, NOT report entries — the
    # report also lists default_judge and extra specs, which aren't pins.
    from provider.sandbox import load_judge_config
    current_panel = existing.get("panel") or list(load_judge_config().get("panel") or [])

    print("\nCurrent panel:")
    for i, spec in enumerate(current_panel, 1):
        print(f"  {i}. {spec}")
    print("\nEnter the new panel as comma-separated judge specs")
    print("(provider:variant[:effort] / bare provider / alias — e.g. "
          "claude:claude-fable-5, codex:gpt-5.5:xhigh, agy).")
    print("Empty input keeps the current panel unchanged.")
    try:
        raw = input("panel> ").strip()
    except EOFError:
        raw = ""
    new_panel = [s.strip() for s in raw.split(",") if s.strip()] if raw else current_panel

    from provider.sandbox import resolve_judge_spec
    for spec in new_panel:
        if spec.endswith(":"):
            print(f"Error: pin '{spec}' has an empty variant", file=sys.stderr)
            return 1
        try:
            resolve_judge_spec(spec)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1

    default_judge = existing.get("default_judge")
    try:
        dj_raw = input(f"default_judge [{default_judge or 'unset'}]> ").strip()
    except EOFError:
        dj_raw = ""
    if dj_raw:
        try:
            resolve_judge_spec(dj_raw)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        default_judge = dj_raw

    existing["panel"] = new_panel
    if default_judge:
        existing["default_judge"] = default_judge
    existing["_updated"] = datetime.now(timezone.utc).date().isoformat()
    existing.setdefault(
        "_doc",
        "Project override for playbook judge selection (shadows the plugin's "
        "provider/models.json per key). Refresh with `tasks models select`; "
        "audit with `tasks models check`.",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}")
    return 0


# ── CLI entry ────────────────────────────────────────────────────────────────

def cli_models(cmd_args: list[str], project_root: Path) -> int:
    """`tasks models check|select [--no-probe] [--claude-candidates a,b]`."""
    args = list(cmd_args)
    sub = args.pop(0) if args and not args[0].startswith("--") else "check"
    probe = True
    candidates: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "--no-probe":
            probe = False
            i += 1
        elif args[i] == "--claude-candidates" and i + 1 < len(args):
            candidates = [s.strip() for s in args[i + 1].split(",") if s.strip()]
            i += 2
        else:
            print(f"Error: unknown models flag '{args[i]}'", file=sys.stderr)
            return 2
    if sub == "check":
        report = check_pins(project_root, probe=probe, claude_candidates=candidates)
        print(render_report(report))
        dead = bad_pins(report)
        if dead:
            print(f"\n{len(dead)} pin(s) cannot run as configured — "
                  f"refresh with: tasks models select", file=sys.stderr)
            return 1
        return 0
    if sub == "select":
        return run_select(project_root, probe=probe, claude_candidates=candidates)
    print(f"Error: unknown models subcommand '{sub}' (use: check, select)", file=sys.stderr)
    return 2
