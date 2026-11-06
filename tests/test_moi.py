"""Observable adapter boundaries; no moi installation or live account needed."""
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from moi.bridge import dispatch, discover_projects, explain, issues, machine_project, machine_usage, overview, read_note, task_summary
from moi.setup import install


class MoiTest(unittest.TestCase):
    def task(self, **values):
        return issues([{"id": "p-1", "title": "可读的任务", "status": "open", **values}])[0]

    def test_untrusted_task_shapes_do_not_become_empty_success(self):
        for value in [{}, [None], [{"id": "p-1", "title": []}], [{"id": "p-1", "title": "ok", "status": "surprise"}]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                issues(value)
        item = self.task(labels="human", description={"wrong": "shape"})
        self.assertEqual(item["labels"], [])
        self.assertEqual(item["description"], "")

    def test_human_and_milestones_have_separate_sources(self):
        human = self.task(labels=["human"])
        blocked = self.task(id="p-2", status="blocked")
        old = self.task(id="p-3", status="closed", labels=["human"], issue_type="epic", closed_at="2024-01-01")
        new = self.task(id="p-4", status="closed", issue_type="epic", closed_at="2025-01-01")
        result = task_summary([human, blocked, old, new], [], [blocked])
        self.assertEqual([x["id"] for x in result["human"]], ["p-1"])
        self.assertEqual([x["id"] for x in result["blocked"]], ["p-2"])
        self.assertEqual([x["id"] for x in result["milestones"]], ["p-4", "p-3"])

    def test_source_failure_is_unknown_not_zero(self):
        with patch("moi.bridge.query", side_effect=ValueError("unavailable")), patch("moi.bridge.reports", return_value=[]):
            result = overview(Path("."))
        self.assertIsNone(result["tasks"])
        self.assertEqual(result["errors"][0]["source"], "任务")
        self.assertTrue(result["captured_at"])

    def test_note_paths_cannot_escape_or_read_other_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docs").mkdir()
            (root / "private.md").write_text("private")
            (root / "docs" / "escape.md").symlink_to(root / "private.md")
            for name in ["../private.md", "docs/../private.md", str(root / "private.md"), ".env", "docs/data.json", "docs/escape.md"]:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    read_note(root, name)

    def test_no_command_or_invalid_task_rpc(self):
        with patch("moi.bridge.run") as command:
            for action, arg in [("delete", "data"), ("task", "--all"), ("task", "p-1; echo secret"), ("preview", None)]:
                with self.subTest(action=action, arg=arg), self.assertRaises(ValueError):
                    dispatch(Path("."), action, arg)
            command.assert_not_called()

    def test_explanations_expire_when_source_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".agents/skills/moi-keel/presentation.json"
            path.parent.mkdir(parents=True)
            item = self.task()
            path.write_text(json.dumps({"beads:p-1": {"revision": item["revision"], "title": "给人看的说明", "summary": "结果"}}))
            data = {"tasks": task_summary([item], [item], []), "reports": [], "errors": []}
            self.assertEqual(explain(root, data)["tasks"]["ready"][0]["display_title"], "给人看的说明")
            changed = self.task(title="原始任务已变化")
            data = {"tasks": task_summary([changed], [changed], []), "reports": [], "errors": []}
            row = explain(root, data)["tasks"]["ready"][0]
            self.assertNotIn("display_title", row)
            self.assertTrue(row["explanation_stale"])

    def test_check_uses_check_api_not_cli_or_sweep(self):
        called = []
        fake = types.ModuleType("keel.cli")
        fake.load_project = lambda root: root
        fake.check = lambda project: called.append(project) or []
        with patch.dict(sys.modules, {"keel.cli": fake}), patch("moi.bridge.run") as command:
            result = dispatch(Path("project"), "check")
        self.assertEqual(called, [Path("project")])
        self.assertTrue(result["ok"])
        command.assert_not_called()

    def test_preview_calls_safety_engine_without_apply(self):
        calls = []
        fake = types.ModuleType("keel.runtime")
        fake.policy = lambda config: (30, None)
        fake.clean = lambda *args, **kwargs: calls.append(kwargs) or ["preview"]
        cli = types.ModuleType("keel.cli")
        cli.load_project = lambda root: types.SimpleNamespace(config={})
        with patch.dict(sys.modules, {"keel.runtime": fake, "keel.cli": cli}):
            result = dispatch(Path("project"), "preview", "run-1")
        self.assertEqual(calls, [{"days": 30, "apply": False}])
        self.assertEqual(result["body"], "preview")

    def test_setup_is_repeatable_and_stops_before_overwriting_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".moi").mkdir()
            installed = install(root)
            self.assertEqual(installed, install(root))
            edited = root / ".moi/views/home.tsx"
            edited.write_text("user edit")
            other = root / ".moi/widgets/overview.tsx"
            before = other.read_bytes()
            with self.assertRaises(ValueError):
                install(root)
            self.assertEqual(edited.read_text(), "user edit")
            self.assertEqual(other.read_bytes(), before)

    def test_machine_discovery_combines_sources_without_crawling_or_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            registry = root / "projects.txt"
            registry.write_text(f"{root}\n{root / 'registered'}\nrelative/ignored\n")
            fake = types.ModuleType("keel.secrets")
            fake.registry_path = lambda: registry
            index = f"{root}/keel.toml\n{root}/indexed/keel.toml\n{root}/node_modules/sample/keel.toml\n"
            with patch.dict(sys.modules, {"keel.secrets": fake}), patch("moi.bridge.run", return_value=index) as command:
                paths, warnings = discover_projects(root)
            self.assertEqual(paths, [root, root / "indexed", root / "registered"])
            self.assertEqual(warnings, [])
            self.assertEqual(command.call_args.args[1][0], "mdfind")
            self.assertEqual(command.call_args.kwargs["timeout"], 5)
            with patch.dict(sys.modules, {"keel.secrets": fake}), patch("moi.bridge.run", side_effect=ValueError("offline")):
                paths, warnings = discover_projects(root)
            self.assertIn(root / "registered", paths)
            self.assertTrue(warnings)
            index = "\n".join(f"{root}/project-{i}/keel.toml" for i in range(30))
            with patch.dict(sys.modules, {"keel.secrets": fake}), patch("moi.bridge.run", return_value=index):
                paths, warnings = discover_projects(root)
            self.assertEqual(len(paths), 24)
            self.assertEqual(paths[0], root)
            self.assertTrue(warnings)

    def test_machine_project_separates_connection_tasks_and_missing_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(machine_project(root, root)["state"], "unavailable")
            (root / "keel.toml").write_text('skills = ["keel"]')
            (root / "keel.lock").write_text('keel_version = "1.2.3"')
            row = machine_project(root, root)
            self.assertEqual(row["state"], "connected")
            self.assertEqual(row["sync_version"], "1.2.3")
            self.assertIsNone(row["tasks"])
            (root / ".beads").mkdir()
            tasks = [self.task(status="in_progress", updated_at="2026-11-06T00:00:00Z"),
                     self.task(id="p-2", labels=["human"], status="blocked"),
                     self.task(id="p-3", status="in_progress", issue_type="epic", labels=["human"]),
                     self.task(id="p-4", status="closed", labels=["human"])]
            with patch("moi.bridge.query", return_value=tasks) as query:
                row = machine_project(root, root)
            self.assertEqual({k:row["tasks"][k] for k in ["open", "active", "human", "blocked"]}, {"open":2,"active":1,"human":2,"blocked":1})
            self.assertTrue(row["tasks"]["updated_at"])
            self.assertEqual(query.call_args.kwargs["timeout"], 8)
            with patch("moi.bridge.query", side_effect=ValueError("unavailable")):
                row = machine_project(root, root)
            self.assertIsNone(row["tasks"])
            self.assertEqual(row["state"], "connected")
            self.assertIn("无法读取", row["task_error"])
            (root / "keel.toml").write_text("not valid toml")
            self.assertEqual(machine_project(root, root)["state"], "invalid")

    def test_machine_one_failed_project_keeps_other_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good"
            good.mkdir()
            (good / "keel.toml").write_text("skills = []")
            fake = types.ModuleType("keel")
            fake.__version__ = "test"
            with patch.dict(sys.modules, {"keel": fake}), patch("moi.bridge.discover_projects", return_value=([good, root / "gone"], ["index unavailable"])):
                result = machine_usage(root)
            self.assertEqual([r["state"] for r in result["projects"]], ["connected", "unavailable"])
            self.assertEqual(result["version"], "test")
            self.assertTrue(result["warnings"])


if __name__ == "__main__":
    unittest.main()
