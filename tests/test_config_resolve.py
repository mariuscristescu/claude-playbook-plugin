#!/usr/bin/env python3
"""Point tests for the per-install review config (.agent/config.json).

Covers the precedence matrix (default / config-file / env) and malformed-value
fallback for resolve_judge_budget / resolve_review_timeout, plus a regression
guard that a configured budget actually reaches the claude judge argv (the panel
path that the plan-review panel flagged as initially mis-wired).

Pure stdlib unittest (no hypothesis — honors the stdlib-only runtime invariant).
Run: python3 tests/test_config_resolve.py   (or: python3 -m unittest ...)
"""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

# The runtime tree is plugins/playbook/ (dispatcher sets PYTHONPATH there).
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from tasks import core  # noqa: E402

_ENV_VARS = ("PLAYBOOK_JUDGE_BUDGET_USD", "PLAYBOOK_REVIEW_TIMEOUT_SECS")


class ConfigResolveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.project = Path(self._tmp.name)
        (self.project / ".agent").mkdir()
        self._saved_env = {k: os.environ.pop(k, None) for k in _ENV_VARS}
        # lru_cache on the bad-value warner would suppress repeat warnings across
        # tests — clear it so each malformed case is independent.
        core._warn_bad_config_value_once.cache_clear()

    def tearDown(self):
        self._tmp.cleanup()
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_config(self, obj):
        (self.project / ".agent" / "config.json").write_text(
            obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")

    # ── defaults ──────────────────────────────────────────────────────────
    def test_defaults_when_no_config(self):
        self.assertEqual(core.resolve_judge_budget(self.project), "2")
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    # ── config file ───────────────────────────────────────────────────────
    def test_config_file_values(self):
        self._write_config({"judge_budget_usd": 5, "review_timeout_secs": 120})
        self.assertEqual(core.resolve_judge_budget(self.project), "5")
        self.assertEqual(core.resolve_review_timeout(self.project), 120)

    def test_float_budget_preserved(self):
        self._write_config({"judge_budget_usd": 3.5})
        self.assertEqual(core.resolve_judge_budget(self.project), "3.5")

    # ── env overrides file ──────────────────────────────────────────────────
    def test_env_overrides_file(self):
        self._write_config({"judge_budget_usd": 5, "review_timeout_secs": 120})
        os.environ["PLAYBOOK_JUDGE_BUDGET_USD"] = "9"
        os.environ["PLAYBOOK_REVIEW_TIMEOUT_SECS"] = "10"
        self.assertEqual(core.resolve_judge_budget(self.project), "9")
        self.assertEqual(core.resolve_review_timeout(self.project), 10)

    # ── malformed fallbacks (never crash) ───────────────────────────────────
    def test_non_numeric_timeout_falls_back(self):
        self._write_config({"review_timeout_secs": "banana"})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    def test_negative_budget_falls_back(self):
        self._write_config({"judge_budget_usd": -3})
        self.assertEqual(core.resolve_judge_budget(self.project), "2")

    def test_nonpositive_timeout_falls_back(self):
        self._write_config({"review_timeout_secs": 0})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)

    def test_malformed_json_falls_back(self):
        self._write_config("{ not valid json")
        self.assertEqual(core.resolve_review_timeout(self.project), 300)
        self.assertEqual(core.resolve_judge_budget(self.project), "2")

    def test_non_object_json_ignored(self):
        self._write_config("[1, 2, 3]")
        self.assertEqual(core.load_config(self.project), {})
        self.assertEqual(core.resolve_review_timeout(self.project), 300)


class PanelBudgetThreadingTest(unittest.TestCase):
    """Regression guard for the panel budget path: run_headless_judge must put
    the resolved budget on the claude argv (not a hardcoded value)."""

    def setUp(self):
        self._saved_env = {k: os.environ.pop(k, None) for k in _ENV_VARS}

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _capture_claude_argv(self, **judge_kwargs):
        import shutil
        from provider import sandbox
        from provider.adapters.claude import ClaudeAdapter

        captured = {}
        orig_run, orig_fmt = sandbox.run, sandbox.format_judge_output
        orig_which = shutil.which

        def fake_run(agent, args, **kw):
            captured["args"] = args
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

        sandbox.run = fake_run
        sandbox.format_judge_output = lambda r: r.stdout
        shutil.which = lambda name: "/usr/bin/" + name  # pretend claude is installed
        try:
            a = ClaudeAdapter(session_id="judge", project_root=Path("/tmp"))
            a.run_headless_judge(prompt="p", model=None, system_context="c",
                                 web_search=False, timeout_secs=5, **judge_kwargs)
        finally:
            sandbox.run, sandbox.format_judge_output = orig_run, orig_fmt
            shutil.which = orig_which
        args = captured["args"]
        return args[args.index("--max-budget-usd") + 1]

    def test_default_budget_on_argv(self):
        self.assertEqual(self._capture_claude_argv(), "2")

    def test_configured_budget_reaches_argv(self):
        self.assertEqual(self._capture_claude_argv(budget_usd="7"), "7")


if __name__ == "__main__":
    unittest.main()
