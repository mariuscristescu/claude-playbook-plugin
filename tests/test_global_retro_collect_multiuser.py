#!/usr/bin/env python3
"""global-retro-collect must see the multi-user `.agent/<user>/` layout (bug #3).

Before task 018 the collector looked only at root `.agent/tasks/` and root
`.agent/chat_log.md`, so a per-user repo (`.agent/current_user` +
`.agent/<user>/tasks/`) was discovered but collected nothing. These tests cover
discovery + collection for multi-user, single-user (byte-layout back-compat),
mixed, and duplicate-task-number-across-lanes repos.

Pure stdlib unittest. Run: python3 tests/test_global_retro_collect_multiuser.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PLUGIN = _HERE.parent / "plugins/playbook"
sys.path.insert(0, str(_PLUGIN))

from tasks.global_retro_collect import (  # noqa: E402
    archive_member_names,
    collect_global_retro,
)


def _task(project: Path, agent_rel: str, dirname: str, body="## Status\ndone\n"):
    td = project / ".agent" / agent_rel / "tasks" / dirname if agent_rel else \
        project / ".agent" / "tasks" / dirname
    td.mkdir(parents=True)
    (td / "task.md").write_text(f"# {dirname}\n{body}", encoding="utf-8")
    return td


class MultiUserCollectTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())

    def _collect(self, project):
        arc, man = collect_global_retro([project], "2020-01-01", self.out,
                                        machine="vm")
        return arc, man, archive_member_names(arc)

    def _rel_members(self, names):
        """Archive members with the machine/path_slug prefix stripped."""
        out = []
        for n in names:
            if n in ("manifest.json", "manifest.tsv"):
                continue
            parts = n.split("/", 2)  # machine / slug / relpath
            out.append(parts[2] if len(parts) == 3 else n)
        return sorted(out)

    def test_multiuser_collects_all_lanes(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        (p / ".agent" / "current_user").write_text("marius\n")
        _task(p, "marius", "001-frontend")
        _task(p, "cristi", "002-backend")
        (p / ".agent" / "marius" / "chat_log.md").write_text("marius chat\n")
        (p / ".agent" / "cristi" / "chat_log.md").write_text("cristi chat\n")
        arc, man, names = self._collect(p)
        rel = self._rel_members(names)
        self.assertIn(".agent/marius/tasks/001-frontend/task.md", rel)
        self.assertIn(".agent/cristi/tasks/002-backend/task.md", rel)
        self.assertIn(".agent/marius/chat_log.md", rel)
        self.assertIn(".agent/cristi/chat_log.md", rel)
        self.assertTrue(man["projects"][0]["kept"])
        self.assertEqual(man["projects"][0]["task_count"], 2)

    def test_single_user_root_layout_unchanged(self):
        # Back-compat: a classic root-only repo collects exactly what it always
        # did — root tasks + root chat_log, no <user> segment anywhere.
        p = Path(tempfile.mkdtemp())
        _task(p, "", "001-thing")
        (p / ".agent" / "chat_log.md").write_text("root chat\n")
        arc, man, names = self._collect(p)
        rel = self._rel_members(names)
        self.assertIn(".agent/tasks/001-thing/task.md", rel)
        self.assertIn(".agent/chat_log.md", rel)
        # No per-user lane dirs leaked into the member set.
        self.assertFalse([m for m in rel if m.startswith(".agent/") and
                          m.split("/")[1] not in ("tasks", "chat_log.md",
                                                   "bash_history", "bash_log.md",
                                                   "playbooks", "monitor")])

    def test_mixed_root_and_user_lanes(self):
        p = Path(tempfile.mkdtemp())
        _task(p, "", "001-legacy-root")
        _task(p, "marius", "002-frontend")
        (p / ".agent" / "chat_log.md").write_text("root chat\n")
        (p / ".agent" / "marius" / "chat_log.md").write_text("marius chat\n")
        arc, man, names = self._collect(p)
        rel = self._rel_members(names)
        self.assertIn(".agent/tasks/001-legacy-root/task.md", rel)
        self.assertIn(".agent/marius/tasks/002-frontend/task.md", rel)
        self.assertIn(".agent/chat_log.md", rel)
        self.assertIn(".agent/marius/chat_log.md", rel)

    def test_duplicate_task_number_across_lanes_distinguishable(self):
        p = Path(tempfile.mkdtemp())
        (p / ".agent").mkdir()
        _task(p, "marius", "001-thing")
        _task(p, "cristi", "001-thing")   # SAME number + slug, different lane
        arc, man, names = self._collect(p)
        tasks = man["projects"][0]["included_tasks"]
        self.assertEqual(len(tasks), 2)
        lanes = {t["lane"] for t in tasks}
        paths = {t["path"] for t in tasks}
        self.assertEqual(lanes, {"marius", "cristi"})
        self.assertEqual(paths, {".agent/marius/tasks/001-thing",
                                 ".agent/cristi/tasks/001-thing"})

    def test_sessions_dir_is_not_a_user_lane(self):
        # `.agent/sessions` must never be mistaken for a user lane even if it
        # somehow contains a tasks dir.
        p = Path(tempfile.mkdtemp())
        _task(p, "marius", "001-x")
        (p / ".agent" / "sessions" / "tasks").mkdir(parents=True)
        arc, man, names = self._collect(p)
        rel = self._rel_members(names)
        self.assertFalse([m for m in rel if m.startswith(".agent/sessions/")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
