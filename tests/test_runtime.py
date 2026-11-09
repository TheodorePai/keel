import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from keel import assets, cli, runtime
from tests.test_check import Base, run


class TestRuntime(Base):
    def old_run(self, name="task/run", managed=True):
        path = self.root / "work/.local" / name
        if managed:
            runtime.transition(self.root, name, "running")
        else:
            path.mkdir(parents=True)
        (path / "log.txt").write_bytes(b"x" * 1024)
        old = time.time() - 60 * 86400
        for child in [*path.rglob("*"), path]:
            os.utime(child, (old, old))
        if managed:
            (path / ".active").unlink()
            runtime.write_json(path / runtime.STATE, {"version": 1, "state": "disposable",
                               "started_at": old - 100, "completed_at": old + 10,
                               "owner": {"pid": 2147483647,
                                         "started": "win32:1" if os.name == "nt" else "fixture-exited"}})
        return path

    def test_status_counts_files_without_following_external_symlinks(self):
        self.assertIn("不存在", runtime.status(self.root)[0])
        path = self.old_run()
        outside = self.root / "private.bin"
        outside.write_bytes(b"x" * 4096)
        (path / "outside").symlink_to(outside)
        usage = runtime.measure(path)
        self.assertEqual(usage.size, 1024 + (path / "outside").lstat().st_size + (path / runtime.STATE).stat().st_size)
        self.assertTrue(any("task" in line for line in runtime.status(self.root)))

    def test_clean_only_previews_by_default_and_applies_to_named_old_content(self):
        path = self.old_run()
        other = self.old_run("other/run")
        self.assertTrue(any("可清理 task/run" in line for line in runtime.clean(self.root, ["task/run"])))
        self.assertTrue(path.exists())
        lines = runtime.clean(self.root, ["task/run"], apply=True)
        self.assertTrue(any("已清理" in line for line in lines))
        self.assertFalse(path.exists())
        self.assertTrue(other.exists())

    def test_recent_nested_output_protects_an_old_directory(self):
        path = self.old_run()
        (path / "log.txt").write_text("recent output")
        lines = runtime.clean(self.root, ["task/run"], apply=True)
        self.assertTrue(any("最近" in line for line in lines))
        self.assertTrue(path.exists())

    def test_keep_and_active_markers_protect_runs_and_parent_tasks(self):
        for marker in runtime.MARKERS:
            for parent in (False, True):
                with self.subTest(marker=marker, parent=parent):
                    path = self.old_run(f"{marker[1:]}-{parent}/run")
                    where = path.parent if parent else path
                    (where / marker).touch()
                    lines = runtime.clean(self.root, [path.relative_to(self.root / "work/.local").as_posix()],
                                          days=0, apply=True)
                    self.assertTrue(any("标记" in line for line in lines))
                    self.assertTrue(path.exists())

    def test_clean_refuses_unsafe_or_overlapping_paths_before_removing_anything(self):
        path = self.old_run()
        for names in (["."], ["../outside"], [str(path)], ["task/run", "missing"],
                      ["task", "task/run"]):
            with self.subTest(names=names), self.assertRaises(runtime.LocalError):
                runtime.clean(self.root, names, days=0, apply=True)
            self.assertTrue(path.exists())
        with self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["task/run"], days=-1, apply=True)

    def test_clean_refuses_symlink_targets_and_does_not_follow_nested_links(self):
        path = self.old_run()
        outside = self.root / "outside"
        outside.mkdir()
        valuable = outside / "keep.txt"
        valuable.write_text("keep")
        base = self.root / "work/.local"
        (base / "alias").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["alias/keep.txt"], days=0, apply=True)
        (path / "outside").symlink_to(outside, target_is_directory=True)
        # Complete after adding the link, then age the run without depending on
        # os.utime(follow_symlinks=False), unavailable on Windows Python 3.11/3.12.
        completed = max(time.time(), runtime.measure(path).latest) + 1
        data = runtime.read_state(path)
        data["completed_at"] = completed
        runtime.write_json(path / runtime.STATE, data)
        with patch.object(time, "time", return_value=completed + 2 * 86400):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertFalse(path.exists())
        self.assertEqual(valuable.read_text(), "keep")

    def test_registered_assets_and_tracked_files_are_retained_until_migrated(self):
        path = self.old_run()
        store = assets.Store(self.root, {})
        store.save({"legacy": {"kind": "local", "path": "work/.local/task/run/log.txt"}})
        self.assertTrue(any("登记资产" in line for line in runtime.clean(self.root, ["task"], days=0, apply=True)))
        self.assertTrue(path.exists())
        store.save({})
        run(self.root, "git", "add", "-f", "work/.local/task/run/log.txt")
        self.assertTrue(any("Git 跟踪" in line for line in runtime.clean(self.root, ["task/run"], days=0, apply=True)))
        self.assertTrue(path.exists())

    def test_invalid_asset_manifest_stops_cleanup(self):
        path = self.old_run()
        (self.root / "assets.toml").write_text("not [valid")
        with self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertTrue(path.exists())

    def test_cli_exposes_status_preview_and_explicit_apply(self):
        path = self.old_run()
        with patch("os.getcwd", return_value=str(self.root)), contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["local", "status"]), 0)
            self.assertEqual(cli.main(["local", "clean", "task/run"]), 0)
            self.assertTrue(path.exists())
            self.assertIn("仅预览", output.getvalue())
            self.assertEqual(cli.main(["local", "clean", "task/run", "--apply"]), 0)
        self.assertFalse(path.exists())

    def test_state_transitions_and_completion_retention(self):
        runtime.transition(self.root, "new/run", "running")
        path = self.root / "work/.local/new/run"
        with self.assertRaises(runtime.LocalError):
            runtime.transition(self.root, "new/run", "disposable")
        with patch.object(runtime, "process_status", return_value="exited"):
            runtime.transition(self.root, "new/run", "completed")
        completed = runtime.read_state(path)["completed_at"]
        runtime.transition(self.root, "new/run", "retained", reason="review", review_on="2099-01-01")
        runtime.transition(self.root, "new/run", "disposable")
        self.assertEqual(runtime.read_state(path)["completed_at"], completed)
        self.assertIn("尚未满", " ".join(runtime.clean(self.root, ["new/run"], apply=True)))
        self.assertTrue(path.exists())
        with self.assertRaises(FileExistsError):
            runtime.transition(self.root, "new/run", "running")

    def test_legacy_unknown_and_corrupt_state_are_never_cleaned(self):
        path = self.old_run(managed=False)
        for text in (None, "{", '{"version": 1, "state": "disposable"}'):
            if text is not None:
                (path / runtime.STATE).write_text(text)
            self.assertIn("状态不明", " ".join(runtime.clean(self.root, ["task/run"], days=0, apply=True)))
            self.assertTrue(path.exists())

    def test_unclassified_file_is_retained_by_explicit_and_automatic_cleanup(self):
        base = self.root / "work/.local"
        base.mkdir(parents=True)
        path = base / "legacy.log"
        path.write_text("keep")
        self.assertIn("状态不明", " ".join(runtime.clean(self.root, ["legacy.log"], days=0, apply=True)))
        self.assertIn("状态不明", " ".join(runtime.sweep(self.root, {})))
        self.assertEqual(path.read_text(), "keep")

    def test_completion_time_not_mtime_controls_retention(self):
        path = self.old_run()
        data = runtime.read_state(path)
        data["completed_at"] = time.time() - 86400
        runtime.write_json(path / runtime.STATE, data)
        self.assertIn("尚未满", " ".join(runtime.clean(self.root, ["task/run"], apply=True)))

    def test_active_identity_exit_reuse_and_unknown(self):
        original = "win32:1" if os.name == "nt" else "original"
        reused = "win32:2" if os.name == "nt" else "reused"
        owner = {"pid": 123, "started": original}
        with patch.object(runtime, "process_identity", return_value=original):
            self.assertEqual(runtime.process_status(owner), "alive")
        with patch.object(runtime, "process_identity", return_value=reused):
            self.assertEqual(runtime.process_status(owner), "exited")
        with patch.object(runtime, "process_identity", return_value=None):
            with patch.object(runtime, "process_exited", return_value=True):
                self.assertEqual(runtime.process_status(owner), "exited")
            with patch.object(runtime, "process_exited", return_value=None):
                self.assertEqual(runtime.process_status(owner), "unknown")
        self.assertEqual(runtime.process_status({"pid": -1}), "unknown")

    def test_stale_active_only_warns_and_explicit_completion_resolves(self):
        runtime.transition(self.root, "active/run", "running")
        path = self.root / "work/.local/active/run"
        with self.assertRaisesRegex(runtime.LocalError, "先确认"):
            runtime.transition(self.root, "active/run", "completed")
        with patch.object(runtime, "process_status", return_value="exited"):
            self.assertIn("状态不明", " ".join(runtime.clean(self.root, ["active/run"], days=0, apply=True)))
            self.assertTrue(path.exists())
            runtime.transition(self.root, "active/run", "completed")
        self.assertFalse((path / ".active").exists())
        self.assertEqual(runtime.read_state(path)["state"], "completed")

    def test_keep_requires_reason_and_review_date_and_warns_without_deleting(self):
        path = self.old_run()
        for reason, review in ((None, None), ("", "2099-01-01"), ("review", "bad")):
            with self.assertRaises(runtime.LocalError):
                runtime.transition(self.root, "task/run", "retained", reason=reason, review_on=review)
        runtime.transition(self.root, "task/run", "retained", reason="等待评审", review_on="2000-01-01")
        self.assertIn("复查已到期", " ".join(runtime.status(self.root)))
        self.assertIn("等待评审", " ".join(runtime.clean(self.root, ["task/run"], days=0, apply=True)))
        self.assertTrue(path.exists())
        runtime.transition(self.root, "task/run", "disposable")
        self.assertFalse((path / ".keep").exists())

    def test_legacy_keep_remains_protected_with_repair_notice(self):
        path = self.old_run()
        (path.parent / ".keep").touch()
        self.assertIn("需补齐", " ".join(runtime.status(self.root)))
        self.assertIn("继续保留", " ".join(runtime.clean(self.root, ["task/run"], days=0, apply=True)))
        self.assertTrue(path.exists())

    def test_daily_sweep_only_once_and_next_day_runs_again(self):
        first = self.old_run()
        self.assertIn("已清理", " ".join(runtime.sweep(self.root, {}, daily=True)))
        self.assertFalse(first.exists())
        second = self.old_run("second/run")
        self.assertEqual(runtime.sweep(self.root, {}, daily=True), [])
        self.assertTrue(second.exists())
        with patch.object(time, "time", return_value=time.time() + 86400):
            self.assertIn("已清理", " ".join(runtime.sweep(self.root, {}, daily=True)))
        self.assertFalse(second.exists())

    def test_budget_only_reclaims_expired_disposable_runs(self):
        expired = self.old_run()
        legacy = self.old_run("legacy/run", managed=False)
        retained = self.old_run("retained/run")
        runtime.transition(self.root, "retained/run", "retained", reason="复核", review_on="2000-01-01")
        recent = self.old_run("recent/run")
        data = runtime.read_state(recent)
        data["completed_at"] = time.time()
        runtime.write_json(recent / runtime.STATE, data)
        lines = runtime.sweep(self.root, {"local": {"budget_mb": 0.001}})
        self.assertFalse(expired.exists())
        self.assertTrue(all(path.exists() for path in (legacy, retained, recent)))
        self.assertIn("超出容量预算", " ".join(lines))
        self.assertIn("状态不明", " ".join(lines))
        self.assertIn("复查已到期", " ".join(lines))

    def test_policy_validation_fails_before_any_deletion(self):
        path = self.old_run()
        for local in ([], {"retention_days": True}, {"retention_days": -1}, {"budget_mb": 0},
                      {"budget_mb": float("inf")}, {"typo": 1}):
            with self.subTest(local=local), self.assertRaises(runtime.LocalError):
                runtime.sweep(self.root, {"local": local})
            self.assertTrue(path.exists())

    def test_lock_and_management_symlinks_prevent_cleanup(self):
        path = self.old_run()
        with runtime.local_lock(self.root), self.assertRaises(runtime.LocalError):
            runtime.sweep(self.root, {})
        self.assertTrue(path.exists())
        metadata = runtime.metadata_dir(self.root)
        (metadata / "daily.json").symlink_to(path / runtime.STATE)
        with self.assertRaises(runtime.LocalError):
            runtime.sweep(self.root, {})
        self.assertTrue(path.exists())

    def test_sweep_preserves_parent_markers_assets_tracked_and_recent_writes(self):
        path = self.old_run()
        (path.parent / ".keep").touch()
        tracked = self.old_run("tracked/run")
        run(self.root, "git", "add", "-f", "work/.local/tracked/run/log.txt")
        recent = self.old_run("recent/run")
        (recent / "log.txt").write_text("changed")
        registered = self.old_run("registered/run")
        assets.Store(self.root, {}).save({"asset": {"kind": "local", "path": "work/.local/registered/run/log.txt"}})
        runtime.sweep(self.root, {})
        self.assertTrue(all(p.exists() for p in (path, tracked, recent, registered)))

    def test_working_check_triggers_daily_maintenance_but_staged_check_does_not(self):
        self.old_run()
        with patch("os.getcwd", return_value=str(self.root)), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), patch.object(runtime, "sweep", return_value=[]) as sweep:
            cli.main(["check"])
            sweep.assert_called_once_with(self.root.resolve(), cli.load_project(self.root).config, daily=True)
            sweep.reset_mock()
            cli.main(["check", "--staged"])
            sweep.assert_not_called()

    def test_start_rule_installs_and_migrates_without_duplicates(self):
        original = (self.root / "AGENTS.md").read_text()
        cli.install_agents_pointer(self.root)
        cli.install_agents_pointer(self.root)
        text = (self.root / "AGENTS.md").read_text()
        self.assertIn(original.strip(), text)
        self.assertEqual(text.count(cli.LOCAL_START), 1)

    def test_cleanup_audit_records_deleted_bytes_and_retained_reason(self):
        expired = self.old_run()
        expected = runtime.measure(expired).size
        legacy = self.old_run("legacy/run", managed=False)
        runtime.sweep(self.root, {})
        audit = self.root / "work/.local/.keel/cleanup.jsonl"
        events = [json.loads(line) for line in audit.read_text().splitlines()]
        deleted = [event for event in events if event["event"] == "deleted"]
        self.assertEqual([(event["path"], event["file_bytes"]) for event in deleted], [("task/run", expected)])
        self.assertTrue(any(event["event"] == "delete_started" for event in events))
        self.assertTrue(any(event["event"] == "retained" and "状态不明" in event["reason"] for event in events))
        self.assertTrue(legacy.exists())
        self.assertEqual(events[-1]["event"], "sweep_finished")

    def test_audit_is_bounded_and_preserves_latest_events(self):
        self.old_run()
        with runtime.local_lock(self.root):
            audit = runtime.Audit(self.root)
            for i in range(250):
                audit.add("retained", str(i), reason="检查" * 1000)
        path = self.root / "work/.local/.keel/cleanup.jsonl"
        self.assertLessEqual(path.stat().st_size, runtime.AUDIT_BYTES)
        entries = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertLessEqual(len(entries), runtime.AUDIT_ENTRIES)
        self.assertEqual(entries[-1]["path"], "249")
        self.assertEqual(len(list(path.parent.glob("cleanup*"))), 1)

    def test_unwritable_corrupt_and_symlink_audit_stop_before_deletion(self):
        path = self.old_run()
        audit = runtime.metadata_dir(self.root) / "cleanup.jsonl"
        with patch.object(runtime, "write_bytes", side_effect=OSError("disk full")), self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertTrue(path.exists())
        audit.write_text("corrupt")
        with self.assertRaises(runtime.LocalError):
            runtime.sweep(self.root, {})
        self.assertTrue(path.exists())
        audit.unlink()
        outside = self.root / "external-log"
        outside.write_text("keep")
        audit.symlink_to(outside)
        with self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertTrue(path.exists())
        self.assertEqual(outside.read_text(), "keep")

    def test_sweep_reports_unknown_sibling_under_a_managed_task(self):
        self.old_run()
        legacy = self.old_run("task/legacy", managed=False)
        lines = runtime.sweep(self.root, {})
        self.assertIn("保留 task/legacy：状态不明", " ".join(lines))
        self.assertTrue(legacy.exists())

    def test_recheck_new_ancestor_keep_prevents_delete(self):
        path = self.old_run()
        original = runtime.Audit.add
        def add_marker(audit, event, *args, **kwargs):
            original(audit, event, *args, **kwargs)
            if event == "delete_started":
                (path.parent / ".keep").touch()
        with patch.object(runtime.Audit, "add", add_marker):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertTrue(path.exists())

    def test_failed_sweep_does_not_consume_daily_trigger(self):
        path = self.old_run()
        with patch.object(runtime, "retained_paths", side_effect=runtime.LocalError("git unavailable")):
            with self.assertRaises(runtime.LocalError):
                runtime.sweep(self.root, {}, daily=True)
        self.assertFalse((runtime.metadata_dir(self.root) / "daily.json").exists())
        runtime.sweep(self.root, {}, daily=True)
        self.assertFalse(path.exists())

    def test_invalid_clock_or_nested_state_is_never_auto_deleted(self):
        path = self.old_run()
        nested = path / "other"
        nested.mkdir()
        runtime.write_json(nested / runtime.STATE, runtime.read_state(path))
        self.assertIn("其他运行记录", " ".join(runtime.sweep(self.root, {})))
        self.assertTrue(path.exists())
        runtime.write_json(runtime.metadata_dir(self.root) / "daily.json", {"checked_at": time.time() + 100})
        with self.assertRaisesRegex(runtime.LocalError, "时钟回退"):
            runtime.sweep(self.root, {}, daily=True)
        self.assertTrue(path.exists())

    def test_partial_delete_is_logged_without_claiming_freed_bytes(self):
        path = self.old_run()
        with patch.object(runtime, "remove_tree", side_effect=OSError("interrupted")), self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["task/run"], apply=True)
        events = [json.loads(line) for line in (runtime.metadata_dir(self.root) / "cleanup.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["event"], "delete_incomplete")
        self.assertEqual(events[-1]["file_bytes"], 0)
        self.assertFalse(any(event["event"] == "deleted" for event in events))
        self.assertTrue(path.exists())

    def test_recent_symlink_and_oversized_state_are_protected(self):
        path = self.old_run()
        outside = self.root / "outside"
        outside.mkdir()
        (path / "link").symlink_to(outside, target_is_directory=True)
        self.assertIn("最近", " ".join(runtime.clean(self.root, ["task/run"], apply=True)))
        self.assertTrue(path.exists())
        (path / runtime.STATE).write_text(" " * 65537)
        self.assertIn("状态不明", " ".join(runtime.sweep(self.root, {})))
        self.assertTrue(path.exists())

    def test_interrupted_log_write_uses_one_pending_file_and_blocks_delete(self):
        path = self.old_run()
        pending = runtime.metadata_dir(self.root) / ".cleanup.jsonl.pending"
        pending.write_text("interrupted write")
        for _ in range(2):
            with self.assertRaisesRegex(runtime.LocalError, "未完成"):
                runtime.clean(self.root, ["task/run"], apply=True)
        self.assertTrue(path.exists())
        self.assertEqual(pending.read_text(), "interrupted write")
        self.assertEqual(len(list(pending.parent.glob("*.pending"))), 1)

    def child(self, code):
        return subprocess.run([sys.executable, "-c", code, str(self.root)],
                              cwd=self.root, capture_output=True, text=True, timeout=15,
                              env={**os.environ, "PYTHONPATH": str(Path(runtime.__file__).parent.parent),
                                   "PYTHONDONTWRITEBYTECODE": "1"})

    def redirect_directory(self, link, target):
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True, timeout=10)
        else:
            link.symlink_to(target, target_is_directory=True)

    def test_process_queries_leave_a_real_producer_alive_and_detect_its_exit(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            identity = runtime.process_identity(child.pid)
            self.assertIsNotNone(identity)
            owner = {"pid": child.pid, "started": identity}
            # On Windows os.kill(pid, 0) terminates the process; never use it.
            with patch.object(runtime.os, "kill", side_effect=AssertionError("must not signal")):
                self.assertEqual(runtime.process_status(owner), "alive")
                if os.name == "nt":
                    with patch.object(runtime, "process_identity", return_value=None):
                        self.assertEqual(runtime.process_status(owner), "unknown")
            self.assertIsNone(child.poll())
        finally:
            child.terminate()
            child.wait(timeout=10)
        self.assertEqual(runtime.process_status(owner), "exited")

    def test_foreign_process_identity_is_unknown_even_if_pid_matches(self):
        identity = "POSIX process time" if os.name == "nt" else "win32:123"
        with patch.object(runtime, "process_identity", return_value="different") as query:
            self.assertEqual(runtime.process_status({"pid": os.getpid(), "started": identity}), "unknown")
        query.assert_not_called()

    def test_lock_contends_between_processes_and_releases_after_process_exit(self):
        probe = """
import sys
from pathlib import Path
from keel import runtime
try:
    with runtime.local_lock(Path(sys.argv[1])):
        print('acquired')
except runtime.LocalError:
    print('blocked')
"""
        with runtime.local_lock(self.root):
            result = self.child(probe)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "blocked")
        result = self.child(probe)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "acquired")
        crashed = self.child("""
import os, sys
from pathlib import Path
from keel import runtime
with runtime.local_lock(Path(sys.argv[1])):
    os._exit(23)
""")
        self.assertEqual(crashed.returncode, 23, crashed.stderr)
        with runtime.local_lock(self.root):
            pass

    def test_hardlinked_lock_is_rejected_without_changing_external_file(self):
        metadata = runtime.metadata_dir(self.root)
        outside = self.root / "important"
        outside.write_bytes(b"keep")
        os.link(outside, metadata / "lock")
        with self.assertRaises(runtime.LocalError):
            with runtime.local_lock(self.root):
                self.fail("hardlinked lock was accepted")
        self.assertEqual(outside.read_bytes(), b"keep")

    def test_redirected_management_directory_is_not_used(self):
        base = self.root / "work/.local"
        base.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        self.redirect_directory(base / ".keel", outside)
        with self.assertRaises(runtime.LocalError):
            runtime.transition(self.root, "task/run", "running")
        self.assertEqual(list(outside.iterdir()), [])

    def test_junction_or_directory_link_target_is_rejected_and_nested_link_is_unlinked(self):
        path = self.old_run()
        base = self.root / "work/.local"
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "valuable").write_text("keep")
        self.redirect_directory(base / "alias", outside)
        self.redirect_directory(path / "nested", outside)
        with self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["alias"], days=0, apply=True)
        self.assertNotIn("alias", runtime.candidates(base))
        self.assertNotIn("nested", " ".join(runtime.notices(base)))
        data = runtime.read_state(path)
        data["completed_at"] = max(time.time(), runtime.measure(path).latest) + 1
        runtime.write_json(path / runtime.STATE, data)
        with patch.object(time, "time", return_value=data["completed_at"] + 2 * 86400):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertFalse(path.exists())
        self.assertEqual((outside / "valuable").read_text(), "keep")

    def test_replaced_cleanup_root_is_not_deleted(self):
        path = self.old_run()
        original = runtime.remove_tree
        def replace_before_delete(target, identity):
            target.rename(target.with_name("saved"))
            target.mkdir()
            (target / "valuable").write_text("keep")
            original(target, identity)
        with patch.object(runtime, "remove_tree", replace_before_delete), self.assertRaises(runtime.LocalError):
            runtime.clean(self.root, ["task/run"], days=0, apply=True)
        self.assertEqual((path / "valuable").read_text(), "keep")
        self.assertTrue(path.with_name("saved").exists())

    def test_failed_atomic_replace_preserves_previous_record(self):
        path = self.root / "record.json"
        runtime.write_json(path, {"version": 1})
        with patch.object(runtime.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                runtime.write_json(path, {"version": 2})
        self.assertEqual(runtime.read_json(path), {"version": 1})
        self.assertFalse(path.with_name(".record.json.pending").exists())

    def test_platform_path_rules_preserve_native_filename_semantics(self):
        for name in ("../outside", ".keel/run", "/absolute"):
            with self.subTest(name=name), self.assertRaises(runtime.LocalError):
                runtime.relative_path(name)
        windows_names = ("C:relative", "C:/absolute", "//server/share/run", "run:stream",
                         ".KEEL/run", "CON/run", "task/NUL.txt", "task./run", "task /run")
        for name in windows_names:
            with self.subTest(name=name):
                if os.name == "nt":
                    with self.assertRaises(runtime.LocalError):
                        runtime.relative_path(name)
                elif not name.startswith("/"):
                    self.assertEqual(runtime.relative_path(name), Path(name))


if os.name == "nt":
    class TestWindowsRuntime(Base):
        def test_guard_pins_ancestors_until_all_handles_are_closed(self):
            parent = self.root / "parent"
            child = parent / "child"
            child.mkdir(parents=True)
            moved = self.root / "moved"
            with runtime.directory_guard(child):
                with self.assertRaises(OSError):
                    parent.rename(moved)
            parent.rename(moved)
            self.assertTrue((moved / "child").is_dir())

        def test_denied_process_query_preserves_unknown_and_never_signals(self):
            windows = runtime._windows
            with patch.object(windows, "_open_process", return_value=None), \
                    patch.object(windows.C, "get_last_error", return_value=5), \
                    patch.object(runtime.os, "kill", side_effect=AssertionError("must not signal")):
                self.assertIsNone(runtime.process_identity(123))
                self.assertEqual(runtime.process_status({"pid": 123, "started": "win32:1"}), "unknown")

        def test_network_path_is_rejected_before_opening_a_handle(self):
            with patch.object(runtime._windows, "_create") as create:
                with self.assertRaises(OSError):
                    with runtime.directory_guard(Path(r"\\server\share\run")):
                        self.fail("network path accepted")
            create.assert_not_called()
