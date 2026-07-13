#!/usr/bin/env python3
"""Point tests for the agy (Antigravity) judge invocation (task 013).

agy 1.1.x `--print`/`--prompt` is a STRING flag: the prompt is its argv VALUE,
not stdin (bare `agy --print` errors "flag needs an argument"). The adapter was
written for agy 1.0.2 (bare --print + stdin), so `agy … --print --print-timeout
<secs>s` had `--print` swallow the token `--print-timeout` as its prompt — every
agy judge investigated the string "--print-timeout" instead of reviewing. These
guard the fixed invocation shape.

Pure stdlib unittest. Run: python3 tests/test_agy_invocation.py
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "plugins/playbook"))

from provider.adapters.antigravity import AntigravityAdapter  # noqa: E402


class AgyInvocationTest(unittest.TestCase):
    def setUp(self):
        self.a = AntigravityAdapter(session_id="judge", project_root=Path("/tmp/proj"))

    def test_prompt_is_print_value_not_stdin(self):
        inv = self.a.headless_argv("REVIEW", None, context="CTX")
        self.assertIsNone(inv.stdin)  # prompt no longer on stdin
        i = inv.argv.index("--print")
        self.assertEqual(inv.argv[i + 1], "CTX\n\n---\n\nREVIEW")

    def test_bare_no_context_prompt_only(self):
        inv = self.a.headless_argv("JUST THE PROMPT", None, context="", bare=True)
        i = inv.argv.index("--print")
        self.assertEqual(inv.argv[i + 1], "JUST THE PROMPT")

    def test_print_timeout_never_adjacent_after_print(self):
        # The exact swallow bug: in the full judge argv, the token after
        # `--print` must be the prompt, never `--print-timeout`.
        captured = {}

        def fake_run(binary, args, **kw):
            captured["args"] = args
            captured["input"] = kw.get("input")
            import subprocess
            return subprocess.CompletedProcess(args, 0, stdout="REVIEW BODY", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/agy"), \
                mock.patch("provider.sandbox.run", side_effect=fake_run), \
                mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            self.a.run_headless_judge("REVIEW", None, "CTX", web_search=False, timeout_secs=90)

        args = captured["args"]
        i = args.index("--print")
        self.assertNotEqual(args[i + 1], "--print-timeout")
        self.assertEqual(args[i + 1], "CTX\n\n---\n\nREVIEW")
        # --print-timeout is still present, just after the value
        self.assertIn("--print-timeout", args)
        self.assertEqual(args[args.index("--print-timeout") + 1], "90s")
        self.assertIsNone(captured["input"])  # no stdin

    def test_windows_argv_guard(self):
        with mock.patch("shutil.which", return_value="/usr/bin/agy"), \
                mock.patch.object(os, "name", "nt"):
            out = self.a.run_headless_judge(
                "P", None, "X" * 40000, web_search=False, timeout_secs=60)
        self.assertTrue(out.startswith("(error: agy judge prompt+context is ~"))
        self.assertIn("Windows caps the command line", out)

    def test_small_payload_not_blocked_on_windows(self):
        # A modest prompt must still run on Windows (guard only trips >30K).
        def fake_run(binary, args, **kw):
            import subprocess
            return subprocess.CompletedProcess(args, 0, stdout="OK", stderr="")

        with mock.patch("shutil.which", return_value="/usr/bin/agy"), \
                mock.patch.object(os, "name", "nt"), \
                mock.patch("provider.sandbox.run", side_effect=fake_run), \
                mock.patch("provider.sandbox.format_judge_output", side_effect=lambda r: r.stdout):
            out = self.a.run_headless_judge("P", None, "small", web_search=False, timeout_secs=60)
        self.assertEqual(out, "OK")


if __name__ == "__main__":
    unittest.main(verbosity=2)
