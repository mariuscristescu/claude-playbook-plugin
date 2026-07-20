#!/usr/bin/env python3
"""Retro generator quirks (task 018 / bug report #5).

5a: bare-checkmark heuristic must not false-positive on gates whose annotation
    lives on indented continuation lines (numbered sub-bullets, `→` lines).
5b: `tasks log` must parse chat-log entries that carry the `(provider/pid)`
    suffix (added by multi-provider tagging) — and legacy entries without it.

Pure stdlib unittest. Run: python3 tests/test_retro_quirks.py
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from tasks.retro import _parse_task, _gate_has_continuation  # noqa: E402
from tasks import cli as tcli  # noqa: E402


class BareCheckmarkTest(unittest.TestCase):
    def test_continuation_annotated_gates_not_bare(self):
        content = (
            "# 001\n## Design Phase\n"
            "- [x] Fix — 4 changes:\n"
            "  1. did a\n"
            "  2. did b\n"
            "- [x] Verify results\n"
            "  → ran the suite, 12 pass\n"
            "- [x] A genuinely bare gate\n"
            "- [ ] not checked\n"
        )
        r = _parse_task(1, "x", content)
        self.assertEqual(r["checked_count"], 3)
        self.assertEqual(r["bare_checkmark_count"], 1)  # only the genuinely bare one

    def test_all_bare_template_gates_still_counted(self):
        content = ("# 002\n## Design Phase\n"
                   "- [x] Understand\n- [x] Structure\n- [x] Verify\n")
        r = _parse_task(2, "x", content)
        self.assertEqual(r["bare_checkmark_count"], 3)

    def test_arrow_continuation_any_indent(self):
        lines = ["- [x] Do the thing", "→ outcome on next line", "- [ ] next"]
        self.assertTrue(_gate_has_continuation(lines, 0, 0))

    def test_deeper_indent_is_continuation(self):
        lines = ["- [x] header", "    some indented detail", "- [ ] next"]
        self.assertTrue(_gate_has_continuation(lines, 0, 0))

    def test_next_gate_same_indent_is_not_continuation(self):
        lines = ["- [x] bare gate", "- [x] another bare gate"]
        self.assertFalse(_gate_has_continuation(lines, 0, 0))

    def test_same_indent_prose_is_not_continuation(self):
        lines = ["- [x] bare gate", "prose at column zero"]
        self.assertFalse(_gate_has_continuation(lines, 0, 0))

    def test_blank_then_continuation(self):
        lines = ["- [x] header", "", "  1. sub-bullet after a blank"]
        self.assertTrue(_gate_has_continuation(lines, 0, 0))

    def test_blank_then_next_gate_not_continuation(self):
        lines = ["- [x] bare", "", "- [x] next bare"]
        self.assertFalse(_gate_has_continuation(lines, 0, 0))


class TasksLogTest(unittest.TestCase):
    """`tasks log` must parse both suffixed (`HOST` (provider/pid)) and legacy
    (bare backticked provider) chat-log entries (bug report #5b)."""

    def _run_log(self, chat_log_text, *args):
        proj = Path(tempfile.mkdtemp())
        (proj / ".agent" / "tasks").mkdir(parents=True)
        (proj / ".agent" / "chat_log.md").write_text(chat_log_text, encoding="utf-8")
        buf = io.StringIO()
        cwd = os.getcwd()
        os.chdir(proj)
        try:
            with mock.patch.object(sys, "argv", ["tasks", "log", *args]), \
                 redirect_stdout(buf):
                try:
                    tcli.main()
                except SystemExit:
                    pass
        finally:
            os.chdir(cwd)
        return buf.getvalue()

    def test_parses_suffixed_entries(self):
        log = (
            "# Chat Log\n\n---\n\n"
            "**[M001]** [2026-07-01 10:00:00 UTC] `HOST` (claude/pid-35089)\n\nhello world\n\n"
            "---\n\n"
            "**[M002]** [2026-07-01 10:05:00 UTC] `HOST` (codex/pid-win-fallback)\n\nsecond msg\n"
        )
        out = self._run_log(log)
        self.assertIn("[M001]", out)
        self.assertIn("hello world", out)
        self.assertIn("claude", out)     # provider from suffix, not "HOST"
        self.assertIn("codex", out)
        self.assertNotIn("HOST", out)    # backticked field is superseded by provider

    def test_parses_legacy_unsuffixed_entries(self):
        # Pre-suffix format: the backticked field WAS the provider, no ` (…)`.
        log = ("**[M001]** [2026-05-01 09:00:00 UTC] `claude`\n\nlegacy entry\n")
        out = self._run_log(log)
        self.assertIn("[M001]", out)
        self.assertIn("legacy entry", out)
        self.assertIn("claude", out)

    def test_mixed_log_both_parse(self):
        log = (
            "**[M001]** [2026-05-01 09:00:00 UTC] `claude`\n\nold\n\n---\n\n"
            "**[M002]** [2026-07-01 10:00:00 UTC] `HOST` (codex/pid-1)\n\nnew\n"
        )
        out = self._run_log(log)
        self.assertIn("old", out)
        self.assertIn("new", out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("[M")]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
