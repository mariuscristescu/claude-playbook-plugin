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
- grok: `grok models` lists the ACCOUNT'S entitled model ids (login-aware —
  unlike the codex cache this list IS the entitlements), so listing alone
  earns OK. A bad `-m` fails fast pre-turn: exit 1 + stderr `Couldn't set
  model '<x>': Invalid params: "unknown model id"` (verified live, 0.2.99),
  which makes grok pins probe-confirmable for the hard-stop path.
- pi: no discovery surface known; adapter availability check only.

Verdicts:
  OK                verified available (live probe, or provider-default pin)
  LISTED            in codex cache but not live-verified (--no-probe)
  GONE              verified NOT available (probe/cache says so)
  BAD_EFFORT        codex model exists but the :effort suffix isn't supported
  NEEDS_CLI_UPGRADE model needs a newer provider CLI (codex 400 signature)
  UNVERIFIABLE      provider offers no way to check (agy, pi)
  PROVIDER_MISSING  the pin's provider CLI is not available on this machine
  UNPROBED          claude pin under --no-probe
  UNKNOWN           probe indeterminate (timeout, launch failure, odd error)
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
PROVIDER_MISSING = "PROVIDER_MISSING"
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

# Live-captured grok bad-model signature (task 014): exit 1 + stderr
# `Couldn't set model 'x': Invalid params: "unknown model id". Run 'grok
# models' to see available models.` Match both fragments — the quoted
# "unknown model id" alone is a phrase a review could plausibly quote.
_GROK_MODEL_GONE = "Couldn't set model"
_GROK_MODEL_GONE_2 = 'Invalid params: "unknown model id"'


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
    if _GROK_MODEL_GONE in t and _GROK_MODEL_GONE_2 in t:
        return MODEL_UNAVAILABLE
    return OTHER


def _adapter_classes() -> dict:
    from provider.adapters.antigravity import AntigravityAdapter
    from provider.adapters.claude import ClaudeAdapter
    from provider.adapters.codex import CodexAdapter
    from provider.adapters.grok import GrokAdapter
    from provider.adapters.pi import PiAdapter
    return {"claude": ClaudeAdapter, "codex": CodexAdapter,
            "agy": AntigravityAdapter, "pi": PiAdapter,
            "grok": GrokAdapter}


# ── codex ────────────────────────────────────────────────────────────────────

