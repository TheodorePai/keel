import shutil
import tomllib
from pathlib import Path

from keel import assets
from keel.cli import check, init_project, load_project
from tests.test_check import Base, run


class FakeRemote:
    """替换 HF / GitHub 后端：按 (仓库, 版本) 保存上传内容的快照。"""

    def __init__(self, root):
        self.root, self.n = root / ".remote", 0

    def hf_upload(self, repo_type, repo_id, local, path_in_repo, message):
        self.n += 1
        dest = self.root / repo_id / str(self.n) / path_in_repo
        if local.is_dir():
            shutil.copytree(local, dest)
        else:
            dest.mkdir(parents=True)
            shutil.copyfile(local, dest / local.name)
        return str(self.n)

    def hf_download(self, repo_type, repo_id, revision, sub, dest):
        shutil.copytree(self.root / repo_id / revision / sub, dest / sub)
        return dest / sub

    def hf_remote_files(self, repo_type, repo_id, revision, sub):
        base = self.root / repo_id / revision / sub
        return {f.relative_to(base).as_posix(): ("sha256", assets.sha256_file(f))
                for f in base.rglob("*") if f.is_file()}

    def gh_upload(self, repo, tag, file):
        (self.root / tag).mkdir(parents=True)
        shutil.copyfile(file, self.root / tag / file.name)

    def gh_download(self, repo, tag, filename, dest):
        shutil.copyfile(self.root / tag / filename, dest / filename)
        return dest / filename


