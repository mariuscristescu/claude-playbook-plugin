#!/usr/bin/env python3
"""mindmap-sync --fix must never clobber a fenced keep-note (task 018 / bug #2).

The bug report: a ```text-fenced `## Legacy` archive whose old node IDs collide
with LIVE node IDs (`[1] [2] [3] [5] …`) had 8 fenced lines overwritten with
live-node prose by a fence-UNAWARE `--fix` write/locate step. Our tree reworked
that write path to be span-scoped via the fence-aware `_partition_overflow`, so
this is a verify-first regression fence, not a new fix.

Fixtures are the REAL bytes from ai-ring-vet commit fcac0d6 (the corrupted-era
file that carried the fenced archive) — a synthetic reconstruction could pass
while the real trigger still corrupts (task 018 panel T14).

Pure stdlib unittest. Run: python3 tests/test_mindmap_sync_fence.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
_FIX = _HERE / "fixtures"
sys.path.insert(0, str(_PLUGIN))

from tasks import cli as tcli  # noqa: E402

MAIN_SRC = (_FIX / "airingvet_fcac0d6_MAIN.md").read_text(encoding="utf-8")
OVERFLOW_SRC = (_FIX / "airingvet_fcac0d6_OVERFLOW.md").read_text(encoding="utf-8")


def _fenced_span(text: str) -> str:
    """Return the ```…``` fenced block (inclusive) — the legacy keep-note."""
    lines = text.splitlines(keepends=True)
    start = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("```"))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("```"))
    return "".join(lines[start:end + 1])


def _run_fix(project: Path) -> None:
    """Invoke `tasks mindmap-sync --fix` in-process, anchored at `project`."""
    cwd = os.getcwd()
    os.chdir(project)
    try:
        with mock.patch.object(sys, "argv", ["tasks", "mindmap-sync", "--fix"]):
            try:
                tcli.main()
            except SystemExit:
                pass
    finally:
        os.chdir(cwd)


class MindmapSyncFenceTest(unittest.TestCase):
    def _project(self, newline="\n"):
        """Temp project with a .agent/tasks marker (so find_project_root anchors
        here, never escaping to the real workspace) + main/overflow fixtures.
        `main` node [1] is drifted so --fix must sync live [1] main→overflow —
        the exact collision case (live [1] vs fenced [1])."""
        d = Path(tempfile.mkdtemp())
        (d / ".agent" / "tasks").mkdir(parents=True)
        main = MAIN_SRC.replace(
            "[1] **Project Overview** - AloVet",
            "[1] **Project Overview** - DRIFTED-MARKER AloVet", 1)
        overflow = OVERFLOW_SRC
        if newline == "\r\n":
            main = main.replace("\n", "\r\n")
            overflow = overflow.replace("\n", "\r\n")
        (d / "MIND_MAP.md").write_text(main, encoding="utf-8", newline="")
        (d / "MIND_MAP_OVERFLOW.md").write_text(overflow, encoding="utf-8", newline="")
        return d

    def _read(self, d):
        return (d / "MIND_MAP_OVERFLOW.md").read_text(encoding="utf-8", errors="replace")

    def test_fenced_block_survives_fix_lf(self):
        d = self._project(newline="\n")
        fence_before = _fenced_span(self._read(d))
        _run_fix(d)
        after = self._read(d)
        self.assertEqual(_fenced_span(after), fence_before,
                         "fenced legacy keep-note was mutated by --fix")

    def test_fenced_block_survives_fix_crlf(self):
        d = self._project(newline="\r\n")
        fence_before = _fenced_span(self._read(d))
        _run_fix(d)
        self.assertEqual(_fenced_span(self._read(d)), fence_before,
                         "fenced legacy keep-note was mutated by --fix (CRLF)")

    def test_live_node_actually_synced(self):
        # Guard against a false-green where --fix is a no-op: the drifted live
        # [1] MUST be copied into the overflow's live [1] mirror.
        d = self._project(newline="\n")
        _run_fix(d)
        after = self._read(d)
        # The live mirror (above the fence) now carries the drift marker...
        live_part = after.split("```")[0]
        self.assertIn("DRIFTED-MARKER", live_part,
                      "--fix did not sync the drifted live node (no-op false-green)")
        # ...and the fenced archive's [1] does NOT (it kept the old prose).
        fenced = _fenced_span(after)
        self.assertNotIn("DRIFTED-MARKER", fenced,
                         "drift leaked into the fenced archive — the exact bug")

    def test_second_fix_is_noop(self):
        d = self._project(newline="\n")
        _run_fix(d)
        once = self._read(d)
        _run_fix(d)
        twice = self._read(d)
        self.assertEqual(twice, once, "second --fix changed bytes — not idempotent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
