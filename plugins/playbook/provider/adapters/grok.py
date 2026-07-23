"""
GrokAdapter — provider adapter for xAI's Grok Build CLI (`grok`).

grok v0.2.99 (verified live, task 014). Rust-based TUI + headless CLI with a
deliberate Claude Code compatibility layer: it auto-loads BOTH CLAUDE.md and
AGENTS.md from the project cwd, reads `.claude/settings.json` hooks, and loads
installed Claude Code plugins (the playbook plugin's hooks.json shows up in
`grok inspect` as `file  plugin: playbook`). State lives under ~/.grok/
(auth.json, sessions/, models_cache.json) — that path must be RW inside the
judge sandbox (sandbox._HOME_RW_SUBPATHS), or grok dies at startup with
FS_PERMISSION_DENIED before reading the prompt.

Headless invocation: `grok -p "<prompt>"` (aliases: --single); prompt is the
flag's argv VALUE, stdin is not a prompt channel (same contract as agy 1.1.x,
task 013). Useful flags: `-m <model>`, `--reasoning-effort <level>`,
`--disable-web-search`, `--max-turns <N>`, `--output-format plain|json`,
`--prompt-file <path>` (an argv-cap escape hatch no other provider has — not
used yet; the win32 >30K guard below mirrors agy/pi instead).

Model catalog: `grok models` lists account-entitled models live (verified:
grok-composer-2.5-fast (default), grok-build) — unlike claude (no list
command) and codex (cache ≠ entitlements), grok pins are directly probeable.
A bad `-m` fails fast: exit 1 + stderr `Couldn't set model '<x>': Invalid
params: "unknown model id"` (signature used by models_check.classify_failure).

Hook surface (payload dialect captured live from a real headless session):
top-level keys are camelCase (`hookEventName`, `sessionId`, `toolName`,
`toolInput`); tool names are partially Claude-aliased — Write/Read keep their
Claude names, but Edit arrives as `StrReplace` and Bash as `Shell`; toolInput
keys are grok-native: `path` (not file_path), `contents` (not content), while
`old_string`/`new_string`/`command` match Claude. The shared normalizer in
scripts/hook-payload-lib handles the translation for the bash hooks.
PreToolUse can block (exit 2 or {"decision":"deny"}); Stop CANNOT block
(fires, but the turn ends regardless) — anti-walk-away is soft, like pi.
Project hooks are folder-trust-gated: a human must run `/hooks-trust` once
per project (or launch with the trust flag); until then project hooks are
silently skipped. Global ~/.grok/hooks/*.json are always trusted.

Session identity: no PLAYBOOK-aware env from grok itself; hooks do receive
GROK_SESSION_ID + a camelCase sessionId payload key, but playbook resolution
follows the codex/agy pattern: $PLAYBOOK_SESSION_ID (set by the playbook-grok
wrapper) → PID-walk fallback (gate-echo-lib.sh knows the `grok` comm name).

Panel-review participation: ["grok-build"] — a deliberate pin (task 014
plan-panel F-G): bare `grok` specs resolve to (grok, None) which would run
the default grok-composer-2.5-fast; review work wants the stronger build
model. composer stays reachable via an explicit `grok:grok-composer-2.5-fast`.
"""

from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from ..adapter import ProviderAdapter, Invocation
from ..capabilities import ProviderCapabilities


# Efforts grok's --reasoning-effort accepts. Only "low" is live-verified
# (task 014); low/medium/high is the conservative documented trio. Kept
# permissive-but-loud like codex._REASONING_EFFORTS: an unknown keyword in a
# `grok:model:effort` pin raises at spec-parse time instead of reaching `-m`
# as a bogus token.
_REASONING_EFFORTS = frozenset({"low", "medium", "high"})

# Always-trusted global hooks dir (task 020). Project/plugin hooks are
# folder-trust-gated and, on Grok sessions under spaced project paths
# (e.g. iCloud "Mobile Documents"), are never scheduled — live survey
# 2026-07-23: 0 project/plugin hook runs under those paths. Global
# ~/.grok/hooks/*.json always load.
_GROK_ENFORCEMENT_HOOKS_FILENAME = "playbook-enforcement.json"
# Matcher covers Claude names + Grok aliases/native names (docs: Write/Edit
# → search_replace, Bash → run_terminal_command; Grok also has a distinct
# `write` tool for creates that does not always alias to Write).
_GROK_PRETOOL_MATCHER = (
    "Edit|Write|search_replace|write|"
    "Bash|Shell|StrReplace|run_terminal_command"
)


