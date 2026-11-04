"""在隔离目录检查 Git 树，不改用户的工作区或暂存区。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from keel.cli import CONFIG_NAME, Finding, check, load_project


def command(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], input="", capture_output=True,
                          text=True, check=True).stdout.strip()


def staged_tree(root: Path) -> str:
    # write-tree 会更新 index 的缓存；只在副本上执行，保留原 index 的每个字节。
    index = Path(command(root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    with tempfile.TemporaryDirectory(prefix="keel-index-") as tmp:
        copy = Path(tmp) / "index"
        if index.exists():
            shutil.copyfile(index, copy)
        return subprocess.run(["git", "-C", str(root), "write-tree"], capture_output=True,
                              text=True, check=True,
                              env={**os.environ, "GIT_INDEX_FILE": str(copy)}).stdout.strip()


@contextmanager
def without_git_env(names: list[str]):
    # git commit --only / -a 的 hook 可以带临时 GIT_INDEX_FILE；读取树后隔离它，
    # 避免快照中的 ls-files 等命令意外访问用户的真实 index。
    saved = {n: os.environ.pop(n) for n in names if n in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def snapshot(root: Path, tree: str, baseline: str, history_ref: str, objects: str, *,
             skill_updates: bool = True) -> list[Finding]:
    with tempfile.TemporaryDirectory(prefix="keel-check-") as tmp:
        dest = Path(tmp)
        command(dest, "init", "-q")
        (dest / ".git/objects/info/alternates").write_text(objects + "\n")
        command(dest, "read-tree", tree)
        command(dest, "checkout-index", "--all", "--force")
        if not (dest / CONFIG_NAME).is_file():
            return [Finding(CONFIG_NAME, "snapshot", "被检查的 Git 树中缺少 keel.toml")]
        project = load_project(dest)
        project.history_root, project.history_ref = root, history_ref
        project.baseline, project.tree = baseline, tree
        return check(project, skill_updates=skill_updates)


def check_git(start: Path, *, staged: bool = False, ref: str | None = None,
              since: str | None = None) -> list[Finding]:
    try:
        root = Path(command(start, "rev-parse", "--show-toplevel"))
        names = command(root, "rev-parse", "--local-env-vars").splitlines()
        objects = command(root, "rev-parse", "--path-format=absolute", "--git-path", "objects")
        # write-tree 尊重 hook 传入的 index；未解决的冲突会在此返回失败。
        target = staged_tree(root) if staged else command(
            root, "rev-parse", "--verify", f"{ref or 'HEAD'}^{{commit}}")
        with without_git_env(names):
            # Git 的空树，作为首个提交的比较基线。
            empty = command(root, "hash-object", "-w", "-t", "tree", "--stdin")
            if staged:
                head = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
                                      capture_output=True, text=True)
                baseline = head.stdout.strip() if head.returncode == 0 else empty
                return snapshot(root, target, baseline, baseline, objects)
            commits = [target]
            if since:
                base = command(root, "rev-parse", "--verify", f"{since}^{{commit}}")
                command(root, "merge-base", "--is-ancestor", base, target)
                commits = command(root, "rev-list", "--reverse", "--topo-order", target, "^" + base).splitlines()
            findings = []
            for commit in commits:
                parents = command(root, "rev-list", "--parents", "-n", "1", commit).split()[1:]
                for parent in parents or [empty]:
                    for finding in snapshot(root, commit, parent, commit, objects, skill_updates=False):
                        if since:
                            finding.message = f"{commit[:12]}: {finding.message}"
                        findings.append(finding)
            return findings
    except (subprocess.CalledProcessError, OSError) as e:
        detail = e.stderr.strip() if isinstance(e, subprocess.CalledProcessError) else str(e)
        return [Finding("git", "snapshot", f"无法检查 Git 快照：{detail or str(e)}")]
