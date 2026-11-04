from keel.cli import check, load_project
from keel.snapshots import check_git
from tests.test_check import Base, DECISION, run


class TestNumbering(Base):
    def test_duplicate_number_with_different_titles_is_rejected(self):
        self.write("decisions/0001-first.md", DECISION.format(status="proposed", body="first"))
        self.write("decisions/0001-second.md", DECISION.format(status="accepted", body="second"))
        findings = check(load_project(self.root))
        self.assertEqual([f.rule for f in findings], ["duplicate-number"])
        self.assertEqual(findings[0].level, "error")
        self.assertIn("0001-first.md", findings[0].message)
        self.assertIn("0001-second.md", findings[0].message)

    def test_only_numbered_types_and_non_ignored_notes_are_checked(self):
        (self.root / "keel.toml").write_text('ignore = ["decisions/0001-ignored.md"]\n')
        self.write("decisions/0001-first.md", DECISION.format(status="proposed", body="first"))
        self.write("decisions/0001-ignored.md", "not a note")
        for name in ("0001-first.md", "0001-second.md"):
            self.write(f"reference/{name}", "---\ntype: reference\nstatus: current\n---\n")
        self.assertEqual(check(load_project(self.root)), [])

    def test_staged_and_committed_duplicates_cannot_hide_behind_worktree(self):
        self.write("decisions/0001-first.md", DECISION.format(status="proposed", body="first"))
        self.commit("first decision")
        duplicate = self.write("decisions/0001-second.md", DECISION.format(status="proposed", body="second"))
        run(self.root, "git", "add", ".")
        renamed = duplicate.with_name("0002-second.md")
        duplicate.rename(renamed)
        self.assertEqual(check(load_project(self.root)), [])
        self.assertIn("duplicate-number", {f.rule for f in check_git(self.root, staged=True)})
        # Commit the staged collision without the hook so history checking sees it too.
        run(self.root, "git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "collision")
        self.assertIn("duplicate-number", {f.rule for f in check_git(self.root, ref="HEAD")})