def resolve_playbook_plugin_root() -> Path:
    """Return the playbook plugin root that contains scripts/ and hooks/.

    Canonical layout: `<plugin_root>/provider/adapters/grok.py`
    Mirror layout:    `<plugin_root>/scripts/lib/provider/adapters/grok.py`

    The mirror is a live import path in some installed contexts (see
    codex_hooks.playbook_scripts_dir); three-parent walk alone would resolve
    the mirror to `scripts/lib` and install_hooks would no-op.
    """
    here = Path(__file__).resolve()
    # Mirror: .../scripts/lib/provider/adapters/grok.py → plugin root is parents[4]
    if (
        here.parent.name == "adapters"
        and here.parent.parent.name == "provider"
        and here.parent.parent.parent.name == "lib"
        and here.parent.parent.parent.parent.name == "scripts"
    ):
        return here.parent.parent.parent.parent.parent
    # Canonical: .../provider/adapters/grok.py → plugin root is parents[2]
    if here.parent.name == "adapters" and here.parent.parent.name == "provider":
        root = here.parent.parent.parent
        if (root / "scripts" / "task-gate-hook").exists():
            return root
    # Fallback: walk parents until scripts/task-gate-hook exists
    for parent in here.parents:
        if (parent / "scripts" / "task-gate-hook").exists():
            return parent
    raise RuntimeError(f"Cannot resolve playbook plugin root from {here}")


def build_enforcement_hooks_payload(plugin_root: Path) -> dict:
    """Build the hooks.json body written to ~/.grok/hooks/playbook-enforcement.json.

    Commands use space-safe absolute paths (`bash "/abs/path/script"`) so a
    spaced plugin root (iCloud checkout) still works as Grok inline-shell.
    CLAUDE_PLUGIN_ROOT / GROK_PLUGIN_ROOT are set in each hook's env map so
    scripts that expand those vars still resolve.
    """
    plugin_root = plugin_root.resolve()
    scripts = plugin_root / "scripts"
    env = {
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
        "GROK_PLUGIN_ROOT": str(plugin_root),
        "PLAYBOOK_PROVIDER": "grok",
    }

    def entry(script_name: str, *, timeout: int = 5000, status_message: Optional[str] = None) -> dict:
        path = (scripts / script_name).resolve()
        hook: dict = {
            "type": "command",
            "command": f'bash "{path}"',
            "timeout": timeout,  # milliseconds (same unit as hooks.json / HOOK_TIMEOUT_MS)
            "env": dict(env),
        }
        if status_message:
            hook["statusMessage"] = status_message
        return hook

    return {
        "hooks": {
            "SessionStart": [
                {"hooks": [entry("session-start-hook")]}
            ],
            "PreToolUse": [
                {
                    "matcher": _GROK_PRETOOL_MATCHER,
                    "hooks": [entry("task-gate-hook")],
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        entry(
                            "chat-log-hook",
                            status_message="Logging to chat_log.md",
                        )
                    ]
                }
            ],
            "PostToolUse": [
                {
                    "matcher": ".*",
                    "hooks": [entry("state-echo-hook")],
                }
            ],
            "Stop": [
                {"hooks": [entry("stop-hook")]}
            ],
            "SessionEnd": [
                {"hooks": [entry("session-end-hook")]}
            ],
        }
    }


def grok_enforcement_hooks_path() -> Path:
    """Path of the always-trusted global enforcement hooks file.

    Override with PLAYBOOK_GROK_HOOKS_DIR (directory) for tests.
    """
    override = os.environ.get("PLAYBOOK_GROK_HOOKS_DIR")
    base = Path(override) if override else (Path.home() / ".grok" / "hooks")
    return base / _GROK_ENFORCEMENT_HOOKS_FILENAME


def _split_reasoning_effort(model: str) -> tuple[str, Optional[str]]:
    """Split a `model` or `model:effort` spec into (model, effort_or_None).

    Mirrors codex._split_reasoning_effort but with grok's effort vocabulary.
    Judge specs like `grok:grok-build:high` arrive here with the effort
    suffix still attached (resolve_judge_spec only peels the provider).
    """
    if ":" not in model:
        return model, None
    model_id, _, effort = model.rpartition(":")
    model_id = model_id.strip()
    effort = effort.strip().lower()
    if not model_id:
        raise ValueError(f"empty model id in grok model spec {model!r}.")
    if effort not in _REASONING_EFFORTS:
        raise ValueError(
            f"unknown reasoning effort {effort!r} in grok model spec {model!r}. "
            f"Use one of: {', '.join(sorted(_REASONING_EFFORTS))}."
        )
    return model_id, effort


