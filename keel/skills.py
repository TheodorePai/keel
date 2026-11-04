"""Skills：随 Keel 安装包分发的共享 skill，复制进项目，用 keel.lock 记录来源摘要。

教训（旧项目）：软链接在别的机器和克隆里失效；各项目复制后各自修改，同名 skill 出现多个版本。
因此：只复制不链接；记录装入时的摘要，项目内改动会被 keel check 发现，提示合回 Keel；
`.agents/skills/` 是唯一入口，`.claude/skills/` 由 keel sync 生成镜像。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tomllib
from pathlib import Path

from keel.assets import dumps_toml

BUNDLED_DIR = Path(__file__).resolve().parent / "data" / "skills"
SKILLS_DIR = ".agents/skills"
MIRROR_DIR = ".claude/skills"
LOCK_NAME = "keel.lock"
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
LINK_RE = re.compile(r"\]\(([^)\s#]+)")


def tree_digest(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file() and p.name != ".DS_Store"):
        h.update(f"{f.relative_to(path).as_posix()}\0".encode())
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return "sha256:" + h.hexdigest()


def bundled() -> dict[str, Path]:
    return {p.name: p for p in sorted(BUNDLED_DIR.iterdir()) if (p / "SKILL.md").exists()} \
        if BUNDLED_DIR.exists() else {}


def load_lock(root: Path) -> dict:
    path = root / LOCK_NAME
    return tomllib.loads(path.read_text()) if path.exists() else {}


def save_lock(root: Path, version: str, skills: dict[str, str], mirrors: list[str]) -> None:
    body = {"keel_version": version, "mirrors": ",".join(sorted(mirrors))}
    text = ("# 由 keel sync 维护，勿手改。记录装入时的 Keel 版本与各共享 skill 的摘要。\n"
            + dumps_toml({**body, "skills": dict(sorted(skills.items()))}))
    (root / LOCK_NAME).write_text(text)


def replace_tree(src: Path, dst: Path) -> None:
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".DS_Store"))


def sync(root: Path, config: dict, version: str, force: bool = False) -> list[str]:
    wanted = list(dict.fromkeys(config.get("skills", [])))
    available = bundled()
    unknown = [n for n in wanted if n not in available]
    if unknown:
        raise SystemExit(f"Keel {version} 没有这些 skill：{', '.join(unknown)}；可选：{', '.join(available)}")
    lock = load_lock(root)
    locked: dict[str, str] = dict(lock.get("skills", {}))
    out = []
    for name in wanted:
        local = root / SKILLS_DIR / name
        if local.exists() and name in locked and tree_digest(local) != locked[name] and not force:
            out.append(f"跳过 {name}：项目内改过（与装入时摘要不同）。先把改动合回 Keel，或 keel sync --force 覆盖")
            continue
        new = tree_digest(available[name])
        if local.exists() and tree_digest(local) == new:
            locked[name] = new
            continue
        replace_tree(available[name], local)
        locked[name] = new
        out.append(f"装入 {name}")
    for name in [n for n in locked if n not in wanted]:
        local = root / SKILLS_DIR / name
        if local.exists() and tree_digest(local) != locked[name] and not force:
            out.append(f"保留 {name}：已从 keel.toml 移除，但项目内改过，未删除")
            continue
        if local.exists():
            shutil.rmtree(local)
        del locked[name]
        out.append(f"移除 {name}")
    out += mirror(root, set(filter(None, str(lock.get("mirrors", "")).split(","))), locked, lock, version)
    return out


def mirror(root: Path, previous: set[str], locked: dict[str, str], lock: dict, version: str) -> list[str]:
    """把 .agents/skills/ 复制到 .claude/skills/，只管理自己生成过的目录。"""
    src_dir, dst_dir = root / SKILLS_DIR, root / MIRROR_DIR
    names = {p.name for p in src_dir.iterdir() if (p / "SKILL.md").exists()} if src_dir.exists() else set()
    out, managed = [], set()
    for name in sorted(names):
        dst = dst_dir / name
        if (dst.exists() or dst.is_symlink()) and name not in previous:
            if dst.is_dir() and not dst.is_symlink() and tree_digest(dst) == tree_digest(src_dir / name):
                managed.add(name)
                continue
            out.append(f"未镜像 {name}：{MIRROR_DIR}/{name} 已存在且不是 keel 生成的，请手动处理")
            continue
        if not dst.exists() or dst.is_symlink() or tree_digest(dst) != tree_digest(src_dir / name):
            replace_tree(src_dir / name, dst)
        managed.add(name)
    for name in previous - names:
        if (dst_dir / name).exists():
            shutil.rmtree(dst_dir / name)
    save_lock(root, version, locked, sorted(managed))
    return out


def parse_skill(path: Path) -> tuple[dict, str | None]:
    from keel.cli import parse_frontmatter
    meta, _, err = parse_frontmatter(path.read_text())
    return meta, err


def checks(root: Path, config: dict, *, check_updates: bool = True) -> list[tuple[str, str, str, str]]:
    out = []
    base = root / SKILLS_DIR
    skill_dirs = sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []
    for d in skill_dirs:
        where = f"{SKILLS_DIR}/{d.name}"
        if d.is_symlink():
            out.append((where, "skill-symlink", "skill 必须是复制的目录，不能是软链接", "error"))
            continue
        skill = d / "SKILL.md"
        if not skill.exists():
            out.append((where, "skill-format", "缺少 SKILL.md", "error"))
            continue
        meta, err = parse_skill(skill)
        if err:
            out.append((f"{where}/SKILL.md", "skill-format", err, "error"))
            continue
        name, desc = str(meta.get("name") or ""), str(meta.get("description") or "")
        if name != d.name or not NAME_RE.match(name) or len(name) > 64:
            out.append((f"{where}/SKILL.md", "skill-format",
                        "name 必须与目录同名，仅小写字母、数字和连字符，最长 64", "error"))
        if not desc or len(desc) > 1024:
            out.append((f"{where}/SKILL.md", "skill-format", "description 必填，最长 1024 字符", "error"))
        for target in LINK_RE.findall(skill.read_text()):
            if not re.match(r"^[a-z][a-z0-9+.-]*:", target) and not (d / target).exists():
                out.append((f"{where}/SKILL.md", "skill-link", f"引用的 {target} 不存在", "error"))
    lock = load_lock(root)
    locked = lock.get("skills", {})
    wanted = config.get("skills", [])
    if sorted(wanted) != sorted(locked):
        out.append(("keel.toml", "skill-sync", "skills 与 keel.lock 不一致，运行 keel sync", "error"))
    available = bundled() if check_updates else {}
    for name, value in locked.items():
        local = base / name
        if not local.exists():
            out.append((f"{SKILLS_DIR}/{name}", "skill-sync", "缺失，运行 keel sync", "error"))
        elif tree_digest(local) != value:
            out.append((f"{SKILLS_DIR}/{name}", "skill-modified",
                        "共享 skill 在项目内被改过：把改动合回 Keel 发布新版，再 keel sync", "warn"))
        elif name in available and tree_digest(available[name]) != value:
            out.append((f"{SKILLS_DIR}/{name}", "skill-outdated", "当前安装的 Keel 有新版本，运行 keel sync", "warn"))
    for name in filter(None, str(lock.get("mirrors", "")).split(",")):
        src, dst = base / name, root / MIRROR_DIR / name
        if src.exists() and (not dst.exists() or tree_digest(src) != tree_digest(dst)):
            out.append((f"{MIRROR_DIR}/{name}", "skill-mirror", "与 .agents/skills 不一致，运行 keel sync", "error"))
    return out
