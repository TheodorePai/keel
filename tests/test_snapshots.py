import os
import shutil
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from keel import assets, skills, snapshots
from keel.cli import check, init_project, load_project
from keel.snapshots import check_git
from tests.test_check import Base, DECISION, IDEA, run


class TestSnapshots(Base):
    def test_local_tracking_and_ignore_rules_are_checked_in_staged_tree_and_history(self):
        self.commit()
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.root, text=True).strip()
        path = self.root / "work/.local/run.log"
        path.parent.mkdir(parents=True)
        path.write_text("log\n")
        run(self.root, "git", "add", "-f", "work/.local/run.log")
        self.assertIn("local-tracked", {f.rule for f in check_git(self.root, staged=True)})
        run(self.root, "git", "commit", "--no-verify", "-qm", "invalid runtime content")
        self.assertIn("local-tracked", {f.rule for f in check_git(self.root, ref="HEAD")})
        run(self.root, "git", "rm", "--cached", "work/.local/run.log")
        ignore = self.root / ".gitignore"
        ignore.write_text("")
        run(self.root, "git", "add", ".gitignore")
        ignore.write_text("work/.local/\n")
        self.assertEqual(check(load_project(self.root)), [])
        self.assertIn("local-ignore", {f.rule for f in check_git(self.root, staged=True)})
        run(self.root, "git", "commit", "--no-verify", "-qm", "invalid ignore")
        self.assertIn("local-ignore", {f.rule for f in check_git(self.root, since=base)})

    def test_runtime_files_cannot_satisfy_note_links(self):
        local = self.root / "work/.local/cache/cached-only.md"
        local.parent.mkdir(parents=True)
        local.write_text("# cached\n")
        self.write("ideas/2026-11-03-a.md", IDEA + "[[cached-only]]\n"
                   "[cached](../../work/.local/cache/cached-only.md)\n")
        run(self.root, "git", "add", ".")
        for found in (check(load_project(self.root)), check_git(self.root, staged=True)):
            self.assertEqual({f.rule for f in found}, {"link", "local-link"})

    def test_snapshots_use_lf_with_unicode_paths_and_preserve_source(self):
        for autocrlf in ("false", "true"):
            with self.subTest(autocrlf=autocrlf):
                with tempfile.TemporaryDirectory(prefix="keel snapshot ") as tmp:
                    root = Path(tmp) / "项目 with spaces"
                    root.mkdir()
                    run(root, "git", "init", "-q")
                    run(root, "git", "config", "user.email", "t@t")
                    run(root, "git", "config", "user.name", "t")
                    run(root, "git", "config", "core.autocrlf", autocrlf)
                    (root / "keel.toml").write_text("skills = []\n")
                    (root / "AGENTS.md").write_text("# AGENTS.md\n")
                    (root / "CLAUDE.md").write_text("@AGENTS.md\n")
                    (root / ".gitignore").write_text("work/.local/\n")
                    run(root, "git", "add", ".")
                    run(root, "git", "commit", "--no-verify", "-qm", "seed")
                    baseline = snapshots.command(root, "rev-parse", "HEAD")
                    note = root / "docs/ideas/2026-11-03-a.md"
                    note.parent.mkdir(parents=True)
                    note.write_text(IDEA)
                    run(root, "git", "add", ".")
                    run(root, "git", "commit", "--no-verify", "-qm", "note")
                    (root / "README.md").write_text("# staged content\n")
                    run(root, "git", "add", ".")
                    index = (root / ".git/index").read_bytes()
                    files = {p.relative_to(root): p.read_bytes() for p in root.rglob("*")
                             if p.is_file() and ".git" not in p.relative_to(root).parts}
                    objects = snapshots.command(root, "rev-parse", "--path-format=absolute",
                                                "--git-path", "objects")
                    inspected = []

                    def inspect_snapshot(project, **options):
                        alternates = project.root / ".git/objects/info/alternates"
                        data = alternates.read_bytes()
                        self.assertTrue(data.endswith(b"\n"))
                        self.assertFalse(data.endswith(b"\r\n"))
                        self.assertEqual(alternates.read_text().splitlines(), [objects])
                        inspected.append(project.root)
                        return check(project, **options)

                    with patch.object(snapshots, "check", side_effect=inspect_snapshot):
                        for options in ({"staged": True}, {"ref": "HEAD"}, {"since": baseline}):
                            with self.subTest(options=options):
                                self.assertEqual(check_git(root, **options), [])
                    self.assertEqual(len(inspected), 3)
                    self.assertEqual((root / ".git/index").read_bytes(), index)
                    self.assertEqual({p: (root / p).read_bytes() for p in files}, files)

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

    def test_registered_ignored_assets_are_checked_at_the_original_location(self):
        folder = self.root / "work/data/local"
        folder.mkdir(parents=True)
        (folder / "source.md").write_text("private source\n")
        store = assets.Store(self.root, load_project(self.root).config)
        assets.add(store, "work/data/local", "local", name="local", shareable=False)
        self.write("ideas/2026-11-03-a.md", IDEA + "[source](../../work/data/local/source.md)\n")
        run(self.root, "git", "add", ".")
        index = (self.root / ".git/index").read_bytes()
        self.assertEqual(check_git(self.root, staged=True), [])
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        (folder / "source.md").unlink()
        self.assertIn("link", {f.rule for f in check_git(self.root, staged=True)})
        folder.rmdir()
        self.assertNotIn("asset-ignore", {f.rule for f in check_git(self.root, staged=True)})
        (self.root / ".gitignore").write_text("work/.local/\n")
        run(self.root, "git", "add", ".gitignore")
        (self.root / ".gitignore").write_text("work/.local/\n/work/data/local/\n")
        folder.mkdir()
        (folder / "source.md").write_text("private source\n")
        rules = {f.rule for f in check_git(self.root, staged=True)}
        self.assertTrue({"asset-ignore", "link"} <= rules)

    def test_relative_links_outside_repository_keep_the_original_base(self):
        with tempfile.TemporaryDirectory(dir=self.root.parent) as tmp:
            external = Path(tmp) / "source.md"
            external.write_text("external source\n")
            target = os.path.relpath(external, self.root / "docs/ideas")
            self.write("ideas/2026-11-03-a.md", IDEA + f"[source]({target})\n")
            run(self.root, "git", "add", ".")
            self.assertEqual(check_git(self.root, staged=True), [])
            external.unlink()
            self.assertIn("link", {f.rule for f in check_git(self.root, staged=True)})

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
