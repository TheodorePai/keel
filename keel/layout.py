"""项目布局（决策 0007）：根目录固定，资产只进 work/，work/ 每个一级目录自己说明自己。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

WORK = "work"
LOCAL = ".local"   # work/.local/：运行产物唯一出口，不进 Git
ROOT_ALLOWED = {"AGENTS.md", "CLAUDE.md", "README.md", "LICENSE", "keel.toml", "keel.lock",
                "fnox.toml", "assets.toml", "docs", WORK, "_keel"}
BANNED = {"tmp", "temp", "output", "outputs", "misc", "old", "new", "backup", "bak",
          "archive", "final", "copy"}
BANNED_SUFFIX = re.compile(r"[-_](v\d+|final|old|bak|copy|\d{4}-?\d{2}-?\d{2})$", re.I)
LINK = re.compile(r"\]\(\s*(?:\./)?([^)\s/#:]+)/")


def files(root: Path) -> list[str]:
    """会进 Git 的文件：已跟踪加未被忽略的新文件。不是 Git 仓库时退回文件系统。"""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                              "--exclude-standard"], capture_output=True, text=True, check=True).stdout
        return [p for p in out.split("\0") if p and (root / p).exists()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [p.relative_to(root).as_posix() for p in root.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(root).parts]


def checks(root: Path, config: dict) -> list[tuple[str, str, str, str]]:
    out = []
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

项目本身：源码、数据、配置和正式交付物。做过什么记在 `../docs/` 和 Beads，运行产物写 `.local/`（不进 Git）。

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
