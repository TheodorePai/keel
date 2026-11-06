import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from keel import layout


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.addCleanup(self.tmp.cleanup)
        self.put("keel.toml", "")
        self.put("docs/x.md", "")
        self.put(".gitignore", "work/.local/\n")

    def put(self, rel, text="x\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def found(self, config=None):
        return {(p, r) for p, r, _, _ in layout.checks(self.root, config or {})}

    def good_work(self):
        self.put("work/README.md", "- [data](data/README.md)：数据集\n- [app](./app/)：应用\n")
        self.put("work/data/README.md")
        self.put("work/app/README.md")
        self.put("work/app/src/main.py")

    def test_clean_project_passes(self):
        self.good_work()
        self.put(".github/workflows/ci.yml")
        self.assertEqual(self.found(), set())

    def test_root_allows_only_fixed_entries(self):
        self.put("output/run.log")
        self.put("build.sh")
        self.put(".gitignore", "work/.local/\nignored/\n")
        self.put("ignored/big.bin")
        self.assertEqual(self.found(), {("output", "layout-root"), ("build.sh", "layout-root")})

    def test_notes_root_is_fixed(self):
        self.assertIn(("keel.toml", "layout-root"), self.found({"notes_root": "notes"}))

    def test_every_work_dir_needs_readme_and_map_entry(self):
        self.good_work()
        self.put("work/eval/run.py")
        self.assertEqual(self.found(), {("work/eval", "layout-readme"), ("work/README.md", "layout-map")})

    def test_map_rejects_missing_dirs_and_ignores_external_links(self):
        self.good_work()
        self.put("work/README.md", "[data](data/) [app](app/) [gone](gone/README.md) "
                                   "[x](https://example.com/a/) [up](../docs/)\n")
        self.assertEqual(self.found(), {("work/README.md", "layout-map")})

    def test_map_required_once_work_has_dirs(self):
        self.put("work/data/README.md")
        self.assertEqual(self.found(), {("work/README.md", "layout-map")})

    def test_dumping_ground_names_rejected_at_any_depth(self):
        self.good_work()
        for rel in ["work/app/tmp/a", "work/app/src/model_v2/a", "work/data/run-2026-10-01/a",
                    "work/data/Archive/a"]:
            self.put(rel)
        self.assertEqual({p for p, r in self.found() if r == "layout-name"},
                         {"work/app/tmp", "work/app/src/model_v2", "work/data/run-2026-10-01",
                          "work/data/Archive"})

    def test_local_is_the_outlet_for_run_products(self):
        self.good_work()
        self.put("work/.local/tmp/output/x")
        self.assertEqual(self.found(), set())

    def test_local_ignore_must_come_from_project_and_exclude_the_directory(self):
        for rules in ("", "work/.local/*\n", "work/.local/\n!work/.local/\n"):
            with self.subTest(rules=rules):
                self.put(".gitignore", rules)
                self.assertIn((".gitignore", "local-ignore"), self.found())
        self.put(".gitignore", "")
        self.put(".git/info/exclude", "work/.local/\n")
        self.assertIn((".gitignore", "local-ignore"), self.found())
        self.put(".gitignore", "/work/.local/\n")
        self.assertNotIn((".gitignore", "local-ignore"), self.found())

    def test_forced_tracked_local_files_are_rejected_even_if_deleted_from_worktree(self):
        self.put("work/.local/run.log")
        subprocess.run(["git", "add", "-f", "work/.local/run.log"], cwd=self.root, check=True)
        for exists in (True, False):
            with self.subTest(exists=exists):
                if not exists:
                    (self.root / "work/.local/run.log").unlink()
                self.assertIn(("work/.local/run.log", "local-tracked"), self.found())

    def test_ignored_tool_default_directories_are_allowed(self):
        self.good_work()
        self.put(".gitignore", "work/.local/\nnode_modules/\n__pycache__/\n.venv/\n")
        for path in ("work/app/node_modules/pkg/index.js", "work/app/__pycache__/main.pyc",
                     "work/.venv/bin/python"):
            self.put(path)
        self.assertEqual(self.found(), set())

    def test_non_git_file_scan_prunes_runtime_directory(self):
        self.put("work/.local/cache/cached-only.md")
        self.put("work/app/README.md")
        visited = []
        import os
        walk = os.walk

        def recording_walk(*args, **kwargs):
            for parent, dirs, names in walk(*args, **kwargs):
                visited.append(Path(parent))
                yield parent, dirs, names

        with patch.object(layout.subprocess, "run", side_effect=FileNotFoundError), \
                patch.object(layout.os, "walk", side_effect=recording_walk):
            paths = layout.files(self.root)
        self.assertIn("work/app/README.md", paths)
        self.assertFalse(any(".local" in p.parts for p in visited))


class TestVerify(unittest.TestCase):
    def setUp(self):
        from tests.test_check import Base
        self.base = Base("setUp")
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.root = self.base.root
        (self.root / "work").mkdir()

    def configure(self, command):
        (self.root / "keel.toml").write_text(f"verify = {command!r}\n")

    def test_failing_verify_blocks_check_and_staged_commit(self):
        from keel.cli import check, load_project
        from keel.snapshots import check_git
        self.configure("test -f ok || { echo 'contract broken'; exit 3; }")
        self.base.commit()
        found = [f for f in check(load_project(self.root)) if f.rule == "verify"]
        self.assertEqual(len(found), 1)
        self.assertIn("contract broken", found[0].message)
        self.assertIn("verify", {f.rule for f in check_git(self.root, staged=True)})
        (self.root / "work/ok").write_text("")   # 在 work/ 下执行
        self.assertNotIn("verify", {f.rule for f in check(load_project(self.root))})

    def test_history_checks_do_not_rerun_verify(self):
        from keel.snapshots import check_git
        self.configure("exit 1")
        self.base.commit()
        self.assertNotIn("verify", {f.rule for f in check_git(self.root, ref="HEAD")})
