"""Handoff store contracts: naming, ranked listing and pending runner reports."""
import os
from pathlib import Path
import tempfile
import unittest

from core import handoffs


class StoreFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()
        self.store = self.home / ".context-guard" / "handoffs"
        self.store.mkdir(parents=True)
        self.workspace = self.home / "dev" / "project"
        (self.workspace / ".git").mkdir(parents=True)

    def handoff(self, name, title, workspace, age, status="open"):
        path = self.store / name
        path.write_text(f"# {title}\nStatus: {status} · Updated: now\nWorkspace: {workspace}\n\n## Goal\nGoal of {title}\n")
        stamp = 1_800_000_000 - age
        os.utime(path, (stamp, stamp))
        return path


class NamingTests(StoreFixture):
    def test_task_path_has_bounded_slug_and_disambiguates_full_identity(self):
        paths = [handoffs.session_handoff_path({}, identity, self.store, "../Fix Billing! " * 20)
                 for identity in ("cursor\0parent", "cursor\0child")]
        self.assertNotEqual(paths[0], paths[1])
        for path in paths:
            self.assertEqual(Path(path).parent, self.store)
            slug, suffix = Path(path).stem.rsplit("-", 1)
            self.assertLessEqual(len(slug), 40)
            self.assertTrue(slug.startswith("fix-billing"))
            self.assertRegex(slug, r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
            self.assertRegex(suffix, r"^[0-9a-f]{16}$")

    def test_semantic_first_save_is_adopted_and_existing_path_wins(self):
        original = self.store / "opening-prompt-0123456789abcdef.md"
        saved = self.store / "fix-invoice-currency-rounding-0123456789abcdef.md"
        saved.write_text("# Task\n")
        self.assertEqual(handoffs.saved_task_path(str(original)), str(saved))
        original.write_text("# Existing\n")
        self.assertEqual(handoffs.saved_task_path(str(original)), str(original))

    def test_missing_ambiguous_symlink_and_short_suffix_paths_are_not_adopted(self):
        original = self.store / "opening-prompt-0123456789abcdef.md"
        first = self.store / "fix-invoice-rounding-0123456789abcdef.md"
        second = self.store / "review-invoice-rounding-0123456789abcdef.md"
        self.assertEqual(handoffs.saved_task_path(str(original)), str(original))
        target = self.home / "target.md"
        target.write_text("# Task\n")
        first.symlink_to(target)
        self.assertEqual(handoffs.saved_task_path(str(original)), str(original))
        first.unlink()
        first.write_text("# Task\n")
        second.write_text("# Task\n")
        self.assertEqual(handoffs.saved_task_path(str(original)), str(original))
        short = self.store / "opening-01234567.md"
        (self.store / "semantic-01234567.md").write_text("# Task\n")
        self.assertEqual(handoffs.saved_task_path(str(short)), str(short))

    def test_label_capture_ignores_placeholders_and_bookkeeping(self):
        state = {}
        handoffs.capture_label(state, "New chat")
        handoffs.capture_label(state, "[context-guard] refresh")
        self.assertEqual(handoffs.capture_label(state, "Fix billing"), "Fix billing")
        self.assertEqual(handoffs.capture_label(state, "Something else"), "Fix billing")


class ListingTests(StoreFixture):
    def lines(self, cwd=None, limit=5):
        text = handoffs.listing(self.store, str(cwd or self.workspace), self.home, limit)
        return [line for line in text.splitlines() if line.startswith("- ")], text

    def test_same_workspace_ranks_first_and_only_top_match_is_marked(self):
        self.handoff("other-aaaaaaaaaaaaaaaa.md", "Other", self.home / "elsewhere", age=10)
        self.handoff("related-bbbbbbbbbbbbbbbb.md", "Related", self.workspace / "sub", age=20)
        self.handoff("mine-old-cccccccccccccccc.md", "Mine old", self.workspace, age=300)
        self.handoff("mine-new-dddddddddddddddd.md", "Mine new", self.workspace, age=200)
        self.handoff("gone-eeeeeeeeeeeeeeee.md", "Gone", self.home / "deleted-worktree", age=1)
        (self.home / "elsewhere").mkdir()
        entries, _ = self.lines()
        titles = [line.split(" — ")[0][2:] for line in entries]
        self.assertEqual(titles, ["Mine new", "Mine old", "Related", "Other", "Gone"])
        self.assertIn("(likely match)", entries[0])
        self.assertEqual(sum("(likely match)" in line for line in entries), 1)
        self.assertIn("workspace missing", entries[-1])

    def test_no_likely_match_without_same_workspace(self):
        self.handoff("other-aaaaaaaaaaaaaaaa.md", "Other", self.home / "elsewhere", age=10)
        entries, _ = self.lines()
        self.assertEqual(len(entries), 1)
        self.assertNotIn("(likely match)", entries[0])

    def test_duplicates_by_identity_and_path_are_listed_once(self):
        self.handoff("opening-prompt-0123456789abcdef.md", "Provisional", self.workspace, age=50)
        self.handoff("fix-rounding-0123456789abcdef.md", "Semantic", self.workspace, age=5)
        target = self.handoff("plain.md", "Plain", self.workspace, age=100)
        (self.store / "alias.md").symlink_to(target)
        entries, _ = self.lines()
        titles = [line.split(" — ")[0][2:] for line in entries]
        self.assertEqual(titles, ["Semantic", "Plain"])

    def test_done_entries_are_hidden(self):
        self.handoff("done-aaaaaaaaaaaaaaaa.md", "Finished", self.workspace, age=1, status="done")
        self.handoff("open-bbbbbbbbbbbbbbbb.md", "Ongoing", self.workspace, age=2)
        entries, _ = self.lines()
        self.assertEqual(len(entries), 1)
        self.assertIn("Ongoing", entries[0])
        (self.store / "open-bbbbbbbbbbbbbbbb.md").unlink()
        self.assertEqual(handoffs.listing(self.store, str(self.workspace), self.home, 5), "")

    def test_overflow_points_to_store_with_count(self):
        for index in range(7):
            self.handoff(f"task-{index:016x}.md", f"Task {index}", self.workspace, age=index)
        entries, text = self.lines(limit=5)
        self.assertEqual(len(entries), 5)
        self.assertIn(f"2 more in {self.store}; search by keyword.", text)

    def test_non_project_cwd_gets_single_pointer_line(self):
        self.handoff("task-aaaaaaaaaaaaaaaa.md", "Task", self.workspace, age=1)
        scratch = self.home / "Documents" / "notes"
        scratch.mkdir(parents=True)
        (self.home / ".git").mkdir()
        for cwd in (scratch, self.home, ""):
            with self.subTest(cwd=cwd):
                text = handoffs.listing(self.store, str(cwd), self.home, 5)
                self.assertEqual(len(text.splitlines()), 1)
                self.assertIn(str(self.store), text)
                self.assertIn("search it by keyword", text)


class PendingTests(StoreFixture):
    def test_pending_target_resolves_semantic_save_without_rewriting_report(self):
        original = self.store / "task-0123456789abcdef.md"
        report = handoffs.publish_pending({"id": "job", "workspace": str(self.workspace), "status": "done",
                                           "handoff": str(original)}, self.store, {})
        saved = self.store / "fix-invoice-rounding-0123456789abcdef.md"
        saved.write_text("# Task\n")
        before = report.read_text()
        self.assertIn(str(saved), handoffs.pending_context([str(self.workspace)], self.store))
        self.assertEqual(report.read_text(), before)

    def test_damaged_reports_do_not_hide_valid_ones_and_other_workspaces_are_ignored(self):
        for index, malformed in enumerate(("bad\0directory", "bad\ud800directory")):
            target = str(self.store / malformed / "task-0123456789abcdef.md")
            self.assertEqual(handoffs.saved_task_path(target), target)
            handoffs.publish_pending({"id": f"damaged-{index}", "workspace": str(self.workspace),
                                      "status": "done", "handoff": target}, self.store, {})
        (self.store / "pending" / "broken.json").write_text("{")
        handoffs.publish_pending({"id": "valid", "workspace": str(self.workspace), "status": "done"},
                                 self.store, {})
        handoffs.publish_pending({"id": "foreign", "workspace": str(self.home), "status": "done"},
                                 self.store, {})
        context = handoffs.pending_context([str(self.workspace)], self.store)
        self.assertIn("valid.json", context)
        self.assertNotIn("foreign.json", context)
        with self.assertRaises(ValueError):
            handoffs.publish_pending({"id": "job", "workspace": str(self.workspace), "status": "running"},
                                     self.store, {})


if __name__ == "__main__":
    unittest.main()