class TestAssets(Base):
    def setUp(self):
        super().setUp()
        init_project(self.root)
        self.remote = FakeRemote(Path(self.tmp.name))
        for fn in ("hf_upload", "hf_download", "hf_remote_files", "gh_upload", "gh_download"):
            original = getattr(assets, fn)
            setattr(assets, fn, getattr(self.remote, fn))
            self.addCleanup(setattr, assets, fn, original)
        self.store = assets.Store(self.root, load_project(self.root).config)

    def make_dataset(self, n=3):
        d = self.root / "work/data/screens"
        d.mkdir(parents=True, exist_ok=True)
        for i in range(n):
            (d / f"s{i}.json").write_text(f'{{"i": {i}}}')
        return d

    def test_init_writes_storage_from_personal_config(self):
        storage = tomllib.loads((self.root / "keel.toml").read_text())["storage"]
        slug = assets.project_slug(self.root)
        self.assertEqual(storage["dataset"], f"hf://datasets/me/{slug}-datasets")
        self.assertEqual(storage["deliverable"], f"gh://me/{slug}")

    def test_dataset_is_one_entry_and_round_trips(self):
        d = self.make_dataset()
        assets.add(self.store, "work/data/screens", "dataset", name="screens")
        entry = self.store.load()["screens"]
        self.assertEqual((entry["files"], entry["revision"], entry["kind"]), (3, "1", "dataset"))
        self.assertIn("/work/data/screens/", (self.root / ".gitignore").read_text())
        self.assertEqual(check(load_project(self.root)), [])
        shutil.rmtree(d)
        self.assertEqual([s for _, s, _ in assets.fetch(self.store, [])], ["fetched"])
        self.assertEqual(assets.digest(d)[0], entry["digest"])
        self.assertEqual([s for _, s, _ in assets.fetch(self.store, [])], ["ok"])

    def test_digest_detects_change_and_new_version_updates_entry(self):
        d = self.make_dataset()
        assets.add(self.store, "work/data/screens", "dataset", name="screens")
        first = self.store.load()["screens"]
        (d / "s0.json").write_text("changed")
        (d / "extra.json").write_text("x")
        (d / "s1.json").unlink()
        report = assets.verify(self.store, "screens")
        self.assertIn("不一致", report[0])
        self.assertEqual(sorted(report[1:]), ["  多出 extra.json", "  改动 s0.json", "  缺少 s1.json"])
        assets.add(self.store, "work/data/screens", "dataset", name="screens")
        second = self.store.load()["screens"]
        self.assertNotEqual((first["digest"], first["revision"]), (second["digest"], second["revision"]))

    def test_empty_single_and_multi_file_directories_round_trip(self):
        for count in (0, 1, 3):
            with self.subTest(files=count):
                d = self.root / f"work/data/set-{count}"
                d.mkdir(parents=True)
                for i in range(count):
                    (d / f"{i}.txt").write_text(str(i))
                assets.add(self.store, str(d.relative_to(self.root)), "dataset", name=f"set-{count}")
                entry = self.store.load()[f"set-{count}"]
                self.assertEqual(entry["path_type"], "directory")
                shutil.rmtree(d)
                self.assertEqual(assets.fetch(self.store, [f"set-{count}"])[0][1], "fetched")
                self.assertTrue(d.is_dir())
                self.assertEqual(assets.digest(d)[0], entry["digest"])

    def test_legacy_single_file_and_directory_both_restore(self):
        d = self.make_dataset(1)
        file = self.root / "weight.bin"
        file.write_bytes(b"model")
        assets.add(self.store, "work/data/screens", "dataset", name="screens")
        assets.add(self.store, "weight.bin", "model", name="weight")
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy:
                    entries = self.store.load()
                    for entry in entries.values():
                        entry.pop("path_type")
                    self.store.save(entries)
                shutil.rmtree(d)
                file.unlink()
                self.assertEqual([s for _, s, _ in assets.fetch(self.store, [])], ["fetched", "fetched"])
                self.assertTrue(d.is_dir())
                self.assertEqual(file.read_bytes(), b"model")

    def test_wrong_remote_digest_does_not_replace_local_directory(self):
        d = self.make_dataset(1)
        assets.add(self.store, "work/data/screens", "dataset", name="screens")
        (d / "s0.json").write_text("local changes")
        entry = self.store.load()["screens"]
        remote = self.remote.root / "me" / f"{assets.project_slug(self.root)}-datasets" / entry["revision"] / "screens/s0.json"
        remote.write_text("damaged remote")
        self.assertEqual(assets.fetch(self.store, ["screens"])[0][1], "mismatch")
        self.assertEqual((d / "s0.json").read_text(), "local changes")

    def test_deliverable_and_local_and_generated(self):
        (self.root / "out").mkdir()
        (self.root / "out/deck.pptx").write_bytes(b"pptx")
        (self.root / "out/rec.wav").write_bytes(b"wav")
        (self.root / "out/build.bin").write_bytes(b"bin")
        assets.add(self.store, "out/deck.pptx", "deliverable", name="deck")
        assets.add(self.store, "out/rec.wav", "local", name="rec", shareable=False)
        assets.add(self.store, "out/build.bin", "generated", name="build", command="make build")
        for f in ("deck.pptx", "rec.wav", "build.bin"):
            (self.root / "out" / f).unlink()
        status = {n: s for n, s, _ in assets.fetch(self.store, [])}
        self.assertEqual(status, {"deck": "fetched", "rec": "missing", "build": "todo"})
        self.assertTrue(self.store.load()["deck"]["source"].startswith("gh://me/"))
        self.assertFalse(self.store.load()["rec"]["shareable"])

    def test_refuses_tracked_paths_and_bad_inputs(self):
        self.make_dataset()
        self.commit()
        with self.assertRaises(assets.AssetError):
            assets.add(self.store, "work/data/screens", "dataset")
        run(self.root, "git", "rm", "-rq", "--cached", "work/data/screens")
        with self.assertRaises(assets.AssetError):
            assets.add(self.store, "work/data/screens", "deliverable")
        with self.assertRaises(assets.AssetError):
            assets.add(self.store, "work/data/screens", "public")

    def test_runtime_paths_are_refused_before_upload_or_manifest_changes(self):
        path = self.root / "work/.local/state.bin"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"valuable state")
        for kind in assets.KINDS:
            with self.subTest(kind=kind), self.assertRaisesRegex(assets.AssetError, r"work/\.local/"):
                assets.add(self.store, "work/.local/state.bin", kind,
                           source="https://example.invalid/state", command="make state")
        alias = self.root / "work/state.bin"
        alias.symlink_to(path)
        with self.assertRaises(assets.AssetError):
            assets.add(self.store, "work/state.bin", "local")
        self.assertEqual(self.store.load(), {})
        self.assertEqual(self.remote.n, 0)
        self.assertEqual(path.read_bytes(), b"valuable state")

    def test_legacy_runtime_manifest_is_rejected_without_touching_files(self):
        entry = {"kind": "local", "path": "work/.local/state.bin", "digest": "sha256:old"}
        self.store.save({"state": entry})
        self.assertIn("asset-local", {f.rule for f in check(load_project(self.root))})
        self.assertEqual(assets.fetch(self.store, ["state"])[0][1], "error")
        path = self.root / entry["path"]
        self.assertFalse(path.exists())
        path.parent.mkdir(parents=True)
        path.write_bytes(b"keep")
        self.assertEqual(assets.fetch(self.store, ["state"])[0][1], "error")
        self.assertEqual(path.read_bytes(), b"keep")

    def test_check_flags_tracked_or_unignored_assets(self):
        self.make_dataset()
        assets.add(self.store, "work/data/screens", "dataset", name="screens")
        (self.root / ".gitignore").write_text("")
        self.assertIn("asset-ignore", {f.rule for f in check(load_project(self.root))})
        self.commit()
        self.assertIn("asset-tracked", {f.rule for f in check(load_project(self.root))})

    def test_too_many_files_in_one_folder_is_refused(self):
        old = assets.HF_FOLDER_LIMIT
        assets.HF_FOLDER_LIMIT = 2
        self.addCleanup(setattr, assets, "HF_FOLDER_LIMIT", old)
        self.make_dataset(3)
        with self.assertRaises(assets.AssetError):
            assets.add(self.store, "work/data/screens", "dataset")


class TestOffline(Base):
    def test_doctor_and_whoami_survive_network_failure(self):
        import urllib.request
        def boom(*a, **k):
            raise OSError("network down")
        original = urllib.request.urlopen
        urllib.request.urlopen = boom
        self.addCleanup(setattr, urllib.request, "urlopen", original)
        self.assertIsNone(assets.hf_whoami())
        lines = assets.doctor(self.root, {"storage": {"dataset": "hf://datasets/me/x-datasets"}})
        self.assertTrue(any("无法连接 HF" in line for ok, line in lines if not ok) or
                        any("HF 未登录" in line for _, line in lines))
