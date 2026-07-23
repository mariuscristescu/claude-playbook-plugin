#!/usr/bin/env python3
"""Point tests for hook-command quoting validation (task 019).

Guards the field bug (AloVet 2026-07-20): quote-wrapped hooks.json commands
fail-open on grok. Covers the shipped file (must be clean and dual-host form),
the pure validator's every branch (full-wrap flagged, bash-wrapped clean,
empty/non-string flagged, missing→[], malformed→advisory, shape checks), and
the doctor §1f wiring seam (buggy fixture → warnings, clean fixture → silent).

Pure stdlib unittest (no hypothesis — honors the stdlib-only runtime invariant).
Run: python3 tests/test_hooks_json_quoting.py   (or: python3 -m unittest ...)
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from tasks.hooks_check import (  # noqa: E402
    EXPECTED_HOOKS,
    _installed_playbook_paths,
    candidate_hooks_paths,
    hook_command_issues,
    hooks_check_report,
)

SHIPPED = _PLUGIN / "hooks" / "hooks.json"


def _write_plugin_tree(root: Path, commands: dict) -> Path:
    """Lay down a minimal plugin tree (hooks/hooks.json + scripts/) and return
    the hooks.json path. `commands` maps event name -> command string."""
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for script in EXPECTED_HOOKS.values():
        s = root / "scripts" / script
        s.write_text("#!/bin/bash\n", encoding="utf-8")
    hooks_dir = root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    obj = {"hooks": {}}
    for event, cmd in commands.items():
        obj["hooks"][event] = [{"hooks": [{"type": "command", "command": cmd}]}]
    path = hooks_dir / "hooks.json"
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return path


def _good_commands() -> dict:
    return {
        ev: f'bash "${{CLAUDE_PLUGIN_ROOT}}/scripts/{script}"'
        for ev, script in EXPECTED_HOOKS.items()
    }


class ShippedFileTests(unittest.TestCase):
    def test_shipped_hooks_json_is_clean(self):
        self.assertEqual(hook_command_issues(SHIPPED), [])

    def test_shipped_commands_are_dual_host_form(self):
        data = json.loads(SHIPPED.read_text(encoding="utf-8"))
        cmds = [
            h["command"]
            for entries in data["hooks"].values()
            for e in entries
            for h in e["hooks"]
        ]
        self.assertEqual(len(cmds), len(EXPECTED_HOOKS))
        for c in cmds:
            self.assertTrue(c.startswith('bash "'), c)
            self.assertTrue(c.endswith('"'), c)
            # not a matched full-wrap
            self.assertNotEqual(c[0], c[-1])

    def test_shipped_scripts_exist_and_executable(self):
        import os

        scripts_dir = SHIPPED.parent.parent / "scripts"
        for script in EXPECTED_HOOKS.values():
            p = scripts_dir / script
            self.assertTrue(p.exists(), f"{script} missing")
            self.assertTrue(os.access(p, os.X_OK), f"{script} not executable")


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_bash_wrapped_form_is_clean(self):
        path = _write_plugin_tree(self.root, _good_commands())
        self.assertEqual(hook_command_issues(path), [])

    def test_full_wrap_double_quote_flagged(self):
        cmds = _good_commands()
        cmds["PreToolUse"] = '"${CLAUDE_PLUGIN_ROOT}/scripts/task-gate-hook"'
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("quote-wrapped" in i and "PreToolUse" in i for i in issues), issues)

    def test_full_wrap_single_quote_flagged(self):
        cmds = _good_commands()
        cmds["Stop"] = "'${CLAUDE_PLUGIN_ROOT}/scripts/stop-hook'"
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("quote-wrapped" in i and "Stop" in i for i in issues), issues)

    def test_leading_whitespace_then_wrapped_flagged(self):
        cmds = _good_commands()
        cmds["PostToolUse"] = '  "${CLAUDE_PLUGIN_ROOT}/scripts/state-echo-hook"  '
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("quote-wrapped" in i and "PostToolUse" in i for i in issues), issues)

    def test_bare_form_is_clean_of_quote_defect(self):
        # Bare (no bash, no quotes) is the reporter's own workaround — it is
        # NOT quote-wrapped, so the quoting check must not flag it.
        cmds = {
            ev: f"${{CLAUDE_PLUGIN_ROOT}}/scripts/{script}"
            for ev, script in EXPECTED_HOOKS.items()
        }
        path = _write_plugin_tree(self.root, cmds)
        self.assertEqual(
            [i for i in hook_command_issues(path) if "quote-wrapped" in i], []
        )

    def test_empty_command_flagged(self):
        cmds = _good_commands()
        cmds["Stop"] = "   "
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("empty" in i for i in issues), issues)

    def test_non_string_command_flagged(self):
        path = _write_plugin_tree(self.root, _good_commands())
        obj = json.loads(path.read_text())
        obj["hooks"]["Stop"][0]["hooks"][0]["command"] = 123
        path.write_text(json.dumps(obj))
        issues = hook_command_issues(path)
        self.assertTrue(any("not a string" in i for i in issues), issues)

    def test_missing_file_is_silent(self):
        self.assertEqual(hook_command_issues(self.root / "nope.json"), [])

    def test_malformed_json_is_single_advisory(self):
        bad = self.root / "bad.json"
        bad.write_text("{not valid", encoding="utf-8")
        issues = hook_command_issues(bad)
        self.assertEqual(len(issues), 1)
        self.assertIn("JSON", issues[0])

    def test_missing_registration_flagged(self):
        cmds = _good_commands()
        del cmds["SessionEnd"]
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(any("SessionEnd" in i and "missing" in i for i in issues), issues)

    def test_missing_referenced_script_flagged(self):
        path = _write_plugin_tree(self.root, _good_commands())
        (self.root / "scripts" / "task-gate-hook").unlink()
        issues = hook_command_issues(path)
        self.assertTrue(any("task-gate-hook" in i and "not found" in i for i in issues), issues)

    def test_wrong_script_referenced_flagged(self):
        cmds = _good_commands()
        # PreToolUse points at the wrong script basename
        cmds["PreToolUse"] = 'bash "${CLAUDE_PLUGIN_ROOT}/scripts/session-start-hook"'
        path = _write_plugin_tree(self.root, cmds)
        issues = hook_command_issues(path)
        self.assertTrue(
            any("PreToolUse" in i and "task-gate-hook" in i for i in issues), issues
        )


class CandidatePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_plugin_root_included_when_present(self):
        path = _write_plugin_tree(self.root, _good_commands())
        env = {"CLAUDE_PLUGIN_ROOT": str(self.root)}
        paths = candidate_hooks_paths(project_path=None, env=env)
        self.assertIn(path.resolve(), [p.resolve() for p in paths])

    def test_installed_playbook_paths_reads_manifest(self):
        # A stale cache copy sits at <installPath>/hooks/hooks.json with a
        # version segment; installPath resolution (not a **/playbook/hooks glob)
        # is what reaches it. Fixture a minimal installed_plugins.json.
        home = self.root
        plugdir = home / ".claude" / "plugins"
        plugdir.mkdir(parents=True)
        install_path = plugdir / "cache" / "mp" / "playbook" / "9.9.9"
        (install_path).mkdir(parents=True)
        (plugdir / "installed_plugins.json").write_text(
            json.dumps(
                {
                    "plugins": {
                        "playbook@mp": [
                            {"scope": "user", "installPath": str(install_path), "version": "9.9.9"}
                        ],
                        "other-plugin@mp": [{"installPath": "/somewhere/else"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        paths = _installed_playbook_paths(home)
        self.assertIn(install_path, paths)
        self.assertNotIn(Path("/somewhere/else"), paths)

    def test_installed_playbook_paths_soft_on_missing_manifest(self):
        self.assertEqual(_installed_playbook_paths(self.root), [])

    def test_nonexistent_candidates_dropped_and_deduped(self):
        # No env root, no workspace copy → only real files (the module-relative
        # shipped copy) survive; and it appears at most once.
        paths = candidate_hooks_paths(project_path="/nonexistent-xyz", env={})
        resolved = [p.resolve() for p in paths]
        self.assertEqual(len(resolved), len(set(resolved)))
        for p in paths:
            self.assertTrue(p.is_file())


class DoctorWiringTests(unittest.TestCase):
    """§1f seam: hooks_check_report drives the doctor warn() loop."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_buggy_copy_produces_warnings(self):
        cmds = _good_commands()
        cmds["PreToolUse"] = '"${CLAUDE_PLUGIN_ROOT}/scripts/task-gate-hook"'
        _write_plugin_tree(self.root, cmds)
        env = {"CLAUDE_PLUGIN_ROOT": str(self.root)}
        report = hooks_check_report(project_path=None, env=env)
        self.assertTrue(report)
        label, detail = report[0]
        self.assertIn("hooks:", label)
        self.assertIn("quote-wrapped", detail)

    def test_clean_copy_is_silent(self):
        _write_plugin_tree(self.root, _good_commands())
        env = {"CLAUDE_PLUGIN_ROOT": str(self.root)}
        # Point project_path at an empty dir so only the env copy is scanned.
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        report = [
            r
            for r in hooks_check_report(project_path=str(empty), env=env)
            if str(self.root) in r[0]
        ]
        self.assertEqual(report, [])


if __name__ == "__main__":
    unittest.main()