class GrokAdapter(ProviderAdapter):
    """Provider adapter for xAI Grok Build CLI (`grok`)."""

    _PANEL_VARIANTS = ["grok-build"]

    def __init__(self, session_id: str, project_root: Path) -> None:
        self._session_id = session_id
        self._project_root = project_root

    # ── CLI identity ─────────────────────────────────────────────────────────

    @classmethod
    def binary_name(cls) -> str:
        return "grok"

    @classmethod
    def panel_variants(cls) -> list[Optional[str]]:
        return list(cls._PANEL_VARIANTS)

    def run_headless_judge(
        self,
        prompt: str,
        model: Optional[str],
        system_context: str,
        *,
        web_search: bool,
        timeout_secs: int,
        budget_usd: str = "2",
    ) -> str:
        import shutil
        if not shutil.which(self.binary_name()):
            return f"(error: {self.binary_name()} not found on PATH)"
        inv = self.headless_argv(prompt, model, context=system_context)
        # Judge-only extra: grok's web tools are on by default — strip them
        # when the caller didn't ask for web search (codex is the inverse:
        # opt-in via --search).
        agent_args = inv.argv + ([] if web_search else ["--disable-web-search"])
        # grok reads its prompt as the `-p` flag value (stdin is not a prompt
        # channel — same contract as agy 1.1.x, task 013). Windows caps the
        # whole command line at 32,767 chars (WinError 206) — fail fast with a
        # clear error instead of a cryptic spawn failure. (grok --prompt-file
        # could lift this cap later; unverified under sandbox, so mirror the
        # agy/pi guard for now.)
        if os.name == "nt":
            payload = sum(len(a) + 1 for a in agent_args)
            if payload > 30_000:
                return (f"(error: grok judge prompt+context is ~{payload} chars on argv; "
                        "Windows caps the command line at 32,767 chars and grok reads its "
                        "prompt from argv — shrink the context or use another backend)")
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self._session_id or "judge"
        from provider import sandbox as _sandbox
        # encoding="utf-8" guards the stdout decode against the Windows cp1252
        # locale default. No stdin (prompt is on argv — see headless_argv).
        result = _sandbox.run(
            "grok", agent_args,
            project_root=self._project_root,
            project_writable=False,   # judge is read-only — cannot mutate repo/task.md
            env=env,
            input=None,
            capture_output=True, text=True, timeout=timeout_secs, encoding="utf-8",
        )
        return _sandbox.format_judge_output(result)

    def headless_argv(
        self,
        prompt: str,
        model: Optional[str],
        *,
        context: str = "",
        bare: bool = False,
        stream: bool = False,
    ) -> Invocation:
        # Context is joined into the prompt (agy/pi pattern) — grok has no
        # append-system-prompt flag, only a full --system-prompt-override,
        # which would clobber grok's own tool-use instructions.
        full_prompt = prompt if (bare or not context) else f"{context}\n\n---\n\n{prompt}"
        argv = ["-p", full_prompt]
        if model:
            model_id, effort = _split_reasoning_effort(model)
            argv += ["-m", model_id]
            if effort:
                argv += ["--reasoning-effort", effort]
        if stream:
            argv += ["--output-format", "streaming-json"]
        return Invocation(argv, stdin=None)

    # ── Identity ─────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def project_root(self) -> Path:
        return self._project_root

    # ── Bootstrap ────────────────────────────────────────────────────────────

    def bootstrap_file_name(self) -> str:
        # grok auto-loads BOTH AGENTS.md and CLAUDE.md from the project cwd
        # (plus .claude/CLAUDE.md at each level when Claude compat is on —
        # default). AGENTS.md is the provider-neutral choice, shared with
        # codex/pi.
        return "AGENTS.md"

    def install_bootstrap(self, project_root: Path) -> None:
        """Write AGENTS.md (shared with codex/pi) if not present."""
        from tasks.template import agents_md_template
        target = project_root / "AGENTS.md"
        if not target.exists():
            target.write_text(agents_md_template(), encoding="utf-8")

    # ── Hooks ─────────────────────────────────────────────────────────────────
    # Discovery channels (task 014–020):
    #   (1) project `.claude/settings.json` — folder-trust-gated
    #   (2) installed Claude plugin hooks.json — folder-trust-gated / plugin load
    #   (3) always-trusted `~/.grok/hooks/playbook-enforcement.json` (task 020)
    # On real Grok under spaced project paths (iCloud Mobile Documents), (1) and
    # (2) are never scheduled (0 hook runs in session survey). (3) is the
    # reliable enforcement channel. Plugin hooks.json still ships dual-host
    # `bash "${CLAUDE_PLUGIN_ROOT}/…"` for Claude Code + space-free Grok hosts.

    def install_hooks(self, project_root: Path) -> None:
        """Install always-trusted global Grok enforcement hooks + print status.

        Writes `~/.grok/hooks/playbook-enforcement.json` with absolute
        `bash "/path/to/script"` commands (space-safe) pointing at this
        plugin's scripts/. Idempotent. Also reminds about folder-trust for
        project-level hooks (monitor-nudge, etc.).

        Override output directory with PLAYBOOK_GROK_HOOKS_DIR (tests).
        """
        plugin_root = resolve_playbook_plugin_root()
        scripts = plugin_root / "scripts"
        if not (scripts / "task-gate-hook").exists():
            print(f"  grok hooks   FAIL: scripts missing under {plugin_root}")
            return

        target = grok_enforcement_hooks_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = build_enforcement_hooks_payload(plugin_root)
        # Atomic write: temp in same dir + os.replace (crash mid-write must not
        # leave truncated JSON that Grok would reject → silent fail-open).
        text = json.dumps(payload, indent=2) + "\n"
        tmp = target.with_name(target.name + f".tmp.{os.getpid()}")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        print(f"  grok hooks   wrote always-trusted {target}")
        print("               PreToolUse task-gate + PostToolUse state-echo + chat-log +")
        print("               session/stop hooks (absolute bash paths; spaced roots OK)")
        print("               restart the Grok session so hooks reload (snapshot at start)")
        print("               re-run after plugin upgrade if script paths go stale")

        settings = project_root / ".claude" / "settings.json"
        if not settings.exists():
            print("  grok hooks   note: .claude/settings.json missing — run `tasks init` for")
            print("               project deny-list + monitor-nudge (still needs /hooks-trust)")
        else:
            print("  grok hooks   project .claude/settings.json present — /hooks-trust once if")
            print("               project hooks (monitor-nudge) should also fire")

    def uninstall_hooks(self, project_root: Path) -> None:
        """Remove the always-trusted global enforcement hooks file if present.

        Note: the file is machine-global (not per-project). Uninstall disables
        enforcement for every Grok project on this host.
        """
        target = grok_enforcement_hooks_path()
        if target.exists():
            target.unlink()
            print(f"  grok hooks   removed {target} (global — all Grok projects)")
        else:
            print(f"  grok hooks   no global enforcement file at {target}")

    # ── Launch ───────────────────────────────────────────────────────────────

    @staticmethod
    def _wrapper_path() -> Optional[Path]:
        """Locate the shipped `playbook-grok` launcher. Installed: `scripts/`
        (next to the hooks). Dev: `bin/`. Walk up from this module looking in
        both."""
        here = Path(__file__).resolve()
        for parent in here.parents:
            for sub in ("scripts", "bin"):
                cand = parent / sub / "playbook-grok"
                if cand.exists():
                    return cand
        return None

    def launch_interactive(self, project_root: Path, **kwargs) -> int:
        """Interactive grok session via the `playbook-grok` wrapper.

        The wrapper sets PLAYBOOK_SESSION_ID + provisions the session dir so
        the gate hooks namespace state correctly. Falls back to direct `grok`
        if the wrapper can't be found.
        """
        env = os.environ.copy()
        wrapper = self._wrapper_path()
        cmd = [str(wrapper)] if wrapper else ["grok"]
        result = subprocess.run(cmd, cwd=str(project_root), env=env, **kwargs)
        return result.returncode

    def launch_headless(self, project_root: Path, prompt: str, **kwargs) -> str:
        """Headless one-shot via sandbox."""
        from provider import sandbox as _sandbox
        result = _sandbox.run(
            "grok", ["-p", prompt],
            project_root=project_root,
            capture_output=True, text=True, **kwargs,
        )
        return result.stdout or ""

    # ── Capabilities ────────────────────────────────────────────────────────

    def detect_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider="grok",
            # Verified live (task 014): UserPromptSubmit / PreToolUse /
            # PostToolUse / Stop / SessionStart all fire in headless mode.
            # PreToolUse blocks via exit 2 or {"decision":"deny"}.
            has_user_prompt_hook=True,
            has_pre_tool_hook=True,
            has_post_tool_hook=True,
            # Stop fires but CANNOT block ("Blocking? No" — 10-hooks.md);
            # anti-walk-away enforcement is soft, like pi. Keep False so
            # callers don't assume a hard Stop.
            has_stop_hook=False,
            # Payload has a camelCase sessionId (grok's own UUID), but
            # playbook session identity comes from PLAYBOOK_SESSION_ID (set
            # by the wrapper) / PID-walk — grok's UUID is not the playbook
            # session id, so don't advertise it.
            session_id_in_payload=False,
            # Sessions live under ~/.grok/sessions/ (sqlite index + per-cwd
            # percent-encoded dirs; hook payloads carry transcriptPath).
            # Parsing deferred — chat-log-hook covers attribution via
            # UserPromptSubmit. Wire read_new_messages when needed.
            session_log_format="unknown",
            session_log_base=None,
        )

    # ── Session log / chat log ──────────────────────────────────────────────
    # Deferred (see detect_capabilities): hook payloads include a
    # transcriptPath under ~/.grok/sessions/, but the format is unverified.
    # UserPromptSubmit → chat-log-hook covers message attribution.

    def session_log_path(self) -> Optional[Path]:
        return None

    def read_new_messages(self, since_offset: int) -> tuple[list[str], int]:
        return [], since_offset
