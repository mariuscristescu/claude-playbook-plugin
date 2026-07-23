"""Hook-command shape validation (task 019) — advisory doctor check.

Guards against the field bug (AloVet 2026-07-20): every `command` in the
plugin's hooks.json was authored quote-WRAPPED
(`"${CLAUDE_PLUGIN_ROOT}/scripts/<hook>"`). Claude Code runs hook commands
through a shell and tolerates it; grok resolves a space-free command as a
PATH relative to hooks.json, keeps the literal quotes, and fails
command-not-found in 0ms — silently fail-open for all six hooks. The fix
ships the dual-host form `bash "${CLAUDE_PLUGIN_ROOT}/scripts/<hook>"` (the
leading `bash ` forces grok's inline-shell path where quotes are honored).

This module detects a REGRESSION back to the wrapped form, plus a few cheap
structural defects, in whatever hooks.json copies the host actually loads —
the daily CLI resolves to an installed cache copy, and grok keeps its own
copies under ~/.grok, so a clean source tree is not proof the running host is
clean. All checks are soft: callers wrap in try/except per doctor's "never
crash on an advisory check" contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# The six lifecycle registrations the plugin ships. Event name -> script
# basename expected under the sibling scripts/ dir.
EXPECTED_HOOKS = {
    "SessionStart": "session-start-hook",
    "PreToolUse": "task-gate-hook",
    "UserPromptSubmit": "chat-log-hook",
    "PostToolUse": "state-echo-hook",
    "Stop": "stop-hook",
    "SessionEnd": "session-end-hook",
}

_QUOTES = ("\"", "'")
_SCRIPT_RE = re.compile(r"scripts/([A-Za-z0-9._-]+)")


def _iter_commands(hooks_obj):
    """Yield every (event, command) pair from a parsed hooks.json `hooks` dict.

    Tolerant of shape drift: skips anything that isn't the expected
    dict -> list[entry] -> entry["hooks"] -> list[hook] -> hook["command"]
    nesting rather than raising.
    """
    if not isinstance(hooks_obj, dict):
        return
    for event, entries in hooks_obj.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []) or []:
                if isinstance(hook, dict) and "command" in hook:
                    yield event, hook["command"]


def hook_command_issues(hooks_json_path) -> list[str]:
    """Return a list of human-readable issue strings for one hooks.json file.

    Empty list = clean (or file absent — a missing copy is not a defect).
    Never raises: malformed JSON becomes a single advisory string.
    """
    path = Path(hooks_json_path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError) as e:
        return [f"unreadable/invalid JSON ({e})"]

    issues: list[str] = []
    hooks_obj = data.get("hooks") if isinstance(data, dict) else None
    scripts_dir = path.parent.parent / "scripts"

    seen_events: set[str] = set()
    for event, command in _iter_commands(hooks_obj):
        seen_events.add(event)
        if not isinstance(command, str):
            issues.append(f"{event}: command is not a string ({type(command).__name__})")
            continue
        stripped = command.strip()
        if not stripped:
            issues.append(f"{event}: command is empty")
            continue
        # The defect: a fully quote-wrapped command. grok treats it as a
        # literal path and fails. `bash "..."` starts with 'b' -> not a pair.
        if len(stripped) > 1 and stripped[0] == stripped[-1] and stripped[0] in _QUOTES:
            issues.append(
                f"{event}: command is quote-wrapped ({command!r}) — grok resolves it "
                f"as a literal path and fail-opens; use bash \"${{CLAUDE_PLUGIN_ROOT}}/scripts/<hook>\""
            )
            continue
        # Structural: the referenced script should exist next to hooks.json.
        m = _SCRIPT_RE.search(command)
        if m and scripts_dir.is_dir():
            script = scripts_dir / m.group(1)
            if not script.exists():
                issues.append(f"{event}: referenced script {m.group(1)} not found in {scripts_dir}")

    # Structural: every expected lifecycle registration should be present and
    # point at its script. Only enforced when the file has a recognizable
    # hooks dict (an entirely unparseable file already reported above).
    if isinstance(hooks_obj, dict):
        for event, script in EXPECTED_HOOKS.items():
            if event not in seen_events:
                issues.append(f"{event}: expected hook registration missing")
                continue
            cmds = [c for ev, c in _iter_commands(hooks_obj) if ev == event and isinstance(c, str)]
            if not any(script in c for c in cmds):
                issues.append(f"{event}: no command references {script}")

    return issues


def _installed_playbook_paths(home: Path) -> list[Path]:
    """installPath(s) for the playbook plugin from installed_plugins.json.

    Mirrors how the `tasks` wrapper resolves the copy Claude Code loads. Soft:
    returns [] if the manifest is absent or unparseable.
    """
    manifest = home / ".claude" / "plugins" / "installed_plugins.json"
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError):
        return []
    out: list[Path] = []
    for key, installs in (data.get("plugins") or {}).items():
        if "playbook" not in str(key).lower():
            continue
        for inst in installs or []:
            p = inst.get("installPath") if isinstance(inst, dict) else None
            if p:
                out.append(Path(p))
    return out


def candidate_hooks_paths(project_path, env=None) -> list[Path]:
    """Resolve the hooks.json copies a running host might actually load.

    Order (most authoritative first): the CLAUDE_PLUGIN_ROOT the host set, the
    copy shipped alongside this module, the workspace source checkout, then
    grok's own installed/marketplace copies. Deduped by resolved path;
    nonexistent candidates are dropped so callers only see real files.
    """
    import os

    env = os.environ if env is None else env
    candidates: list[Path] = []

    home = Path.home()

    plugin_root = env.get("CLAUDE_PLUGIN_ROOT")
    if plugin_root:
        # The copy Claude Code actually loads (env set inside a hook context).
        candidates.append(Path(plugin_root) / "hooks" / "hooks.json")
    else:
        # Manual `tasks doctor` has no CLAUDE_PLUGIN_ROOT. The authoritative
        # "what Claude Code loads" is the installPath recorded in
        # installed_plugins.json (the daily `tasks` wrapper resolves the same
        # way); the cache copy sits at <installPath>/hooks/hooks.json with a
        # version segment a plain **/playbook/hooks glob would miss. Also add
        # the marketplaces clone, which some trust-loaded setups read from.
        for install_path in _installed_playbook_paths(home):
            candidates.append(install_path / "hooks" / "hooks.json")
        candidates.extend(
            sorted(home.glob(".claude/plugins/marketplaces/*/plugins/playbook/hooks/hooks.json"))
        )

    # Alongside this module: tasks/ package parent is plugins/playbook/.
    candidates.append(Path(__file__).resolve().parent.parent / "hooks" / "hooks.json")

    if project_path is not None:
        candidates.append(Path(project_path) / "plugins" / "playbook" / "hooks" / "hooks.json")

    candidates.extend(sorted(home.glob(".grok/installed-plugins/playbook-*/hooks/hooks.json")))
    candidates.extend(sorted(home.glob(".grok/marketplace-cache/*/plugins/playbook/hooks/hooks.json")))

    seen: set[str] = set()
    resolved: list[Path] = []
    for c in candidates:
        try:
            if not c.is_file():
                continue
            key = str(c.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        resolved.append(c)
    return resolved


def hooks_check_report(project_path, env=None) -> list[tuple[str, str]]:
    """Doctor §1f payload: (label, detail) warnings across every real copy.

    Thin, side-effect-free glue over candidate_hooks_paths + hook_command_issues
    so the doctor wiring is a plain for-loop and this function is unit-testable
    with fixture paths. Each warning is labelled with which copy is dirty so a
    clean CLI tree but stale grok copy is distinguishable.
    """
    report: list[tuple[str, str]] = []
    for path in candidate_hooks_paths(project_path, env=env):
        for issue in hook_command_issues(path):
            report.append((f"hooks: {path}", issue))
    return report


def grok_enforcement_issues(env=None) -> list[str]:
    """Doctor advisory for ~/.grok/hooks/playbook-enforcement.json (task 020).

    Returns human-readable issues: missing file (when we can detect Grok use is
    in play is left to the caller), invalid JSON, or command paths that no
    longer exist (stale after upgrade/move → silent fail-open).
    Empty list = clean or not applicable. Never raises.
    """
    import os
    import re

    env = os.environ if env is None else env
    override = env.get("PLAYBOOK_GROK_HOOKS_DIR")
    if override:
        path = Path(override) / "playbook-enforcement.json"
    else:
        path = Path.home() / ".grok" / "hooks" / "playbook-enforcement.json"

    if not path.is_file():
        return [
            f"missing {path} — run `tasks init --provider grok` "
            "(auto-installs always-trusted global enforcement; restart Grok after)"
        ]

    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (ValueError, OSError) as e:
        return [f"unreadable/invalid JSON at {path} ({e})"]

    issues: list[str] = []
    hooks_obj = data.get("hooks") if isinstance(data, dict) else None
    # Extract absolute paths inside bash "..." commands
    path_re = re.compile(r'bash\s+"([^"]+)"')
    for event, command in _iter_commands(hooks_obj):
        if not isinstance(command, str):
            issues.append(f"{event}: non-string command")
            continue
        m = path_re.search(command)
        if not m:
            continue
        script = Path(m.group(1))
        if not script.is_file():
            issues.append(
                f"{event}: script missing ({script}) — re-run "
                "`tasks init --provider grok` after upgrade/move"
            )
    return issues


def grok_enforcement_report(env=None) -> list[tuple[str, str]]:
    """Doctor payload: (label, detail) for the Grok global enforcement file."""
    return [("hooks: grok enforcement", issue) for issue in grok_enforcement_issues(env=env)]
