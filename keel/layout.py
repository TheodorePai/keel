"""项目布局（决策 0007）：根目录固定，资产只进 work/，work/ 每个一级目录自己说明自己。"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

WORK = "work"
LOCAL = ".local"   # work/.local/：允许丢失的主动运行产物，不进 Git
LOCAL_PATH = f"{WORK}/{LOCAL}"
ROOT_ALLOWED = {"AGENTS.md", "CLAUDE.md", "README.md", "LICENSE", "keel.toml", "keel.lock",
                "fnox.toml", "assets.toml", "docs", WORK, "_keel"}
BANNED = {"tmp", "temp", "output", "outputs", "misc", "old", "new", "backup", "bak",
          "archive", "final", "copy"}
BANNED_SUFFIX = re.compile(r"[-_](v\d+|final|old|bak|copy|\d{4}-?\d{2}-?\d{2})$", re.I)
LINK = re.compile(r"\]\(\s*(?:\./)?([^)\s/#:]+)/")


def is_local(root: Path, path: Path) -> bool:
    """同时识别直接路径和指向运行目录的软链接（含尚不存在的目标）。"""
    local = root.resolve() / LOCAL_PATH
    return (Path(os.path.abspath(path)).is_relative_to(local)
            or path.resolve().is_relative_to(local.resolve()))


def files(root: Path) -> list[str]:
    """会进 Git 的文件：已跟踪加未被忽略的新文件。不是 Git 仓库时退回文件系统。"""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, text=True, check=True).stdout
        return [p for p in out.split("\0") if p and (root / p).exists()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = []
        for parent, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d != ".git" and not is_local(root, Path(parent) / d)]
            paths += [(Path(parent) / n).relative_to(root).as_posix() for n in names
                      if (Path(parent) / n).is_file()]
        return paths


def check_local(root: Path) -> list[tuple[str, str, str, str]]:
    out = []
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--", LOCAL_PATH],
                             capture_output=True, text=True)
    for path in filter(None, tracked.stdout.split("\0")):
        out.append((path, "local-tracked", "运行产物不能进入 Git；保留本机文件并用 "
                    f"git rm --cached -- {path} 移出暂存跟踪", "error"))
    # 要求目录规则；仅忽略内容的通配符仍可被后续反向规则穿透。
    # 不接受全局忽略或 .git/info/exclude：克隆和暂存快照必须能得到同样的保护。
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "--no-index", "-z", "-v", "--stdin"],
                             input=LOCAL_PATH + "/\0", capture_output=True, text=True)
    fields = ignored.stdout.split("\0")
    if not (ignored.returncode == 0 and len(fields) >= 4
            and fields[0] == ".gitignore" and fields[2] in (LOCAL_PATH + "/", "/" + LOCAL_PATH + "/")):
        out.append((".gitignore", "local-ignore", f"项目必须忽略整个 {LOCAL_PATH}/；"
                    "运行 keel init 补齐规则，并移除覆盖它的反向规则", "error"))
    return out


def checks(root: Path, config: dict) -> list[tuple[str, str, str, str]]:
    out = check_local(root)
    paths = files(root)
    for name in sorted({p.split("/")[0] for p in paths}):
        if not name.startswith(".") and name not in ROOT_ALLOWED:
            out.append((name, "layout-root", "根目录只放 Agent 说明、Keel 配置、docs/ 和 work/；"
                        "项目资产放 work/，运行产物放 work/.local/", "error"))
    if "notes_root" in config and config["notes_root"] != "docs":
        out.append(("keel.toml", "layout-root", "笔记目录固定为 docs，删除 notes_root", "error"))

    work = [p[len(WORK) + 1:] for p in paths if p.startswith(WORK + "/")]
    dirs = sorted({p.split("/")[0] for p in work if "/" in p and not p.startswith(".")})
    for d in dirs:
        if f"{d}/README.md" not in work:
            out.append((f"{WORK}/{d}", "layout-readme", "缺少 README.md：首段说明这个目录装什么", "error"))
    if dirs:
        out += check_map(root, dirs)
    seen = set()
    for p in work:
        parts = p.split("/")[:-1]
        if parts and parts[0].startswith("."):
            continue
        for i, part in enumerate(parts):
            where = "/".join([WORK, *parts[:i + 1]])
            if where in seen:
                continue
            seen.add(where)
            if part.lower() in BANNED or BANNED_SUFFIX.search(part):
                out.append((where, "layout-name", "目录名应说明装的是什么；历史版本归 Git，"
                            f"运行产物放 {WORK}/{LOCAL}/", "error"))
    return out


def check_map(root: Path, dirs: list[str]) -> list[tuple[str, str, str, str]]:
    where = f"{WORK}/README.md"
    readme = root / where
    if not readme.is_file():
        return [(where, "layout-map", "缺少 work/README.md：每个一级目录一行，链接到它的 README", "error")]
    listed = set(LINK.findall(readme.read_text())) - {".."}
    out = [(where, "layout-map", f"地图未列出 {d}/", "error") for d in dirs if d not in listed]
    out += [(where, "layout-map", f"地图列出的 {d}/ 不存在", "error")
            for d in sorted(listed - set(dirs))]
    return out


WORK_README = """# work

项目本身：源码、数据、配置和正式交付物。做过什么记在 `../docs/` 和 Beads；允许丢失的主动运行产物写 `.local/`（不进 Git）。工具管理的依赖和缓存可保留默认位置并忽略。

每个一级目录一行，链接到它自己的 README：

"""


def init(root: Path) -> list[str]:
    done = []
    readme = root / WORK / "README.md"
    if not readme.exists():
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text(WORK_README)
        done.append(f"写入 {WORK}/README.md（一级目录地图）")
    ignore = root / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    line = f"{WORK}/{LOCAL}/"
    if line not in lines:
        ignore.write_text("\n".join([*lines, "", "# 运行产物（决策 0007）", line]).lstrip("\n") + "\n")
        done.append(f".gitignore 忽略 {line}")
    return done


def verify(root: Path, config: dict) -> list[tuple[str, str, str, str]]:
    """运行项目在 keel.toml 声明的 verify 命令（领域契约检查），在 work/ 下执行。

    依赖（虚拟环境、node_modules）只在工作区，所以对工作区运行，而不是对暂存快照。"""
    command = config.get("verify")
    if not command:
        return []
    if not isinstance(command, str):
        return [("keel.toml", "verify", "verify 应是一条 shell 命令字符串", "error")]
    cwd = root / WORK if (root / WORK).is_dir() else root
    try:
        result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=config.get("verify_timeout", 600))
    except subprocess.TimeoutExpired:
        return [("keel.toml", "verify", f"verify 超时：{command}", "error")]
    if result.returncode == 0:
        return []
    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-20:])
    return [("keel.toml", "verify", f"verify 失败（退出码 {result.returncode}）：{command}"
             + (f"\n{tail}" if tail else ""), "error")]
