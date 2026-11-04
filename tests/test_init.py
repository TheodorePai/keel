import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel import cli
from tests.test_check import Base, run


@unittest.skipUnless(shutil.which("bd"), "real Beads CLI is required for publish integration")
class TestPublishBeads(Base):
    def test_publish_pushes_task_ref_to_the_project_git_origin(self):
        self.commit()
        with tempfile.TemporaryDirectory(prefix="keel-publish-origin-") as tmp:
            origin = Path(tmp) / "origin.git"
            run(self.root, "git", "init", "--bare", "-q", str(origin))
            run(self.root, "git", "remote", "add", "origin", str(origin))
            run(self.root, "bd", "init", "--prefix", "publishprobe", "--skip-hooks", "--skip-agents")
            task = subprocess.check_output(
                ["bd", "create", "--title", "Survives publishing", "--type", "task", "--silent"],
                cwd=self.root, text=True).strip()
            messages = cli.publish_github(self.root)
            self.assertIn("已推送任务库（refs/dolt/data）", messages)
            remote_ref = subprocess.check_output(
                ["git", "ls-remote", "origin", "refs/dolt/data"], cwd=self.root, text=True)
            self.assertIn("refs/dolt/data", remote_ref)
            clone = Path(tmp) / "clone"
            run(self.root, "git", "clone", "-q", str(origin), str(clone))
            run(clone, "bd", "bootstrap")
            restored = subprocess.check_output(["bd", "show", task, "--json"], cwd=clone, text=True)
            self.assertIn("Survives publishing", restored)


class TestInitFailures(Base):
    def test_failed_bd_init_stops_publish_and_can_retry(self):
        (self.root / "keel.toml").unlink()
        real_sh = cli.sh
        attempts = []

        def task_command(root, *args):
            if args[:2] == ("bd", "init"):
                attempts.append(args)
                (root / ".beads").mkdir(exist_ok=True)
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(args, 1, "", "task database unavailable")
                (root / ".beads/embeddeddolt").mkdir()
                return subprocess.CompletedProcess(args, 0, "", "")
            return real_sh(root, *args)

        with patch.object(cli, "sh", side_effect=task_command), \
                patch.object(cli.shutil, "which", return_value="/fake/bd"), \
                patch.object(cli, "install_hook", return_value=[]) as hooks, \
                patch.object(cli, "commit_managed", return_value=[]), \
                patch.object(cli, "publish_github", return_value=[]) as publish:
            with self.assertRaisesRegex(SystemExit, "task database unavailable"):
                cli.init_project(self.root, commit=True, github=True)
            self.assertFalse((self.root / "keel.toml").exists())
            hooks.assert_not_called()
            publish.assert_not_called()
            cli.init_project(self.root, commit=True, github=True)
            publish.assert_called_once()
        self.assertEqual(len(attempts), 2)

    def test_clone_bootstrap_failure_stops_following_steps(self):
        d = self.root / ".beads"
        d.mkdir()
        (d / "metadata.json").write_text('{}\n')
        self.commit()
        real_sh = cli.sh

        def fail_bootstrap(root, *args):
            if args[:2] == ("bd", "bootstrap"):
                return subprocess.CompletedProcess(args, 1, "", "remote unavailable")
            return real_sh(root, *args)

        with patch.object(cli, "sh", side_effect=fail_bootstrap), \
                patch.object(cli.shutil, "which", return_value="/fake/bd"), \
                patch.object(cli.assets, "ensure_storage") as storage, \
                patch.object(cli, "publish_github") as publish:
            with self.assertRaisesRegex(SystemExit, "remote unavailable"):
                cli.init_project(self.root, commit=True, github=True)
            storage.assert_not_called()
            publish.assert_not_called()

    def test_commit_hook_failure_stops_publish_and_retry_keeps_other_changes(self):
        self.commit()
        (self.root / "keel.toml").write_text('ignore = []\n')
        (self.root / "app.py").write_text('print("user work")\n')
        run(self.root, "git", "add", "app.py")
        hook = self.root / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n")
        hook.chmod(0o755)
        with patch.object(cli, "setup_beads", return_value=[]), \
                patch.object(cli, "install_hook", return_value=[]), \
                patch.object(cli, "publish_github", return_value=[]) as publish:
            with self.assertRaisesRegex(SystemExit, "rejected"):
                cli.init_project(self.root, commit=True, github=True)
            publish.assert_not_called()
            hook.write_text("#!/bin/sh\nexit 0\n")
            cli.init_project(self.root, commit=True, github=True)
            publish.assert_called_once()
        staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=self.root, text=True)
        self.assertEqual(staged.strip(), "app.py")
        committed = subprocess.check_output(["git", "ls-tree", "--name-only", "HEAD"], cwd=self.root, text=True)
        self.assertNotIn("app.py", committed)

    def test_missing_bd_and_git_init_failure_are_errors(self):
        with patch.object(cli.shutil, "which", return_value=None):
            with self.assertRaisesRegex(SystemExit, "未安装 bd"):
                cli.setup_beads(self.root, None)
        new = self.root / "new"
        new.mkdir()
        failure = subprocess.CompletedProcess([], 1, "", "cannot initialize repository")
        with patch.object(cli, "sh", return_value=failure), patch.object(cli, "setup_beads") as beads:
            with self.assertRaisesRegex(SystemExit, "cannot initialize repository"):
                cli.init_project(new, commit=True)
            beads.assert_not_called()


class TestInitLayout(Base):
    def test_fresh_init_scaffolds_work_and_passes_check(self):
        (self.root / "keel.toml").unlink()
        cli.init_project(self.root)
        self.assertTrue((self.root / "work/README.md").is_file())
        self.assertIn("work/.local/", (self.root / ".gitignore").read_text().splitlines())
        (self.root / "work/.local/run").mkdir(parents=True)
        (self.root / "work/.local/run/log.txt").write_text("x\n")
        self.assertEqual([f for f in cli.check(cli.load_project(self.root)) if f.level == "error"], [])
        cli.init_project(self.root)
        self.assertEqual((self.root / ".gitignore").read_text().count("work/.local/"), 1)
