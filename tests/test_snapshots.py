import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from keel import skills
from keel.cli import check, init_project, load_project
from keel.snapshots import check_git
from tests.test_check import Base, DECISION, IDEA, run


class TestSnapshots(Base):
    def test_staged_claude_rules_cannot_hide_behind_clean_worktree(self):
        self.commit()
        path = self.root / "CLAUDE.md"
        path.write_text("@AGENTS.md\n额外规则\n")
        run(self.root, "git", "add", "CLAUDE.md")
        path.write_text("@AGENTS.md\n")
        self.assertEqual(check(load_project(self.root)), [])
        self.assertIn("agent-entrypoint", {f.rule for f in check_git(self.root, staged=True)})

    def test_unstaged_claude_rules_do_not_block_valid_commit(self):
        self.commit()
        (self.root / "CLAUDE.md").write_text("@AGENTS.md\n额外规则\n")
        self.assertIn("agent-entrypoint", {f.rule for f in check(load_project(self.root))})
        self.assertEqual(check_git(self.root, staged=True), [])

    def test_entrypoints_must_exist_in_staged_tree(self):
        self.commit()
        for name in ("CLAUDE.md", "AGENTS.md"):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_text()
                run(self.root, "git", "rm", name)
                path.write_text(original)
                found = check_git(self.root, staged=True)
                self.assertEqual([(f.path, f.rule) for f in found], [(name, "agent-entrypoint")])
                run(self.root, "git", "add", name)

    def final_note(self):
        p = self.write("decisions/0001-a.md", DECISION.format(status="accepted", body="原文"))
        self.commit()
        return p

    def test_staged_rewrite_cannot_hide_behind_clean_worktree(self):
        p = self.final_note()
        original = p.read_text()
        p.write_text(original.replace("原文", "违规改写"))
        run(self.root, "git", "add", ".")
        p.write_text(original)
        index = (self.root / ".git/index").read_bytes()
        self.assertEqual(check(load_project(self.root)), [])
        self.assertIn("append-only", {f.rule for f in check_git(self.root, staged=True)})
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(p.read_text(), original)

    def test_unstaged_edit_does_not_block_valid_commit(self):
        p = self.final_note()
        p.write_text(p.read_text().replace("原文", "未暂存的改写"))
        self.assertEqual(check_git(self.root, staged=True), [])

    def test_final_deletion_rejected_and_rename_allowed(self):
        p = self.final_note()
        run(self.root, "git", "mv", str(p), str(p.with_name("0001-b.md")))
        self.assertEqual(check_git(self.root, staged=True), [])
        self.commit()
        run(self.root, "git", "rm", str(p.with_name("0001-b.md")))
        self.assertIn("append-only", {f.rule for f in check_git(self.root, staged=True)})

    def test_type_change_cannot_bypass_finality(self):
        p = self.final_note()
        p.write_text(p.read_text().replace("type: decision", "type: idea").replace("status: accepted", "status: open"))
        self.assertIn("append-only", {f.rule for f in check(load_project(self.root))})

    def test_staged_link_requires_staged_target(self):
        self.write("ideas/2026-11-03-a.md", IDEA + "[target](2026-11-03-b.md)\n")
        run(self.root, "git", "add", ".")
        self.write("ideas/2026-11-03-b.md", IDEA)
        self.assertIn("link", {f.rule for f in check_git(self.root, staged=True)})
        run(self.root, "git", "add", ".")
        self.assertEqual(check_git(self.root, staged=True), [])

    def test_history_range_catches_rewrite_even_if_later_restored(self):
        p = self.final_note()
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        original = p.read_text()
        p.write_text(original.replace("原文", "违规改写"))
        self.commit()
        p.write_text(original)
        self.commit()
        self.assertEqual(check_git(self.root, ref=base), [])
        self.assertIn("append-only", {f.rule for f in check_git(self.root, since=base)})

    def test_history_ignores_available_updates_but_keeps_integrity_checks(self):
        config = self.root / "keel.toml"
        config.write_text(config.read_text() + 'skills = ["keel"]\n')
        init_project(self.root)
        self.commit("old skill release")
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        (self.root / "README.md").write_text("# new code\n")
        self.commit("code change")
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp)
            shutil.copytree(self.root / ".agents/skills/keel", bundle / "keel")
            new = bundle / "keel/SKILL.md"
            new.write_text(new.read_text() + "\n# New release\n")
            with patch.object(skills, "BUNDLED_DIR", bundle):
                self.assertIn("skill-outdated", {f.rule for f in check(load_project(self.root))})
                self.assertIn("skill-outdated", {f.rule for f in check_git(self.root, staged=True)})
                self.assertEqual(check_git(self.root, ref="HEAD"), [])
                self.assertEqual(check_git(self.root, since=base), [])
                local = self.root / ".agents/skills/keel/SKILL.md"
                local.write_text(local.read_text().replace("name: keel", "name: invalid-name"))
                self.commit("tampered skill")
                findings = check_git(self.root, since=base)
                rules = {f.rule for f in findings}
                self.assertNotIn("skill-outdated", rules)
                self.assertTrue({"skill-modified", "skill-mirror", "skill-format"} <= rules)

    def test_hook_index_is_respected_and_restored(self):
        p = self.final_note()
        index = self.root / ".git/commit-index"
        shutil.copyfile(self.root / ".git/index", index)
        p.write_text(p.read_text().replace("原文", "违规改写"))
        with patch.dict(os.environ, {"GIT_INDEX_FILE": str(index)}):
            run(self.root, "git", "add", ".")
            self.assertIn("append-only", {f.rule for f in check_git(self.root, staged=True)})
            self.assertEqual(os.environ["GIT_INDEX_FILE"], str(index))
        self.assertEqual(check_git(self.root, staged=True), [])

    def test_real_commit_hook_checks_index_instead_of_worktree(self):
        p = self.final_note()
        original = p.read_text()
        init_project(self.root)
        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "keel"
            launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m keel.cli "$@"\n')
            launcher.chmod(0o755)
            env = {**os.environ, "PATH": tmp + os.pathsep + os.environ["PATH"],
                   "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            p.write_text(original.replace("原文", "违规改写"))
            run(self.root, "git", "add", ".")
            p.write_text(original)
            result = subprocess.run(["git", "commit", "-qm", "invalid staged note"], cwd=self.root,
                                    capture_output=True, text=True, env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("append-only", result.stderr)
            run(self.root, "git", "add", str(p))
            p.write_text(original.replace("原文", "未暂存的改写"))
            result = subprocess.run(["git", "commit", "-qm", "valid staged notes"], cwd=self.root,
                                    capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("未暂存的改写", p.read_text())
