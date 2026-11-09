import concurrent.futures
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel import assets, cli
from keel.snapshots import check_git
from tests.test_check import DECISION, cleanup_temporary


class WorktreeFixture(unittest.TestCase):
    use_beads = False

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="keel-worktree-test-")
        self.addCleanup(cleanup_temporary, temp)
        self.base = Path(temp.name).resolve()
        self.root, self.worker = self.base / "main", self.base / "worker"
        self.root.mkdir()
        source = Path(__file__).resolve().parents[1]
        # Do not inherit another project's Beads routing or a hook's temporary index.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("BEADS_", "BD_", "GIT_"))}
        env.update(BD_NON_INTERACTIVE="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_CONFIG_NOSYSTEM="1", PYTHONPATH=str(source))
        bin_dir = self.base / "bin"
        bin_dir.mkdir()
        launcher = bin_dir / "keel"
        launcher.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m keel.cli "$@"\n')
        launcher.chmod(0o755)
        env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
        environ = patch.dict(os.environ, env, clear=True)
        environ.start()
        self.addCleanup(environ.stop)
        personal = self.base / "personal.toml"
        personal.write_text('hf_namespace = "test"\ngh_owner = "test"\n')
        storage = patch.object(assets, "PERSONAL_CONFIG", personal)
        storage.start()
        self.addCleanup(storage.stop)
        self.command(self.root, "git", "init", "-q", "-b", "main")
        self.command(self.root, "git", "config", "user.name", "worktree test")
        self.command(self.root, "git", "config", "user.email", "test@example.invalid")
        (self.root / "keel.toml").write_text('skills = []\nsecrets = []\n'
                                             '[storage]\nmodel = "hf://models/test/offline"\n')
        if self.use_beads:
            self.bd(self.root, "init", "--prefix", "wtprobe", "--skip-agents", "--skip-hooks")
            self.task_id = self.bd_json(self.root, "create", "--title", "Shared task", "--type", "task")["id"]
        cli.init_project(self.root)
        note = self.root / "docs/decisions/0001-original.md"
        note.parent.mkdir(parents=True)
        text = DECISION.format(status="accepted", body="原文")
        if self.use_beads:
            text = text.replace("beads: []", f"beads: [{self.task_id}]")
        note.write_text(text)
        self.command(self.root, "git", "add", ".")
        self.command(self.root, "git", "commit", "--no-verify", "-qm", "seed")
        self.command(self.root, "git", "worktree", "add", "-q", "-b", "feature", str(self.worker))

    def command(self, root, *args, ok=True):
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=45)
        if ok:
            self.assertEqual(result.returncode, 0, f"{args}:\n{result.stderr}\n{result.stdout}")
        return result

    def bd(self, root, *args, ok=True):
        return self.command(root, "bd", "--sandbox", *args, ok=ok)

    def bd_json(self, root, *args):
        return json.loads(self.bd(root, *args, "--json").stdout)

    def index(self, root):
        path = self.command(root, "git", "rev-parse", "--path-format=absolute", "--git-path", "index")
        return Path(path.stdout.strip()).read_bytes()