def parse_codex_cache(text: str) -> dict:
    """`models_cache.json` content → {fetched_at, client_version, models}.

    `models` maps slug → list of supported effort levels. Hidden entries
    (visibility != "list") are kept — a pin to one still runs.
    """
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("models cache is not a JSON object")
    models: dict[str, list[str]] = {}
    entries = raw.get("models", [])
    for m in entries if isinstance(entries, list) else []:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        levels = m.get("supported_reasoning_levels", [])
        efforts = [
            lvl.get("effort")
            for lvl in (levels if isinstance(levels, list) else [])
            if isinstance(lvl, dict) and isinstance(lvl.get("effort"), str)
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


def probe_codex_model(model: str, effort: Optional[str] = None,
                      timeout: int = PROBE_TIMEOUT_SECS) -> tuple[str, str]:
    """Live-probe one codex model id → (verdict, detail).

    The cache is a catalog, not an entitlement list (a listed model can 400
    per-account), so GONE/NEEDS_CLI_UPGRADE come only from the live 400
    signatures; timeouts and unrecognized failures are UNKNOWN. When the pin
    carries an :effort suffix, the probe sends the same
    `-c model_reasoning_effort=` the judge path sends (codex.py), so an
    effort the model doesn't accept fails here instead of at review time.
    """
    argv = ["codex", "exec", "-m", model, "--skip-git-repo-check"]
    if effort:
        argv += ["-c", f"model_reasoning_effort={effort}"]
    argv.append("reply with exactly: ok")
    with tempfile.TemporaryDirectory(prefix="playbook-models-probe-") as td:
        try:
            result = subprocess.run(
                argv,
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


# ── grok ─────────────────────────────────────────────────────────────────────

def parse_grok_models(text: str) -> list[str]:
    """`grok models` stdout → model-id list.

    Live format (grok 0.2.99):
        You are logged in with grok.com.

        Default model: grok-composer-2.5-fast

        Available models:
          * grok-composer-2.5-fast (default)
          - grok-build

    Model ids are the first token after a `*`/`-` bullet; the `(default)`
    decoration and prose lines (login banner, headers) are dropped.
    """
    models: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith(("* ", "- ")):
            model = s[2:].split()[0].strip()
            if model:
                models.append(model)
    return models


def list_grok_models() -> Optional[list[str]]:
    """Run `grok models`; None when the CLI is missing or errors.

    Unlike the codex models cache (a catalog), this list is login-aware —
    what it lists IS what the account can run, so a listed pin earns OK
    without a live turn.
    """
    if not shutil.which("grok"):
        return None
    try:
        result = subprocess.run(
            ["grok", "models"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=60, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return parse_grok_models(result.stdout or "")


def probe_grok_model(model: str, timeout: int = PROBE_TIMEOUT_SECS) -> tuple[str, str]:
    """Live-probe one grok model id → (verdict, detail).

    A bad `-m` fails BEFORE any turn runs (exit 1 + the stderr signature —
    verified live on 0.2.99), so a GONE probe costs nothing; a good model
    answers one tiny turn. Runs from a throwaway temp cwd so the probe
    session can't attach to a playbook project.
    """
    # --disable-web-search: grok's web tools are default-ON; without this a
    # probe turn can wander into a web search, blow PROBE_TIMEOUT_SECS, and
    # misclassify a live pin as UNKNOWN (probes gate the hard-stop path).
    argv = ["grok", "-p", "reply with exactly: ok", "-m", model,
            "--max-turns", "1", "--disable-web-search"]
    with tempfile.TemporaryDirectory(prefix="playbook-models-probe-") as td:
        try:
            result = subprocess.run(
                argv,
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
    if _GROK_MODEL_GONE in combined and _GROK_MODEL_GONE_2 in combined:
        return GONE, "grok rejects this model id for this account"
    first = combined.strip().splitlines()[0][:160] if combined.strip() else f"exit {result.returncode}"
    return UNKNOWN, f"probe failed for another reason: {first}"


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


def _claude_configured_models(project_root: Optional[Path] = None) -> list[str]:
    """Claude model id(s) the user's Claude Code is actually configured to run.

    Claude has no list command (anthropics/claude-code#12612) and the Models
    API needs a key — but Claude Code records the selected model in its own
    settings, a key-free live signal we can read the way we read
    ~/.codex/models_cache.json for codex. Reads the `model` field from
    ~/.claude/settings.json (user scope) and <project_root>/.claude/settings.json
    (project scope). Returns each `claude-*` id plus its bracket-stripped bare
    form (e.g. 'claude-fable-5[1m]' -> ['claude-fable-5[1m]', 'claude-fable-5'])
    so both the exact configured id and the plain id get probed. Short alias
    words ('sonnet', 'default') are skipped — the shipped alias baseline already
    covers those. Defensive: a missing/unreadable/malformed file yields nothing.
    """
    import json
    import re
    out: list[str] = []
    paths = [Path.home() / ".claude" / "settings.json"]
    if project_root is not None:
        paths.append(Path(project_root) / ".claude" / "settings.json")
    for p in paths:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        m = raw.get("model") if isinstance(raw, dict) else None
        if not isinstance(m, str) or not m.strip().startswith("claude-"):
            continue
        m = m.strip()
        out.append(m)
        bare = re.sub(r"\[.*?\]\s*$", "", m).strip()
        if bare and bare != m:
            out.append(bare)
    return out


def claude_candidate_models(panel_specs: list[str], extra: Optional[list[str]] = None,
                            project_root: Optional[Path] = None) -> list[str]:
    """Claude ids worth probing, most-live first:
    Claude Code's configured model ∪ pins ∪ shipped alias targets ∪ user-supplied.

    Claude has no list API/CLI command, so the set is assembled from live
    signals rather than one hardcoded table: `_claude_configured_models` reads
    the model the user's Claude Code is actually set to (key-free), pins and
    shipped aliases seed the current family, and `--claude-candidates` covers
    anything newer. Each is probed to confirm availability.
    """
    from provider.sandbox import MODEL_ALIASES, resolve_judge_spec
    candidates: list[str] = []
    # Live signal first: what Claude Code is actually configured to run.
    candidates.extend(_claude_configured_models(project_root))
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

    cfg = load_judge_config(project_root)
    panel = list(cfg.get("panel") or [])
    default_judge = cfg.get("default_judge")
    specs = list(panel)
    # User-supplied claude candidates get probed verdict rows like any pin —
    # this is the only way a NEW claude model id enters the report (I7).
    candidate_specs = [f"claude:{cid}" for cid in (claude_candidates or [])]
    for s in ([default_judge] if default_judge else []) + list(extra_specs or []) + candidate_specs:
        if s and s not in specs:
            specs.append(s)

    adapters = _adapter_classes()
    codex_cache = load_codex_cache()
    codex_version = installed_cli_version("codex")
    agy_models = list_agy_models() if adapters["agy"].is_available() else None
    _grok_adapter = adapters.get("grok")
    grok_models = (list_grok_models()
                   if _grok_adapter and _grok_adapter.is_available() else None)
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

    def _probe(provider: str, model: str, effort: Optional[str] = None) -> tuple[str, str]:
        key = (provider, model, effort)
        if key not in probed:
            if provider == "claude":
                probed[key] = probe_claude_model(model)
            elif provider == "grok":
                probed[key] = probe_grok_model(model)
            else:
                probed[key] = probe_codex_model(model, effort=effort)
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
        adapter = adapters.get(provider)
        if adapter is None:
            entries.append({"spec": spec, "provider": provider, "variant": variant,
                            "verdict": GONE,
                            "detail": f"unknown provider '{provider}' (bad alias?)"})
            continue
        if not adapter.is_available():
            entries.append({"spec": spec, "provider": provider, "variant": variant,
                            "verdict": PROVIDER_MISSING,
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
                    verdict, detail = _probe("codex", model_id, effort)
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
        elif provider == "grok":
            if not variant:
                verdict, detail = OK, "uses the grok default model"
            else:
                from provider.adapters.grok import _split_reasoning_effort as _grok_split
                try:
                    model_id, effort = _grok_split(variant)
                except ValueError as e:
                    entries.append({"spec": spec, "provider": provider, "variant": variant,
                                    "verdict": BAD_EFFORT, "detail": str(e)})
                    continue
                if grok_models is None:
                    # CLI present but `grok models` failed (logged out?) —
                    # fall back to a live probe when allowed.
                    if probe:
                        verdict, detail = _probe("grok", model_id)
                    else:
                        verdict, detail = UNKNOWN, "`grok models` unavailable (logged out?); re-run without --no-probe"
                elif model_id in grok_models:
                    # The list is login-aware entitlements — no live turn needed.
                    verdict, detail = OK, "in `grok models` (account-entitled list)"
                else:
                    verdict = GONE
                    detail = f"'{model_id}' not in `grok models` (have: {', '.join(grok_models)})"
        else:  # pi
            verdict, detail = UNVERIFIABLE, "pi has no model-discovery surface"
        entries.append({"spec": spec, "provider": provider, "variant": variant,
                        "verdict": verdict, "detail": detail})

    return {"entries": entries, "codex": codex_cache, "codex_cli_version": codex_version,
            "agy_models": agy_models, "grok_models": grok_models,
            "claude_candidates": claude_candidate_models(specs, claude_candidates, project_root),
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
    if report.get("grok_models") is not None:
        lines.append("\n=== grok models (account-entitled list from `grok models`) ===")
        for name in report["grok_models"]:
            lines.append(f"  {name}")
    if report.get("claude_candidates"):
        lines.append("\n=== claude candidates (probed — claude has no list command, so this "
                     "is your Claude Code configured model ∪ pins ∪ known ids; add more "
                     "with --claude-candidates) ===")
        for model in report["claude_candidates"]:
            lines.append(f"  {model}")
    for w in report.get("warnings", []):
        lines.append(f"\nWARNING: {w}")
    return "\n".join(lines)


def bad_pins(report: dict) -> list[dict]:
    """Entries whose verdict means the judge cannot run as pinned."""
    return [e for e in report["entries"]
            if e["verdict"] in (GONE, BAD_EFFORT, NEEDS_CLI_UPGRADE, PROVIDER_MISSING)]


def confirm_dead_specs(failed_outputs: dict, spec_providers: dict, *,
                       probe_claude=None, probe_codex=None, probe_grok=None) -> dict:
    """Probe-confirm which FAILED judge specs are actually dead.

    The shared hard-stop gate for panel and single-judge reviews:
    classification of the failure string is only a hint (failure tails can
    echo prompt fragments containing the very signatures we match); a live
    probe of the exact spec is the evidence that justifies exit 1.

    failed_outputs: {spec_label: output_text} for failed judges only.
    spec_providers: {spec_label: (provider, variant_or_None)}.
    Probes are injectable for tests. Returns {spec_label: (verdict, detail)}
    holding only probe-confirmed GONE / NEEDS_CLI_UPGRADE specs — agy/pi,
    variantless pins, and local effort-spec errors are unconfirmable and
    skipped (they keep today's soft-fail). grok pins ARE confirmable (a bad
    -m fails pre-turn with a stable signature, task 014).
    """
    from provider.adapters.codex import _split_reasoning_effort
    from provider.adapters.grok import _split_reasoning_effort as _grok_split
    probe_claude = probe_claude or probe_claude_model
    probe_codex = probe_codex or probe_codex_model
    probe_grok = probe_grok or probe_grok_model
    confirmed: dict = {}
    for spec in sorted(failed_outputs):
        if classify_failure(failed_outputs[spec]) not in (
                MODEL_UNAVAILABLE, CLI_UPGRADE_REQUIRED):
            continue
        provider, variant = spec_providers.get(spec, (None, None))
        if provider == "claude" and variant:
            pv, detail = probe_claude(variant)
        elif provider == "codex" and variant:
            try:
                model_id, effort = _split_reasoning_effort(variant)
            except ValueError:
                continue  # local spec error, not availability
            pv, detail = probe_codex(model_id, effort=effort)
        elif provider == "grok" and variant:
            try:
                model_id, _effort = _grok_split(variant)
            except ValueError:
                continue  # local spec error, not availability
            pv, detail = probe_grok(model_id)
        else:
            continue
        if pv in (GONE, NEEDS_CLI_UPGRADE):
            confirmed[spec] = (pv, detail)
    return confirmed


def apply_confirmed(report: dict, confirmed: dict) -> dict:
    """Override report entries with probe-confirmed verdicts.

    The hard-stop report is built with probe=False for speed; without this,
    a pin just probe-confirmed GONE (per-account 400) could still render
    LISTED from the cache in the very same output — a self-contradiction.
    """
    for e in report["entries"]:
        if e["spec"] in confirmed:
            e["verdict"], e["detail"] = confirmed[e["spec"]]
    return report


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
    current_panel = existing.get("panel") or list(load_judge_config(project_root).get("panel") or [])

    print("\nCurrent panel:")
    for i, spec in enumerate(current_panel, 1):
        print(f"  {i}. {spec}")
    print("\nEnter the new panel as comma-separated judge specs")
    print("(provider:variant[:effort] / bare provider / alias — e.g. "
          "claude:claude-fable-5, codex:gpt-5.5:xhigh, grok:grok-build, agy).")
    print("Empty input keeps the current panel unchanged.")
    try:
        raw = input("panel> ").strip()
    except EOFError:
        raw = ""
    new_panel = [s.strip() for s in raw.split(",") if s.strip()] if raw else current_panel

    from provider.adapters.codex import _split_reasoning_effort
    from provider.sandbox import resolve_judge_spec

    def _spec_error(spec: str) -> Optional[str]:
        """Syntactic validation shared by panel entries and default_judge:
        empty variants and codex effort suffixes are checked here because
        resolve_judge_spec accepts both (`codex:` → default model,
        `codex:gpt-5.5:bogus` → effort unvalidated until review time)."""
        if spec.endswith(":"):
            return f"pin '{spec}' has an empty variant"
        try:
            provider, variant = resolve_judge_spec(spec)
        except ValueError as e:
            return str(e)
        if provider == "codex" and variant:
            try:
                _split_reasoning_effort(variant)
            except ValueError as e:
                return str(e)
        if provider == "grok" and variant:
            from provider.adapters.grok import _split_reasoning_effort as _grok_split
            try:
                _grok_split(variant)
            except ValueError as e:
                return str(e)
        return None

    for spec in new_panel:
        err = _spec_error(spec)
        if err:
            print(f"Error: {err}", file=sys.stderr)
            return 1

    default_judge = existing.get("default_judge")
    try:
        dj_raw = input(f"default_judge [{default_judge or 'unset'}]> ").strip()
    except EOFError:
        dj_raw = ""
    if dj_raw:
        err = _spec_error(dj_raw)
        if err:
            print(f"Error: {err}", file=sys.stderr)
            return 1
        default_judge = dj_raw

    # Audit the PROPOSED pins (cheap checks) before writing — otherwise select
    # can immediately re-create a rotten panel (impl-panel I8). Bad verdicts
    # need an explicit confirmation.
    proposed = list(new_panel) + ([default_judge] if default_judge else [])
    audit = check_pins(project_root, probe=False, extra_specs=proposed)
    bad = [e for e in bad_pins(audit) if e["spec"] in proposed]
    if bad:
        print("\nProposed pin(s) look unusable:", file=sys.stderr)
        for e in bad:
            print(f"  {e['spec']}: {e['verdict']} — {e['detail']}", file=sys.stderr)
        try:
            answer = input("Write anyway? [y/N]> ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print("Aborted — nothing written.", file=sys.stderr)
            return 1

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
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)  # atomic — an interrupt can't truncate models.json
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
