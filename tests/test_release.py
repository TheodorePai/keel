"""Release boundaries, version selection, snapshot identity and distribution checks."""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / ".github/scripts/release.py"
spec = importlib.util.spec_from_file_location("release", SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class TestVersionSelection(unittest.TestCase):
    def test_fixes_and_maintenance_use_one_patch(self):
        prs = [{"title": "fix: Windows paths"}, {"title": "ci: add gates"}]
        self.assertEqual(release.next_version("v0.5.3", prs), "v0.5.4")

    def test_features_and_explicit_minor_label(self):
        self.assertEqual(release.next_version("v0.5.3", [{"title": "feat(cli): export"}]), "v0.6.0")
        self.assertEqual(release.next_version("v1.2.3", [{"title": "update", "labels": [{"name": "release:minor"}]}]), "v1.3.0")

    def test_breaking_changes_require_explicit_version(self):
        prs = [{"title": "feat!: replace format"}]
        with self.assertRaisesRegex(ValueError, "Breaking"):
            release.next_version("v0.5.3", prs)
        self.assertEqual(release.next_version("v0.5.3", prs, "0.6.0"), "v0.6.0")

    def test_invalid_or_non_increasing_override_is_rejected(self):
        for version in ("v0.5.3", "v0.5.2", "../main", "0.6.0rc1", "01.2.3"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                release.next_version("v0.5.3", [], version)


class TestReleaseGit(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.git("config", "user.name", "omoyx")
        self.git("config", "user.email", release.IDENTITY)
        (self.repo / "keel").mkdir()
        (self.repo / "keel/__init__.py").write_text('__version__ = "0.5.3"\n')
        (self.repo / "README.md").write_text("Public source\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.git("tag", "v0.5.3")

    def git(self, *args):
        return release.git(*args, cwd=self.repo)

    def in_repo(self, function, *args):
        original = Path.cwd()
        try:
            os.chdir(self.repo)
            return function(*args)
        finally:
            os.chdir(original)

    def snapshot(self):
        return self.in_repo(release.candidate, self.base, "v0.5.4", self.root / "candidate")

    def test_snapshot_is_deterministic_public_source_plus_version(self):
        first = self.snapshot()
        self.assertEqual(first, self.snapshot())
        self.assertEqual(self.git("show", "-s", "--format=%P", first), self.base)
        self.assertEqual(self.git("show", "-s", "--format=%B", first), "keel v0.5.4")
        self.assertEqual(self.git("diff", "--name-only", self.base, first), "keel/__init__.py")
        self.assertEqual(self.git("show", f"{first}:keel/__init__.py"), '__version__ = "0.5.4"')
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_tag_only_snapshot_maps_back_to_its_public_source(self):
        sha = self.snapshot()
        self.git("tag", "v0.5.4", sha)
        metadata = {"tag_name": "v0.5.4", "body": f"<!-- keel-release-source: {self.base} -->"}
        self.assertEqual(self.in_repo(release.release_base, metadata, self.base), self.base)
        self.assertEqual(self.in_repo(release.release_base, {"tag_name": "v0.5.3"}, self.base), self.base)

    def test_forged_release_source_is_rejected(self):
        sha = self.snapshot()
        self.git("tag", "v0.5.4", sha)
        metadata = {"tag_name": "v0.5.4", "body": f"<!-- keel-release-source: {sha} -->"}
        with self.assertRaisesRegex(ValueError, "metadata"):
            self.in_repo(release.release_base, metadata, self.base)

    def test_unreleased_scan_includes_old_merges_but_excludes_closed_prs(self):
        (self.repo / "README.md").write_text("A merged fix\n")
        self.git("commit", "-qam", "fix: accepted")
        merged = self.git("rev-parse", "HEAD")
        base = {"ref": "main", "repo": {"full_name": release.REPOSITORY}}
        prs = [
            {"number": 1, "title": "fix: accepted", "merged_at": "2020-01-01", "merge_commit_sha": merged,
             "base": base, "html_url": "https://github.com/omoyx/keel/pull/1", "labels": []},
            {"number": 2, "title": "closed", "merged_at": None, "merge_commit_sha": merged, "base": base},
        ]
        with patch.object(release, "api", return_value=prs):
            result = self.in_repo(release.merged_prs, self.base, merged, release.REPOSITORY)
        self.assertEqual([pr["number"] for pr in result], [1])
        with patch.object(release, "api") as api:
            self.assertEqual(self.in_repo(release.merged_prs, merged, merged, release.REPOSITORY), [])
            api.assert_not_called()

    def test_daily_no_change_does_not_create_a_candidate_or_push(self):
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        real_git = release.git
        def local_git(*args, **kwargs):
            return "" if args[0] == "fetch" else real_git(*args, **kwargs)
        with patch.dict(os.environ, GITHUB_REPOSITORY=release.REPOSITORY), \
             patch.object(release, "git", side_effect=local_git), \
             patch.object(release, "api", return_value={"tag_name": "v0.5.3"}), \
             patch.object(release, "candidate") as candidate, \
             patch.object(release, "push") as push:
            self.assertEqual(self.in_repo(release.plan_release), {"ready": False})
            candidate.assert_not_called()
            push.assert_not_called()


class TestReleaseAssets(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def assets(self, version="0.5.4"):
        with zipfile.ZipFile(self.root / "keel-0.5.4-py3-none-any.whl", "w") as wheel:
            wheel.writestr("keel-0.5.4.dist-info/METADATA", f"Name: keel\nVersion: {version}\n")
        content = f'__version__ = "{version}"\n'.encode()
        with tarfile.open(self.root / "keel-0.5.4.tar.gz", "w:gz") as archive:
            info = tarfile.TarInfo("keel-0.5.4/keel/__init__.py")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    def test_tested_distributions_are_versioned_and_checksummed(self):
        self.assets()
        files = release.validate_distributions(self.root, "v0.5.4")
        self.assertEqual(len(files), 3)
        for line in (self.root / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((self.root / name).read_bytes()).hexdigest())

    def test_mismatched_version_cannot_be_published(self):
        self.assets("0.5.3")
        with self.assertRaisesRegex(ValueError, "version"):
            release.validate_distributions(self.root, "v0.5.4")

    def test_incomplete_or_multiple_distributions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            release.validate_distributions(self.root, "v0.5.4")
        self.assets()
        (self.root / "another.whl").write_bytes(b"not a tested wheel")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            release.validate_distributions(self.root, "v0.5.4")

    def test_retry_of_published_release_verifies_assets_without_writes(self):
        self.assets()
        files = release.validate_distributions(self.root, "v0.5.4")
        sha = "a" * 40
        plan = {"repo": release.REPOSITORY, "tag": "v0.5.4", "candidate": sha}
        published = {"draft": False, "html_url": "https://github.com/omoyx/keel/releases/tag/v0.5.4",
                     "assets": [{"name": path.name, "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()} for path in files]}
        with patch.dict(os.environ, GITHUB_REPOSITORY=release.REPOSITORY), \
             patch.object(release, "api", return_value=published), \
             patch.object(release, "git", return_value=f"{sha}\trefs/tags/v0.5.4"), \
             patch.object(release, "push") as push, \
             patch.object(release, "command") as command:
            self.assertEqual(release.publish(plan, self.root), published["html_url"])
            push.assert_not_called()
            command.assert_not_called()
            published["assets"][0]["digest"] = "sha256:" + "0" * 64
            with self.assertRaisesRegex(ValueError, "assets differ"):
                release.publish(plan, self.root)


if __name__ == "__main__":
    unittest.main()
