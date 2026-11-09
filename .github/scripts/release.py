"""Plan and publish releases from reviewed public commits using only GITHUB_TOKEN."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import zipfile


REPOSITORY = "omoyx/keel"
IDENTITY = "191834765+omoyx@users.noreply.github.com"
MARKER = re.compile(r"<!-- keel-release-source: ([0-9a-f]{40}) -->")
VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


def command(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, env=env, text=True).strip()


def git(*args: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return command("git", *args, cwd=cwd, env=env)


def api(path: str, optional: bool = False):
    result = subprocess.run(
        ["gh", "api", path], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode:
        if optional and "(HTTP 404)" in result.stderr:
            return None
        raise RuntimeError(f"GitHub API failed for {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def tagged_release(tag: str):
    published = api(f"repos/{REPOSITORY}/releases/tags/{tag}", optional=True)
    if published:
        return published
    # GitHub's by-tag endpoint can hide drafts even from their creator. The
    # authenticated release list includes them and provides the stable API ID.
    page = 1
    while True:
        releases = api(f"repos/{REPOSITORY}/releases?per_page=100&page={page}")
        for release in releases:
            if release["tag_name"] == tag:
                return release
        if len(releases) < 100:
            return None
        page += 1


def version_tuple(value: str) -> tuple[int, int, int]:
    if not VERSION.fullmatch(value):
        raise ValueError(f"Expected a stable semantic version, got {value!r}")
    return tuple(map(int, value.removeprefix("v").split(".")))


def next_version(previous: str, prs: list[dict], override: str = "") -> str:
    before = version_tuple(previous)
    if override:
        after = version_tuple(override)
        if after <= before:
            raise ValueError("The release version must increase")
    else:
        titles = [pr["title"] for pr in prs]
        labels = {label["name"] for pr in prs for label in pr.get("labels", [])}
        if labels & {"breaking-change", "release:major"} or any(
            re.match(r"[a-z]+(?:\([^)]*\))?!:", title) for title in titles
        ):
            raise ValueError("Breaking changes need an explicit workflow version")
        major, minor, patch = before
        if "release:minor" in labels or any(
            re.match(r"feat(?:\([^)]*\))?:", title) for title in titles
        ):
            after = major, minor + 1, 0
        else:
            after = major, minor, patch + 1
    return "v" + ".".join(map(str, after))


def is_ancestor(ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant], check=False
    )
    if result.returncode not in (0, 1):
        raise RuntimeError("Cannot compare the release commit graph")
    return result.returncode == 0


def release_base(release: dict, tip: str) -> str:
    tag = release["tag_name"]
    version_tuple(tag)
    target = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    marker = MARKER.search(release.get("body") or "")
    if marker:
        source = marker[1]
        parents = git("show", "-s", "--format=%P", target).split()
        if parents != [source] or git("show", "-s", "--format=%B", target) != f"keel {tag}":
            raise ValueError("Release source metadata does not match its snapshot")
        changed = git("diff", "--name-only", source, target).splitlines()
        if set(changed) - {"keel/__init__.py"}:
            raise ValueError("Release snapshot changed files other than its version")
        package = git("show", f"{target}:keel/__init__.py")
        if f'__version__ = "{tag.removeprefix("v")}"' not in package:
            raise ValueError("Release snapshot package version differs from its tag")
        target = source
    if not is_ancestor(target, tip):
        raise ValueError("Latest release source is not an ancestor of public main")
    return target


def merged_prs(base: str, tip: str, repo: str) -> list[dict]:
    prs = {}
    for sha in git("rev-list", "--first-parent", "--reverse", f"{base}..{tip}").splitlines():
        for pr in api(f"repos/{repo}/commits/{sha}/pulls?per_page=100"):
            if (
                pr.get("merged_at")
                and pr.get("merge_commit_sha") == sha
                and pr["base"]["ref"] == "main"
                and pr["base"]["repo"]["full_name"] == repo
            ):
                prs[pr["number"]] = {
                    key: pr[key] for key in ("number", "title", "html_url", "labels")
                }
    return list(prs.values())


def push(*refs: str) -> None:
    subprocess.run(
        ["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
         "push", "origin", *refs], check=True
    )


def candidate(source: str, tag: str, directory: Path) -> str:
    git("worktree", "add", "--detach", str(directory), source)
    try:
        init = directory / "keel/__init__.py"
        content, count = re.subn(
            r'(?m)^__version__ = "[^"]+"$',
            f'__version__ = "{tag.removeprefix("v")}"', init.read_text(),
        )
        if count != 1:
            raise ValueError("Cannot locate exactly one package version")
        init.write_text(content)
        git("add", "keel/__init__.py", cwd=directory)
        tree = git("write-tree", cwd=directory)
        timestamp = git("show", "-s", "--format=%ct", source)
        identity = dict(os.environ, GIT_AUTHOR_NAME="omoyx", GIT_COMMITTER_NAME="omoyx",
                        GIT_AUTHOR_EMAIL=IDENTITY, GIT_COMMITTER_EMAIL=IDENTITY,
                        GIT_AUTHOR_DATE=f"@{timestamp} +0000",
                        GIT_COMMITTER_DATE=f"@{timestamp} +0000")
        return git("commit-tree", tree, "-p", source, "-m", f"keel {tag}", env=identity)
    finally:
        git("worktree", "remove", "--force", str(directory))


def output(plan: dict) -> None:
    text = json.dumps(plan, separators=(",", ":"))
    print(text)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"ready={str(plan['ready']).lower()}\nplan={text}\n")
            stream.write(f"candidate={plan.get('candidate', '')}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(f"Release: {plan.get('tag', 'no unreleased merged PRs')}\n")


def plan_release(override: str = "") -> dict:
    repo = os.environ["GITHUB_REPOSITORY"]
    if repo != REPOSITORY:
        raise ValueError("Automatic releases only run in the public repository")
    git("fetch", "origin", "main", "--tags")
    source = git("rev-parse", "origin/main")
    release = api(f"repos/{repo}/releases/latest")
    base = release_base(release, source)
    prs = merged_prs(base, source, repo)
    if not prs:
        return {"ready": False}
    tag = next_version(release["tag_name"], prs, override)
    package = git("show", f"{source}:keel/__init__.py")
    package_version = re.search(r'(?m)^__version__ = "([^"]+)"$', package)
    if not package_version:
        raise ValueError("Cannot read the reviewed package version")
    if not override and version_tuple(package_version[1]) > version_tuple(tag):
        tag = "v" + package_version[1]
    existing = git("ls-remote", "origin", f"refs/tags/{tag}")
    if existing:
        sha = existing.split()[0]
        pending = tagged_release(tag)
        if pending and not pending["draft"]:
            raise ValueError("A release was published while planning; retry the check")
        if not pending:
            pending_source = git("show", "-s", "--format=%P", sha)
            pending = {"tag_name": tag,
                       "body": f"<!-- keel-release-source: {pending_source} -->"}
        source = release_base(pending, source)
        prs = merged_prs(base, source, repo)
        if not prs:
            raise ValueError("Pending snapshot does not contain unreleased merged PRs")
    else:
        with tempfile.TemporaryDirectory(prefix="keel-release-") as temporary:
            sha = candidate(source, tag, Path(temporary) / "source")
    branch = f"codex/release-{tag}-{source[:12]}"
    current = git("ls-remote", "origin", f"refs/heads/{branch}")
    if current and current.split()[0] != sha:
        raise ValueError("Existing candidate branch points at a different snapshot")
    push(f"{sha}:refs/heads/{branch}")
    return {"ready": True, "repo": repo, "base": release["tag_name"], "source": source,
            "candidate": sha, "branch": branch, "tag": tag, "prs": prs}


def validate_distributions(directory: Path, tag: str) -> list[Path]:
    version = tag.removeprefix("v")
    wheels = list(directory.glob("*.whl"))
    sdists = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("Expected exactly one tested wheel and one sdist")
    with zipfile.ZipFile(wheels[0]) as wheel:
        metadata = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata) != 1:
            raise ValueError("Expected exactly one wheel package")
        text = wheel.read(metadata[0]).decode()
        if not re.search(r"(?m)^Name: keel$", text) or f"\nVersion: {version}\n" not in text:
            raise ValueError("Wheel version differs from release tag")
    with tarfile.open(sdists[0]) as archive:
        members = [member for member in archive.getmembers()
                   if member.name.endswith("/keel/__init__.py")]
        if len(members) != 1 or f'__version__ = "{version}"' not in archive.extractfile(members[0]).read().decode():
            raise ValueError("Sdist version differs from release tag")
    files = wheels + sdists
    checksums = directory / "SHA256SUMS"
    checksums.write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(files)
    ))
    return files + [checksums]


def release_notes(plan: dict) -> str:
    lines = [f"<!-- keel-release-source: {plan['source']} -->", "", "Changes:"]
    for pr in plan["prs"]:
        title = " ".join(pr["title"].splitlines())
        lines.append(f"- {title} ([#{pr['number']}]({pr['html_url']}))")
    return "\n".join(lines) + "\n"


def publish(plan: dict, directory: Path) -> str:
    if plan["repo"] != REPOSITORY or os.environ["GITHUB_REPOSITORY"] != REPOSITORY:
        raise ValueError("Unexpected release repository")
    tag, sha = plan["tag"], plan["candidate"]
    version_tuple(tag)
    files = validate_distributions(directory, tag)
    release = tagged_release(tag)
    if release and not release["draft"]:
        target = git("ls-remote", "origin", f"refs/tags/{tag}")
        if not target or target.split()[0] != sha:
            raise ValueError("Published tag differs from the tested candidate")
        assets = {asset["name"]: asset.get("digest") for asset in release["assets"]}
        for path in files:
            if assets.get(path.name) != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError("Published assets differ from the tested distributions")
        return release["html_url"]
    latest = api(f"repos/{REPOSITORY}/releases/latest")
    if latest["tag_name"] != plan["base"]:
        raise ValueError("A different release was published while CI ran; replan")
    main = api(f"repos/{REPOSITORY}/git/ref/heads/main")["object"]["sha"]
    git("fetch", "origin", "main", plan["branch"])
    if not is_ancestor(plan["source"], main):
        raise ValueError("Public main no longer contains the planned source")
    if git("rev-parse", "HEAD") != sha or git("show", "-s", "--format=%P", sha) != plan["source"]:
        raise ValueError("Publish checkout is not the tested candidate")
    target = git("ls-remote", "origin", f"refs/tags/{tag}")
    if target:
        if target.split()[0] != sha:
            raise ValueError("Refusing to move an existing release tag")
    else:
        push(f"{sha}:refs/tags/{tag}")
    notes = directory / "release-notes.md"
    notes.write_text(release_notes(plan))
    if not release:
        command("gh", "release", "create", tag, "--repo", REPOSITORY,
                "--verify-tag", "--draft", "--title", f"keel {tag}", "--notes-file", str(notes))
    command("gh", "release", "upload", tag, "--repo", REPOSITORY, "--clobber",
            *(str(path) for path in files))
    uploaded = tagged_release(tag)
    if not uploaded:
        raise ValueError("Uploaded draft could not be found")
    assets = {asset["name"]: asset.get("digest") for asset in uploaded["assets"]}
    for path in files:
        if assets.get(path.name) != "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError("Uploaded asset checksum verification failed")
    command("gh", "release", "edit", tag, "--repo", REPOSITORY, "--draft=false", "--latest")
    published = api(f"repos/{REPOSITORY}/releases/{uploaded['id']}")
    if published["draft"] or published["prerelease"]:
        raise ValueError("Release is not published as stable")
    try:
        push(f":refs/heads/{plan['branch']}")
    except subprocess.CalledProcessError:
        print("Release published; candidate branch cleanup can be retried separately")
    return published["html_url"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "publish"))
    parser.add_argument("--version", default="")
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    if args.action == "plan":
        output(plan_release(args.version))
    else:
        url = publish(json.loads(os.environ["RELEASE_PLAN"]), args.dist)
        print(url)
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(f"Published: {url}\n")


if __name__ == "__main__":
    main()