class TestWorktreeGit(WorktreeFixture):
    def test_hook_checks_worker_index_and_leaves_main_index_alone(self):
        (self.root / "README.md").write_text("staged only in main\n")
        self.command(self.root, "git", "add", "README.md")
        main_head = self.command(self.root, "git", "rev-parse", "HEAD").stdout
        note = self.worker / "docs/decisions/0001-original.md"
        original = note.read_text()
        note.write_text(original.replace("原文", "违规改写"))
        self.command(self.worker, "git", "add", str(note))
        note.write_text(original)
        indices = (self.index(self.root), self.index(self.worker))
        self.assertEqual(cli.check(cli.load_project(self.worker)), [])
        self.assertEqual(check_git(self.root, staged=True), [])
        self.assertIn("append-only", {f.rule for f in check_git(self.worker, staged=True)})
        self.assertEqual((self.index(self.root), self.index(self.worker)), indices)
        # Exercise both Git's shared default hooks and an absolute core.hooksPath.
        for absolute_path in (False, True):
            with self.subTest(absolute_hooks=absolute_path):
                if absolute_path:
                    self.command(self.root, "git", "config", "core.hooksPath", str(self.root / ".git/hooks"))
                result = self.command(self.worker, "git", "commit", "-qm", "invalid staged note", ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("append-only", result.stderr)
        self.command(self.worker, "git", "restore", "--staged", str(note))
        note.write_text(original.replace("原文", "未暂存的改写"))
        (self.worker / "README.md").write_text("valid staged content\n")
        self.command(self.worker, "git", "add", "README.md")
        self.command(self.worker, "git", "commit", "-qm", "valid worker commit")
        self.assertIn("未暂存的改写", note.read_text())
        self.assertEqual(self.command(self.root, "git", "rev-parse", "HEAD").stdout, main_head)
        self.assertEqual(self.index(self.root), indices[0])

    def test_merge_of_parallel_decisions_requires_renumbering(self):
        main_note = cli.new_note(cli.load_project(self.root), "decision", "main decision", None, [])
        worker_note = cli.new_note(cli.load_project(self.worker), "decision", "worker decision", None, [])
        self.assertEqual(main_note.name[:5], worker_note.name[:5])
        for root in (self.root, self.worker):
            self.command(root, "git", "add", ".")
            self.command(root, "git", "commit", "-qm", "parallel decision")
        self.command(self.root, "git", "merge", "--no-commit", "--no-ff", "feature")
        self.assertIn("duplicate-number", {f.rule for f in check_git(self.root, staged=True)})
        result = self.command(self.root, "git", "commit", "-qm", "colliding merge", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate-number", result.stderr)
        incoming = self.root / worker_note.relative_to(self.worker)
        self.command(self.root, "git", "mv", str(incoming), str(incoming.with_name("0003-worker-decision.md")))
        self.command(self.root, "git", "commit", "-qm", "merge with unique numbers")
        self.assertEqual(check_git(self.root, ref="HEAD"), [])


@unittest.skipUnless(shutil.which("bd"), "real Beads CLI is required for database integration tests")
class TestWorktreeBeads(WorktreeFixture):
    use_beads = True

    def test_init_uses_the_database_discovered_by_beads(self):
        main = self.bd_json(self.root, "where")
        self.assertEqual(main["database_path"], self.bd_json(self.worker, "where")["database_path"])
        self.assertFalse((self.worker / ".beads/embeddeddolt").exists())
        cli.init_project(self.worker, commit=True)
        self.assertEqual(main["database_path"], self.bd_json(self.worker, "where")["database_path"])
        self.assertFalse((self.worker / ".beads/embeddeddolt").exists())
        self.assertEqual(self.bd_json(self.worker, "show", self.task_id)[0]["id"], self.task_id)
        self.assertEqual(check_git(self.worker, staged=True), [])
        # Keep the real Beads hook installed: upgrading it must not bypass Keel.
        note = self.worker / "docs/decisions/0001-original.md"
        original = note.read_text()
        note.write_text(original.replace("原文", "违规改写"))
        self.command(self.worker, "git", "add", str(note))
        note.write_text(original)
        result = self.command(self.worker, "git", "commit", "-qm", "invalid with beads hook", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("append-only", result.stderr)
        self.command(self.worker, "git", "restore", "--staged", str(note))
        (self.worker / "README.md").write_text("valid change with both hooks\n")
        self.command(self.worker, "git", "add", "README.md")
        self.command(self.worker, "git", "commit", "-qm", "valid with beads hook")
        self.assertEqual(main["database_path"], self.bd_json(self.worker, "where")["database_path"])

    def test_distinct_actors_claim_once_and_the_winner_can_repeat(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.bd, root, "--actor", actor, "update", self.task_id,
                                   "--claim", "--json", ok=False)
                       for root, actor in ((self.root, "agent-main"), (self.worker, "agent-worker"))]
            results = [future.result() for future in futures]
        self.assertEqual(sorted(r.returncode for r in results), [0, 1])
        winner = self.bd_json(self.worker, "show", self.task_id)[0]["assignee"]
        self.assertIn(winner, ("agent-main", "agent-worker"))
        # A repeat by the same actor is idempotent, not a second distinct claimant.
        self.bd(self.worker, "--actor", winner, "update", self.task_id, "--claim")
        self.assertEqual(self.bd_json(self.root, "show", self.task_id)[0]["assignee"], winner)

    def test_separate_clone_has_its_own_database(self):
        clone = self.base / "clone"
        self.command(self.base, "git", "clone", "-q", "--no-local", str(self.root), str(clone))
        self.bd(clone, "bootstrap")  # origin is a local fixture, with no Dolt remote history.
        clone_db = self.bd_json(clone, "where")["database_path"]
        self.assertNotEqual(clone_db, self.bd_json(self.root, "where")["database_path"])
        self.assertTrue(Path(clone_db).is_relative_to(clone))
        clone_task = self.bd_json(clone, "create", "--title", "Only in clone", "--type", "task")["id"]
        main_ids = {row["id"] for row in self.bd_json(self.root, "list", "--all", "--limit", "0")}
        self.assertNotIn(clone_task, main_ids)
