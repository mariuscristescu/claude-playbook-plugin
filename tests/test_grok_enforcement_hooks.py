"""Tests for Grok always-trusted global enforcement hooks (task 020)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PLUGIN = _REPO / "plugins" / "playbook"
sys.path.insert(0, str(_PLUGIN))

from provider.adapters.grok import (  # noqa: E402
    GrokAdapter,
    _GROK_PRETOOL_MATCHER,
    build_enforcement_hooks_payload,
    grok_enforcement_hooks_path,
    resolve_playbook_plugin_root,
)


class BuildPayloadTests(unittest.TestCase):
    def test_commands_are_abs_bash_paths(self):
        root = resolve_playbook_plugin_root()
        payload = build_enforcement_hooks_payload(root)
        cmds = []
        for event, entries in payload["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    cmds.append((event, hook["command"]))
                    self.assertTrue(
                        hook["command"].startswith('bash "'),
                        msg=hook["command"],
                    )
                    self.assertTrue(hook["command"].endswith('"'), msg=hook["command"])
                    # Path inside quotes must exist
                    path = hook["command"][len('bash "') : -1]
                    self.assertTrue(Path(path).is_file(), msg=path)
                    self.assertIn("CLAUDE_PLUGIN_ROOT", hook.get("env", {}))
                    self.assertEqual(
                        hook["env"]["CLAUDE_PLUGIN_ROOT"], str(root.resolve())
                    )

        events = {e for e, _ in cmds}
        self.assertEqual(
            events,
            {
                "SessionStart",
                "PreToolUse",
                "UserPromptSubmit",
                "PostToolUse",
                "Stop",
                "SessionEnd",
            },
        )

    def test_pretool_matcher_covers_grok_tools(self):
        root = resolve_playbook_plugin_root()
        payload = build_enforcement_hooks_payload(root)
        matcher = payload["hooks"]["PreToolUse"][0]["matcher"]
        self.assertEqual(matcher, _GROK_PRETOOL_MATCHER)
        for token in (
            "write",
            "search_replace",
            "run_terminal_command",
            "Edit",
            "Write",
            "Bash",
            "StrReplace",
            "Shell",
        ):
            self.assertIn(token, matcher)


class InstallHooksTests(unittest.TestCase):
    def test_install_writes_enforcement_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_dir = Path(tmp)
            old = os.environ.get("PLAYBOOK_GROK_HOOKS_DIR")
            os.environ["PLAYBOOK_GROK_HOOKS_DIR"] = str(env_dir)
            try:
                target = grok_enforcement_hooks_path()
                self.assertEqual(target.parent, env_dir)
                adapter = GrokAdapter("test-session", _REPO)
                adapter.install_hooks(_REPO)
                self.assertTrue(target.is_file())
                data = json.loads(target.read_text(encoding="utf-8"))
                self.assertIn("PreToolUse", data["hooks"])
                # Idempotent second write
                adapter.install_hooks(_REPO)
                self.assertTrue(target.is_file())
                adapter.uninstall_hooks(_REPO)
                self.assertFalse(target.exists())
            finally:
                if old is None:
                    os.environ.pop("PLAYBOOK_GROK_HOOKS_DIR", None)
                else:
                    os.environ["PLAYBOOK_GROK_HOOKS_DIR"] = old


class ShippedHooksMatcherTests(unittest.TestCase):
    def test_plugin_hooks_json_matcher_includes_grok_names(self):
        hooks = json.loads(
            (_PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8")
        )
        matcher = hooks["hooks"]["PreToolUse"][0]["matcher"]
        for token in ("write", "search_replace", "run_terminal_command"):
            self.assertIn(token, matcher)


class NormalizerGrokToolNamesTests(unittest.TestCase):
    NORM = str(_PLUGIN / "scripts" / "hook-payload-normalize.py")

    def _norm(self, payload: dict) -> dict:
        raw = json.dumps(payload)
        out = subprocess.check_output(
            [sys.executable, self.NORM], input=raw.encode(), stderr=subprocess.STDOUT
        )
        return json.loads(out.decode())

    def test_search_replace_maps_to_edit(self):
        got = self._norm(
            {
                "hookEventName": "pre_tool_use",
                "toolName": "search_replace",
                "toolInput": {"path": "/tmp/x.py", "old_string": "a", "new_string": "b"},
            }
        )
        self.assertEqual(got["tool_name"], "Edit")
        self.assertEqual(got["tool_input"]["file_path"], "/tmp/x.py")

    def test_write_maps_to_write(self):
        got = self._norm(
            {
                "hookEventName": "pre_tool_use",
                "toolName": "write",
                "toolInput": {"path": "/tmp/x.py", "contents": "hi"},
            }
        )
        self.assertEqual(got["tool_name"], "Write")
        self.assertEqual(got["tool_input"]["file_path"], "/tmp/x.py")
        self.assertEqual(got["tool_input"]["content"], "hi")

    def test_snake_case_write_maps_without_dialect_markers(self):
        """Hybrid hosts: tool_name=write with snake_case keys (panel 020)."""
        got = self._norm(
            {
                "tool_name": "write",
                "tool_input": {"path": "/tmp/x.py", "contents": "hi"},
            }
        )
        self.assertEqual(got["tool_name"], "Write")
        self.assertEqual(got["tool_input"]["file_path"], "/tmp/x.py")
        self.assertEqual(got["tool_input"]["content"], "hi")

    def test_run_terminal_command_maps_to_bash(self):
        got = self._norm(
            {
                "hookEventName": "pre_tool_use",
                "toolName": "run_terminal_command",
                "toolInput": {"command": "echo hi"},
            }
        )
        self.assertEqual(got["tool_name"], "Bash")


class GateHookGrokDialectTests(unittest.TestCase):
    """Subprocess: grok-dialect PreToolUse → task-gate-hook exit 2 (no task).

    Mirrors the live Checkpoint probe (task 020) so a normalizer/guard
    regression fails in CI rather than only in a manual Grok session.
    """

    HOOK = str(_PLUGIN / "scripts" / "task-gate-hook")
    SESSION = "test-gate-no-task"

    def _project(self, tmp: str) -> Path:
        project = Path(tmp)
        (project / ".agent" / "tasks").mkdir(parents=True)
        return project

    def _run_gate(self, payload: dict, project: Path) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PLAYBOOK_SESSION_ID"] = self.SESSION
        # Isolate session state: no active task
        sess = project / ".agent" / "sessions" / self.SESSION
        if sess.is_dir():
            for child in sess.iterdir():
                child.unlink()
        return subprocess.run(
            ["bash", self.HOOK],
            input=json.dumps(payload).encode(),
            cwd=str(project),
            env=env,
            capture_output=True,
        )

    def test_grok_write_blocked_without_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            target = str(project / "probe.py")
            proc = self._run_gate(
                {
                    "hookEventName": "pre_tool_use",
                    "toolName": "write",
                    "toolInput": {"path": target, "contents": "print(1)\n"},
                },
                project,
            )
            self.assertEqual(proc.returncode, 2, msg=proc.stderr.decode())
            self.assertIn(b"No active task", proc.stderr)

    def test_grok_search_replace_blocked_without_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            target = project / "probe.py"
            target.write_text("a=1\n", encoding="utf-8")
            proc = self._run_gate(
                {
                    "hookEventName": "pre_tool_use",
                    "toolName": "search_replace",
                    "toolInput": {
                        "path": str(target),
                        "old_string": "a=1",
                        "new_string": "a=2",
                    },
                },
                project,
            )
            self.assertEqual(proc.returncode, 2, msg=proc.stderr.decode())
            self.assertIn(b"No active task", proc.stderr)

    def test_grok_write_allowed_with_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            sess = project / ".agent" / "sessions" / self.SESSION
            sess.mkdir(parents=True)
            (sess / "current_state").write_text("001\n", encoding="utf-8")
            target = str(project / "probe.py")
            env = os.environ.copy()
            env["PLAYBOOK_SESSION_ID"] = self.SESSION
            proc = subprocess.run(
                ["bash", self.HOOK],
                input=json.dumps(
                    {
                        "hookEventName": "pre_tool_use",
                        "toolName": "write",
                        "toolInput": {"path": target, "contents": "print(1)\n"},
                    }
                ).encode(),
                cwd=str(project),
                env=env,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode())



    def test_snake_case_write_blocked_without_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = self._project(tmp)
            target = str(project / "probe.py")
            proc = self._run_gate(
                {
                    "tool_name": "write",
                    "tool_input": {"path": target, "contents": "print(1)\n"},
                },
                project,
            )
            self.assertEqual(proc.returncode, 2, msg=proc.stderr.decode())
            self.assertIn(b"No active task", proc.stderr)


class ResolvePluginRootTests(unittest.TestCase):
    def test_canonical_and_mirror_resolve_same_root(self):
        root = resolve_playbook_plugin_root()
        self.assertTrue((root / "scripts" / "task-gate-hook").is_file())
        mirror = (
            _PLUGIN
            / "scripts"
            / "lib"
            / "provider"
            / "adapters"
            / "grok.py"
        )
        self.assertTrue(mirror.is_file())
        self.assertEqual(root, _PLUGIN.resolve())

    def test_payload_sets_playbook_provider(self):
        root = resolve_playbook_plugin_root()
        payload = build_enforcement_hooks_payload(root)
        env = payload["hooks"]["PreToolUse"][0]["hooks"][0]["env"]
        self.assertEqual(env.get("PLAYBOOK_PROVIDER"), "grok")


class GrokEnforcementDoctorTests(unittest.TestCase):
    def test_missing_file_reports(self):
        from tasks.hooks_check import grok_enforcement_issues

        with tempfile.TemporaryDirectory() as tmp:
            issues = grok_enforcement_issues(
                env={"PLAYBOOK_GROK_HOOKS_DIR": tmp}
            )
            self.assertTrue(any("missing" in i for i in issues), issues)

    def test_stale_script_path_reports(self):
        from tasks.hooks_check import grok_enforcement_issues

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "playbook-enforcement.json"
            target.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": 'bash "/no/such/task-gate-hook"',
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            issues = grok_enforcement_issues(
                env={"PLAYBOOK_GROK_HOOKS_DIR": tmp}
            )
            self.assertTrue(any("script missing" in i for i in issues), issues)


if __name__ == "__main__":
    unittest.main()