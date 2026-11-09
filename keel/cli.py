"""Keel 命令行：按决策 0002 的笔记类型初始化、新建和检查项目笔记。"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

from keel import __version__, assets, entrypoints, layout, secrets, skills

DATA_DIR = Path(__file__).resolve().parent / "data"   # 随安装包分发，不依赖 Keel 仓库
SCHEMA_PATH = DATA_DIR / "schema.toml"
TEMPLATES_DIR = DATA_DIR / "notes"
CONFIG_NAME = "keel.toml"
CLAUDE_POINTER = "@AGENTS.md\n"
NOTES_ROOT = "docs"   # 决策 0007：固定，不可配置
OBSIDIAN_DIR = DATA_DIR / "obsidian"
ASSETS_DIR = "assets"
HOOK_BEGIN = "# --- BEGIN KEEL ---"
HOOK_END = "# --- END KEEL ---"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_CONFIG = """\
# Keel 项目配置。安装 Keel：
#   uv tool install keel --from git+https://github.com/omoyx/keel.git@v<版本>
# 然后在项目根目录运行 keel init / keel sync / keel doctor。
# 从 Keel 装入的共享 skill（keel sync 复制到 .agents/skills/）
skills = ["keel"]
# 本项目需要的密钥（变量名）。值统一存放在 fnox 全局配置，运行时用 fnox exec -- <命令> 注入
secrets = []
# 不参与检查的路径（相对 docs/，glob）
ignore = []
# 同一 incident cause 出现几次提示提炼成 skill 或规则
incident_repeat = 3
# Obsidian 不索引的路径（源码、依赖、数据等）
vault_exclude = ["node_modules/", "work/.local/"]
# 单个附件超过多少 MB 提醒改用专门存储
asset_max_mb = 5
# 项目自己的检查命令（测试、领域契约），在 work/ 下执行；keel check 与提交前都会运行
# verify = "make verify"
"""


# ---------- 基础 ----------

@dataclass
class Finding:
    path: str
    rule: str
    message: str
    level: str = "error"

    def __str__(self) -> str:
        return f"{self.level.upper():5} {self.path}: [{self.rule}] {self.message}"


@dataclass
class Note:
    path: Path
    rel: str
    meta: dict
    body: str
    text: str
    meta_error: str | None = None


@dataclass
class Project:
    root: Path
    config: dict
    schema: dict
    history_root: Path | None = None
    history_ref: str = "HEAD"
    baseline: str = "HEAD"
    tree: str | None = None
    notes_root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.notes_root = self.root / NOTES_ROOT

    @property
    def types(self) -> dict:
        return self.schema["types"]

    def type_for_dir(self, dirname: str) -> str | None:
        for name, spec in self.types.items():
            if spec["dir"] == dirname:
                return name
        return None


def load_project(start: Path) -> Project:
    root = start.resolve()
    for candidate in [root, *root.parents]:
        if (candidate / CONFIG_NAME).exists():
            root = candidate
            break
    else:
        raise SystemExit(f"找不到 {CONFIG_NAME}，先在项目根目录运行 keel init")
    config = tomllib.loads((root / CONFIG_NAME).read_text())
    schema = tomllib.loads(SCHEMA_PATH.read_text())
    return Project(root, config, schema)


def parse_frontmatter(text: str) -> tuple[dict, str, str | None]:
    """解析 frontmatter 的 YAML 子集：标量、[a, b] 行内列表、- 项列表。"""
    if not text.startswith("---\n"):
        return {}, text, "缺少 frontmatter"
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text, "frontmatter 未闭合"
    block, body = text[4:end], text[end + 4:].lstrip("\n")
    meta: dict = {}
    key = None
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  -", "- ")) and key:
            if meta.get(key) is None:
                meta[key] = []
            if isinstance(meta[key], list):
                meta[key].append(_scalar(line.split("-", 1)[1]))
            continue
        if ":" not in line:
            return meta, body, f"无法解析：{line!r}"
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [_scalar(v) for v in value[1:-1].split(",") if v.strip()]
        elif value == "":
            meta[key] = None
        else:
            meta[key] = _scalar(value)
    return meta, body, None


def _scalar(value: str):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def git(project: Project, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(project.history_root or project.root), *args],
                             capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout


def collect_notes(project: Project) -> list[Note]:
    ignore = project.config.get("ignore", [])
    notes = []
    if not project.notes_root.exists():
        return notes
    for path in sorted(project.notes_root.rglob("*.md")):
        rel = path.relative_to(project.notes_root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in ignore):
            continue
        text = path.read_text()
        meta, body, err = parse_frontmatter(text)
        notes.append(Note(path, rel, meta, body, text, err))
    return notes


# ---------- 检查 ----------

def check(project: Project, *, skill_updates: bool = True) -> list[Finding]:
    notes = collect_notes(project)
    findings = check_agent_entrypoints(project.root, project.config, require_work_mode=skill_updates)
    for note in notes:
        findings += check_placement(project, note)
    typed = [n for n in notes if not n.meta_error and n.meta.get("type") in project.types]
    for note in typed:
        findings += check_fields(project, note)
        findings += check_protocol_order(project, note)
    findings += check_numbering(project, notes)
    findings += check_final_records(project, notes)
    findings += check_beads(project, typed)
    findings += check_links(project, notes)
    findings += check_incident_repeats(project, typed)
    findings += check_assets(project, notes)
    for module in (assets, skills, secrets, layout):
        options = {"check_updates": skill_updates} if module is skills else {}
        findings += [Finding(path, rule, msg, level)
                     for path, rule, msg, level in module.checks(project.root, project.config, **options)]
    if skill_updates:  # 只检查当前状态；逐个历史提交不重跑项目命令
        findings += [Finding(*f) for f in layout.verify(project.history_root or project.root, project.config)]
    return findings


def valid_claude_pointer(path: Path) -> bool:
    try:
        return (not path.is_symlink() and path.is_file()
                and path.read_text().splitlines() == [CLAUDE_POINTER.rstrip("\n")])
    except (OSError, UnicodeError):
        return False


def check_agent_entrypoints(root: Path, config: dict, *, require_work_mode: bool = True) -> list[Finding]:
    out = []
    if not valid_claude_pointer(root / "CLAUDE.md"):
        out.append(Finding("CLAUDE.md", "agent-entrypoint",
                           "必须是只含一行 @AGENTS.md 的普通文件；缺失时运行 keel init，"
                           "已有规则请迁入 AGENTS.md 后仅保留该导入"))
    if not (root / "AGENTS.md").is_file():
        out.append(Finding("AGENTS.md", "agent-entrypoint",
                           "CLAUDE.md 的导入目标不存在或不是文件；运行 keel init 补齐"))
    else:
        try:
            text = (root / "AGENTS.md").read_text()
        except (OSError, UnicodeError):
            out.append(Finding("AGENTS.md", "agent-entrypoint", "必须是可读取的文本文件"))
            return out
        targets = markdown_link_targets(text)
        # 历史快照不追溯施加新增的接入约定；其中已有的链接仍核对可达性。
        if require_work_mode and "keel" in config.get("skills", []) and (
                not has_markdown_target(text, entrypoints.WORK_MODE_TARGET)
                or not (root / entrypoints.WORK_MODE_TARGET).is_file()):
            out.append(Finding("AGENTS.md", "work-mode",
                               "缺少 Keel 工作模式入口；运行 keel init 补齐，保留项目规则"))
        for target in targets:
            if not (root / target).exists():
                out.append(Finding("AGENTS.md", "agent-entrypoint-link", f"链接 {target} 指向不存在的文件"))
    return out


def display(project: Project, note: Note) -> str:
    return note.path.relative_to(project.root).as_posix()


def check_numbering(project: Project, notes: list[Note]) -> list[Finding]:
    groups: dict[tuple[str, str], list[Note]] = {}
    for note in notes:
        kind = project.type_for_dir(note.rel.split("/")[0])
        number = re.match(r"^(\d{4})-", note.path.name)
        if kind and project.types[kind].get("numbered") and number:
            groups.setdefault((kind, number[1]), []).append(note)
    return [Finding(display(project, duplicates[0]), "duplicate-number",
                    f"{kind} 编号 {number} 重复：{'、'.join(display(project, n) for n in duplicates)}；"
                    "合并前重编号并更新引用")
            for (kind, number), duplicates in groups.items() if len(duplicates) > 1]


def check_placement(project: Project, note: Note) -> list[Finding]:
    where = display(project, note)
    parts = note.rel.split("/")
    dir_type = project.type_for_dir(parts[0]) if len(parts) > 1 else None
    if dir_type is None:
        return [Finding(where, "placement",
                        "不在任何笔记类型目录下；放进对应类型目录，或在 keel.toml 的 ignore 中排除")]
    if note.meta_error:
        return [Finding(where, "frontmatter", note.meta_error)]
    declared = note.meta.get("type")
    if declared != dir_type:
        return [Finding(where, "type", f"type 为 {declared!r}，但位于 {dir_type} 目录")]
    return []


def check_fields(project: Project, note: Note) -> list[Finding]:
    where = display(project, note)
    spec = project.types[note.meta["type"]]
    out = []
    status = note.meta.get("status")
    if status not in spec["status"]:
        out.append(Finding(where, "status", f"status 必须是 {spec['status']} 之一，当前 {status!r}"))
    if spec["kind"] == "record":
        date = note.meta.get("date")
        if not (isinstance(date, str) and DATE_RE.match(date)):
            out.append(Finding(where, "date", "记录类笔记必须有 date: YYYY-MM-DD"))
    for key in spec.get("required", []):
        if not note.meta.get(key):
            out.append(Finding(where, "required", f"缺少必填属性 {key}"))
    if spec.get("numbered") and not re.match(r"^\d{4}-", note.path.name):
        out.append(Finding(where, "numbered", "文件名需以四位编号开头，如 0003-xxx.md"))
    elif spec.get("dated") and isinstance(note.meta.get("date"), str):
        if not note.path.name.startswith(note.meta["date"] + "-"):
            out.append(Finding(where, "filename", f"文件名需以 date 开头，如 {note.meta['date']}-xxx.md"))
    areas = project.config.get("areas")
    area = note.meta.get("area") or []
    for value in area if isinstance(area, list) else [area]:
        if areas is not None and value not in areas:
            out.append(Finding(where, "area", f"area {value!r} 不在 keel.toml 的 areas {areas} 中"))
    headings = HEADING_RE.findall(FENCE_RE.sub("", note.body))
    for keyword in spec.get("sections", []):
        if not any(keyword in h for h in headings):
            out.append(Finding(where, "section", f"缺少包含“{keyword}”的二级标题"))
    return out


def changed_paths(project: Project) -> list[tuple[str, str | None]]:
    args = ["diff", "-M", "--name-status", "-z", project.baseline]
    if project.tree:
        args.append(project.tree)
    fields = iter((git(project, *args) or "").rstrip("\0").split("\0"))
    changes = []
    for status in fields:
        if not status:
            continue
        old = next(fields)
        new = next(fields) if status.startswith(("R", "C")) else old
        if not status.startswith("A"):
            changes.append((old, None if status == "D" else new))
    return changes


def check_final_records(project: Project, notes: list[Note]) -> list[Finding]:
    """从旧版本的类型判断终态，删除、改类型、排除检查也不能绕过。"""
    current = {display(project, n): n for n in notes}
    out = []
    old_config = git(project, "show", f"{project.baseline}:{CONFIG_NAME}")
    config = tomllib.loads(old_config) if old_config else project.config
    prefix = NOTES_ROOT + "/"
    for old_path, new_path in changed_paths(project):
        if not old_path.startswith(prefix) or not old_path.endswith(".md"):
            continue
        if any(fnmatch.fnmatch(old_path[len(prefix):], p) for p in config.get("ignore", [])):
            continue
        old = git(project, "show", f"{project.baseline}:{old_path}")
        old_meta, old_body, err = parse_frontmatter(old or "")
        spec = project.types.get(old_meta.get("type"), {})
        final = spec.get("final", [])
        if err or spec.get("kind") != "record" or old_meta.get("status") not in final:
            continue
        note = current.get(new_path)
        if note is None:
            out.append(Finding(old_path, "append-only", "定稿记录不能删除或移出笔记检查范围"))
            continue
        if (note.meta.get("type") != old_meta.get("type") or note.meta.get("status") not in final
                or not strip_targets(note.body).rstrip().startswith(strip_targets(old_body).rstrip())):
            out.append(Finding(new_path, "append-only",
                               f"状态为 {old_meta['status']} 的记录只能在末尾追加，不能改写、改类型或退回草稿"))
    return out


def strip_targets(text: str) -> str:
    """只改链接地址（如文件改名后修链接）不算改写正文。"""
    return re.sub(r"(\[\[)[^\]|]+", r"\1", re.sub(r"\]\([^)]*\)", "]()", text))


def renamed_from(project: Project) -> dict[str, str]:
    return {new: old for old, new in changed_paths(project) if new and old != new}


def note_history(project: Project, rel: str) -> list[tuple[str, str]]:
    """返回 (提交, 当时路径)，沿改名追溯，不依赖可相同或倒退的时钟。"""
    def versions(path: str):
        log = git(project, "log", "--follow", "--format=%x00%H%x00", "--name-only", "-z",
                  project.history_ref, "--", path) or ""
        return re.findall(r"\0([0-9a-f]{40,64})\0\0\n([^\0]+)\0", log)
    return versions(rel) or versions(renamed_from(project).get(rel, rel))


def check_protocol_order(project: Project, note: Note) -> list[Finding]:
    ref = note.meta.get("protocol")
    if note.meta["type"] != "report" or not ref:
        return []
    where = display(project, note)
    target = (note.path.parent / ref).resolve()
    if not target.exists():
        target = (project.notes_root / ref).resolve()
    if not target.exists():
        return [Finding(where, "protocol", f"找不到引用的 protocol：{ref}")]
    meta, _, _ = parse_frontmatter(target.read_text())
    if meta.get("type") != "protocol" or meta.get("status") != "frozen":
        return [Finding(where, "protocol", f"引用的 protocol 尚未冻结（status: {meta.get('status')!r}）")]
    proto_rel = target.relative_to(project.root).as_posix()
    frozen = next((sha for sha, path in reversed(note_history(project, proto_rel))
                   if parse_frontmatter(git(project, "show", f"{sha}:{path}") or "")[0].get("status") == "frozen"), None)
    if frozen is None:
        return [Finding(where, "protocol", "引用的 protocol 冻结状态尚未提交；先提交冻结的 protocol，再写报告")]
    history = note_history(project, note.path.relative_to(project.root).as_posix())
    first_report = history[-1][0] if history else None
    if first_report and (first_report == frozen or git(project, "merge-base", "--is-ancestor", frozen, first_report) is None):
        return [Finding(where, "protocol", "protocol 的冻结提交必须严格早于报告首次提交，且为其祖先提交")]
    return []


def bd_ids(project: Project) -> set[str] | None:
    root = project.history_root or project.root
    if not (root / ".beads").exists() or not shutil.which("bd"):
        return None
    try:
        out = subprocess.run(["bd", "list", "--all", "--json", "--limit", "0"], cwd=root,
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "BD_NON_INTERACTIVE": "1"})
        return {item["id"] for item in json.loads(out.stdout)}
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
        return None


def check_beads(project: Project, notes: list[Note]) -> list[Finding]:
    referenced = [(n, i) for n in notes for i in (n.meta.get("beads") or []) if i]
    if not referenced:
        return []
    ids = bd_ids(project)
    if ids is None:
        return [Finding(".beads", "beads", "无法读取 bd 任务列表，跳过任务 ID 检查", "warn")]
    return [Finding(display(project, n), "beads", f"任务 {i} 在 bd 中不存在")
            for n, i in referenced if i not in ids]


FENCE_RE = re.compile(r"^(```|~~~).*?^\1", re.M | re.S)
HEADING_RE = re.compile(r"^##\s+(.+)$", re.M)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
MDLINK_RE = re.compile(r"\]\((?:<([^>]+)>|([^)\s]+))\)")


def markdown_link_targets(text: str) -> list[str]:
    """仅取正文的本地 Markdown 文件链接；示例、行内代码和远端链接不参与。"""
    text = re.sub(r"`[^`\n]*`", "", FENCE_RE.sub("", text))
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    targets = []
    for angled, plain in MDLINK_RE.findall(text):
        target = angled or plain
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("#"):
            continue
        path = urllib.parse.unquote(target.split("#")[0])
        if path:
            targets.append(os.path.normpath(path))
    return targets


def link_exists(project: Project, note: Note, target: str, registered: dict) -> bool:
    """快照内的普通目标必须入树；登记的忽略资产和仓库外链接在原位置核验。"""
    path = Path(os.path.abspath(note.path.parent / target))
    if not project.history_root:
        return path.exists()
    original = Path(os.path.abspath(project.history_root / note.path.relative_to(project.root).parent / target))
    if not original.is_relative_to(project.history_root):
        return original.exists()
    snapshot_path = project.root / original.relative_to(project.history_root)
    if snapshot_path.exists():
        return True
    for entry in registered.values():
        rel = entry.get("path", "")
        asset_path = Path(os.path.abspath(project.history_root / rel))
        if not rel or not asset_path.is_relative_to(project.history_root):
            continue
        if original != asset_path and not (entry.get("path_type") == "directory"
                                          and original.is_relative_to(asset_path)):
            continue
        ignored = rel.rstrip("/") + "/" if entry.get("path_type") == "directory" else rel
        if (assets.run("git", "check-ignore", "-q", ignored, cwd=project.root,
                       check=False).returncode == 0
                and original.resolve().is_relative_to(asset_path.resolve())):
            return original.exists()
    return False


def has_markdown_target(text: str, target: str) -> bool:
    """比较双方都按本机路径规范化，保留链接解析器原有的路径语义。"""
    return os.path.normpath(target) in markdown_link_targets(text)


def check_links(project: Project, notes: list[Note]) -> list[Finding]:
    try:
        registered = assets.Store(project.root, project.config).load()
    except tomllib.TOMLDecodeError:
        registered = {}  # assets.checks 单独报告清单格式错误。
    paths = [Path(p) for p in layout.files(project.root)
             if not layout.is_local(project.root, project.root / p)]
    names = {p.stem for p in paths if p.suffix == ".md"} | {p.name for p in paths}
    out = []
    for note in notes:
        text = FENCE_RE.sub("", note.text)
        text = re.sub(r"`[^`\n]*`", "", text)
        for target in WIKILINK_RE.findall(text):
            target = target.strip()
            decoded = urllib.parse.unquote(target)
            if any(layout.is_local(project.root, base / decoded)
                   for base in (project.root, note.path.parent)):
                out.append(Finding(display(project, note), "local-link",
                                   f"[[{target}]] 依赖可丢弃的 work/.local/；先保存为正式资产或笔记附件"))
            elif Path(target).name not in names and Path(target).stem not in names:
                out.append(Finding(display(project, note), "link", f"[[{target}]] 指向不存在的笔记"))
        for target in markdown_link_targets(text):
            path = note.path.parent / target
            if layout.is_local(project.root, path):
                out.append(Finding(display(project, note), "local-link",
                                   f"链接 {target} 依赖可丢弃的 work/.local/；先保存为正式资产或笔记附件"))
            elif not link_exists(project, note, target, registered):
                out.append(Finding(display(project, note), "link", f"链接 {target} 指向不存在的文件"))
    return out


def check_assets(project: Project, notes: list[Note]) -> list[Finding]:
    """附件放在所属笔记同目录的 assets/ 下；未被引用的附件给提醒。过大的文件由 assets.checks 报错。"""
    referenced = set()
    for note in notes:
        for angled, plain in MDLINK_RE.findall(FENCE_RE.sub("", note.text)):
            target = urllib.parse.unquote((angled or plain).split("#")[0])
            referenced.add((note.path.parent / target).resolve())
    ignore = project.config.get("ignore", [])
    out = []
    if not project.notes_root.exists():
        return out
    for path in sorted(project.notes_root.rglob("*")):
        rel = path.relative_to(project.notes_root).as_posix()
        if (not path.is_file() or path.suffix in (".md", ".base") or path.name.startswith(".")
                or any(fnmatch.fnmatch(rel, pat) for pat in ignore)):
            continue
        where = path.relative_to(project.root).as_posix()
        if path.parent.name != ASSETS_DIR:
            out.append(Finding(where, "asset", f"附件应放在所属笔记旁的 {ASSETS_DIR}/ 目录", "warn"))
        elif path.resolve() not in referenced:
            out.append(Finding(where, "asset", "没有笔记引用这个附件", "warn"))
    return out


def check_incident_repeats(project: Project, notes: list[Note]) -> list[Finding]:
    limit = int(project.config.get("incident_repeat", 3))
    causes: dict[str, list[Note]] = {}
    for note in notes:
        if note.meta["type"] == "incident" and note.meta.get("cause"):
            causes.setdefault(str(note.meta["cause"]), []).append(note)
    return [Finding(f"cause={cause}", "repeat",
                    f"已出现 {len(group)} 次，提炼成 skill 或项目规则", "warn")
            for cause, group in causes.items() if len(group) >= limit]


# ---------- 新建与初始化 ----------

def slugify(title: str) -> str:
    slug = re.sub(r"[\s/\\:]+", "-", title.strip().lower())
    slug = re.sub(r"[^\w\-.]", "", slug)
    return slug.strip("-") or "untitled"


def new_note(project: Project, kind: str, title: str, slug: str | None, beads: list[str],
             area: list[str] | None = None, protocol: str | None = None, cause: str | None = None) -> Path:
    if kind not in project.types:
        raise SystemExit(f"未知类型 {kind}；可选：{', '.join(project.types)}")
    spec = project.types[kind]
    folder = project.notes_root / spec["dir"]
    name = slug or slugify(title)
    if spec.get("numbered"):
        nums = [int(p.name[:4]) for p in folder.glob("[0-9][0-9][0-9][0-9]-*.md")] if folder.exists() else []
        name = f"{max(nums, default=0) + 1:04d}-{name}"
    elif spec.get("dated"):
        name = f"{dt.date.today().isoformat()}-{name}"
    path = folder / f"{name}.md"
    if path.exists():
        raise SystemExit(f"已存在：{path}")
    text = (TEMPLATES_DIR / f"{kind}.md").read_text()
    text = text.replace("{{title}}", title).replace("{{date}}", dt.date.today().isoformat())
    if beads:
        text = text.replace("beads: []", f"beads: [{', '.join(beads)}]", 1)
    if area:
        text = text.replace("area: []", f"area: [{', '.join(area)}]", 1)
    if cause:
        text = text.replace("cause:\n", f"cause: {cause}\n", 1)
    if protocol:
        target = next((c for c in [Path(protocol), project.notes_root / "protocols" / protocol,
                                   project.notes_root / "protocols" / f"{protocol}.md"] if c.is_file()), None)
        if target is None:
            raise SystemExit(f"找不到 protocol：{protocol}")
        rel = os.path.relpath(target.resolve(), folder.resolve())
        text = text.replace("protocol:\n", f"protocol: {rel}\n", 1)
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


MANAGED_PATHS = [CONFIG_NAME, skills.LOCK_NAME, ".gitignore", "AGENTS.md", "CLAUDE.md", ".agents/skills", ".claude/skills",
                 ".claude/settings.json", ".codex/config.toml", ".codex/hooks.json",
                 ".cursor/hooks.json", ".cursor/rules/beads.mdc",
                 ".obsidian", ".beads/hooks/pre-commit", secrets.FNOX_FILE, "work/README.md"]


def dolt_remote_url(origin: str) -> str:
    """git origin → Dolt 的 git remote 写法。bd 只自动识别网络地址，本地路径要显式给 git+file://。"""
    if origin.startswith(("git+", "https://doltremoteapi")):
        return origin
    m = re.match(r"^([\w.-]+)@([\w.-]+):(.+)$", origin)
    if m:
        return f"git+ssh://{m.group(1)}@{m.group(2)}/{m.group(3)}"
    if re.match(r"^(https?|ssh|file)://", origin):
        return "git+" + origin
    return "git+file://" + str(Path(origin).expanduser().resolve())


def sh(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=root, capture_output=True, text=True,
                          env={**os.environ, "BD_NON_INTERACTIVE": "1"})


def required_step(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        out = sh(root, *args)
    except OSError as e:
        raise SystemExit(f"{' '.join(args[:2])} 失败：{e}") from e
    if out.returncode != 0:
        raise SystemExit(f"{' '.join(args[:2])} 失败：{(out.stderr or out.stdout).strip()[-400:]}")
    return out


def setup_beads(root: Path, prefix: str | None) -> list[str]:
    """编排 bd：新项目 bd init，克隆来的项目 bd bootstrap。日常任务命令仍直接用 bd。"""
    if not shutil.which("bd"):
        raise SystemExit("未安装 bd：安装后重新运行 keel init；只准备 Keel 文件可用 --no-commit")
    if (root / ".beads" / "embeddeddolt").exists():
        return []
    tracked = sh(root, "git", "ls-files", "--error-unmatch", ".beads/metadata.json").returncode == 0
    if not tracked:
        # 未完成的本地初始化可能留下 .beads/；重新 init，不误当成远端克隆。
        required_step(root, "bd", "init", "--prefix", prefix or assets.project_slug(root),
                      "--init-if-missing", "--skip-agents")
        return ["已初始化任务库（bd init）"]
    required_step(root, "bd", "bootstrap")
    return ["已取回任务库（bd bootstrap）"]


def setup_beads_integrations(root: Path) -> list[str]:
    """由 bd 维护 skill 和各编辑器钩子，入口正文由 Keel 统一维护。"""
    # bd setup 同时写入口与钩子。无论成功或失败，都恢复原入口；已有项目 skill
    # 和 Cursor 规则也不让上游 setup 覆盖，新的文件则保留供 Keel 同步和提交。
    names = ("AGENTS.md", "CLAUDE.md", ".agents/skills/beads/SKILL.md", ".cursor/rules/beads.mdc")
    saved = {}
    for name in names:
        path = root / name
        if path.is_symlink() or any((root / p).is_symlink() for p in Path(name).parents):
            raise SystemExit(f"{name} 不能是软链接；原文未改动")
        if path.exists() and not path.is_file():
            raise SystemExit(f"{name} 必须是普通文件；原文未改动")
        saved[name] = path.read_bytes() if path.exists() else None
    try:
        for recipe in ("claude", "codex", "cursor"):
            required_step(root, "bd", "setup", recipe)
    finally:
        for name, original in saved.items():
            path = root / name
            if original is not None:
                path.write_bytes(original)
            elif name in ("AGENTS.md", "CLAUDE.md"):
                path.unlink(missing_ok=True)
    return ["已启用 Beads skill 与 Claude/Codex/Cursor 上下文钩子"]


def commit_managed(root: Path, message: str) -> list[str]:
    """只提交 Keel 管理的路径，不夹带项目里其他未提交的改动。"""
    paths = [p for p in MANAGED_PATHS if (root / p).exists()]
    required_step(root, "git", "add", "--", *paths)
    staged = required_step(root, "git", "diff", "--cached", "--name-only", "--", *paths).stdout.splitlines()
    if not staged:
        return []
    required_step(root, "git", "commit", "-m", message, "--", *paths)
    return [f"已提交 {len(staged)} 个文件"]


def publish_github(root: Path) -> list[str]:
    """--github：建私有仓库并推送代码与任务库。对外操作，只在显式要求时执行。"""
    done = []
    personal = assets.load_personal()
    owner = personal.get("gh_owner") or assets.gh_login()
    if not owner:
        raise SystemExit("GitHub 未登录：先运行 gh auth login")
    if not sh(root, "git", "remote", "get-url", "origin").stdout.strip():
        repo = f"{owner}/{assets.project_slug(root)}"
        out = sh(root, "gh", "repo", "create", repo, "--private", "--source", ".", "--remote", "origin", "--push")
        if out.returncode != 0:
            raise SystemExit(f"gh repo create 失败：{out.stderr.strip()[-400:]}")
        done.append(f"已创建私有仓库 {repo} 并推送")
    else:
        out = sh(root, "git", "push", "-u", "origin", "HEAD")
        if out.returncode != 0:
            raise SystemExit(f"git push 失败：{out.stderr.strip()[-400:]}")
        done.append("已推送代码")
    if (root / ".beads").is_dir() and shutil.which("bd"):
        if "origin" not in sh(root, "bd", "dolt", "remote", "list").stdout:
            origin = sh(root, "git", "remote", "get-url", "origin").stdout.strip()
            # Keel deliberately keeps task data in this project's Git remote.
            out = sh(root, "bd", "dolt", "remote", "add", "origin", dolt_remote_url(origin),
                     "--allow-git-origin")
            if out.returncode != 0:
                raise SystemExit(f"bd dolt remote add 失败：{out.stderr.strip()[-400:]}")
        out = sh(root, "bd", "dolt", "push")
        if out.returncode != 0:
            raise SystemExit(f"bd dolt push 失败：{out.stderr.strip()[-400:]}")
        if not sh(root, "git", "ls-remote", "origin", "refs/dolt/data").stdout.strip():
            raise SystemExit(f"bd dolt push 没有把任务库推到 origin：{out.stdout.strip()[-400:]}")
        commit_paths = [".beads/config.yaml"]
        required_step(root, "git", "add", "--", *commit_paths)
        if required_step(root, "git", "diff", "--cached", "--name-only", "--", *commit_paths).stdout.strip():
            required_step(root, "git", "commit", "-m", "bd: record sync remote", "--", *commit_paths)
        required_step(root, "git", "push", "origin", "HEAD")
        done.append("已推送任务库（refs/dolt/data）")
    return done


def init_project(root: Path, prefix: str | None = None, commit: bool = False, github: bool = False) -> list[str]:
    done = []
    prepared = entrypoints.prepare(root)
    if commit and not (root / ".git").exists():
        required_step(root, "git", "init", "-b", "main")
        done.append("已 git init")
    if commit:
        done += setup_beads(root, prefix)  # 先于 Keel 的文件：bd init 会自己提交一次
        done += setup_beads_integrations(root)
    done += entrypoints.install_migrations(root, prepared)
    config = root / CONFIG_NAME
    if not config.exists():
        config.write_text(DEFAULT_CONFIG)
        done.append(f"写入 {CONFIG_NAME}")
    done += assets.ensure_storage(root, config)
    done += install_hook(root)
    done += install_claude_pointer(root)
    done += install_agents_pointer(root)
    if "keel" in tomllib.loads(config.read_text()).get("skills", []):
        agents = root / "AGENTS.md"
        text = agents.read_text()
        if entrypoints.LEGACY_WORK_MODE_POINTER in text:
            agents.write_text(text.replace(entrypoints.LEGACY_WORK_MODE_POINTER, entrypoints.WORK_MODE_POINTER))
            done.append("AGENTS.md 已补充 skill 与常见错误处理方法的提炼约定")
        elif not has_markdown_target(text, entrypoints.WORK_MODE_TARGET):
            agents.write_text(text.rstrip() + "\n\n" + entrypoints.WORK_MODE_POINTER + "\n")
            done.append("AGENTS.md 已接入 Keel 默认工作模式")
        elif entrypoints.SKILL_CAPTURE_RULE not in text:
            agents.write_text(text.rstrip() + "\n\n- " + entrypoints.SKILL_CAPTURE_RULE
                              + "提炼与验收按已有工作模式入口执行。\n")
            done.append("AGENTS.md 已补充 skill 与常见错误处理方法的提炼约定")
        text = agents.read_text()
        if (updated := entrypoints.with_knowledge_rule(text)) != text:
            agents.write_text(updated)
            done.append("AGENTS.md 已补充知识收尾约定")
    if (root / ".agents/skills/beads/SKILL.md").is_file():
        agents = root / "AGENTS.md"
        text = agents.read_text()
        if ".agents/skills/beads/SKILL.md" not in text:
            agents.write_text(text.rstrip() + "\n\n" + entrypoints.BEADS_POINTER + "\n")
            done.append("AGENTS.md 已指向 beads skill")
    done += entrypoints.install_git_authority(root)
    done += install_obsidian_templates(root)
    done += skills.sync(root, tomllib.loads(config.read_text()), __version__) or ["共享 skill 已是最新"]
    done += secrets.init(root, tomllib.loads(config.read_text()))
    done += layout.init(root)
    if commit:
        done += commit_managed(root, "chore: init with keel")
    if github:
        done += publish_github(root)
    if commit and (root / assets.MANIFEST_NAME).exists():
        done.append("有资产清单：需要时运行 keel fetch 取回大文件")
    return done


LOCAL_START = "开始项目工作时运行 `keel local sweep --daily`；运行产物的生命周期与收尾按 Keel skill 执行。"


AGENTS_POINTER = ("本项目使用 Keel 工作流（命令 `keel`，说明见 `.agents/skills/keel/SKILL.md`；不是 keel.sh）。"
                  "笔记、大文件、共享 skill 按其执行，提交前 `keel check` 必须通过。")


def install_claude_pointer(root: Path) -> list[str]:
    """Claude 入口只导入 AGENTS.md；已有额外内容必须先迁移，不自动覆盖。"""
    path = root / "CLAUDE.md"
    if path.exists() or path.is_symlink():
        if not valid_claude_pointer(path):
            raise SystemExit("CLAUDE.md 必须只含一行 @AGENTS.md；已有内容未改动，"
                             "请将规则迁入 AGENTS.md 后重试 keel init")
        return []
    path.write_text(CLAUDE_POINTER)
    return ["CLAUDE.md 已导入 AGENTS.md"]


def install_agents_pointer(root: Path) -> list[str]:
    """在 AGENTS.md 标题后插入一行指向 keel skill；规则本身不抄进 AGENTS.md。"""
    path = root / "AGENTS.md"
    text = path.read_text() if path.exists() else "# AGENTS.md\n"
    if ".agents/skills/keel/SKILL.md" in text:
        if LOCAL_START not in text:
            path.write_text(text.rstrip() + "\n\n" + LOCAL_START + "\n")
            return ["AGENTS.md 已接入每日运行产物检查"]
        return []
    lines = text.splitlines()
    at = 1 if lines and lines[0].startswith("# ") else 0
    lines[at:at] = ["", AGENTS_POINTER, "", LOCAL_START]
    path.write_text("\n".join(lines).rstrip("\n") + "\n")
    return ["AGENTS.md 已指向 keel skill"]


def install_hook(root: Path) -> list[str]:
    done = []
    hooks_path_set = subprocess.run(["git", "-C", str(root), "config", "core.hooksPath"],
                                    capture_output=True, text=True).stdout.strip()
    if (root / ".beads").is_dir() and shutil.which("bd") and not hooks_path_set:
        # 克隆后 bd 的钩子不会自动启用；先装 bd 的，再把 keel check 接到同一个 pre-commit 上
        required_step(root, "bd", "hooks", "install")
        done.append("已启用 bd git 钩子")
    if (root / ".beads").is_dir():
        (root / ".beads").chmod(0o700)  # bd 要求；克隆出来是 0755，否则每条命令都告警
        role = subprocess.run(["git", "-C", str(root), "config", "beads.role"], capture_output=True, text=True)
        if not role.stdout.strip():
            required_step(root, "git", "config", "beads.role", "maintainer")
            done.append("设置 beads.role=maintainer（本机）")
    return done + _install_keel_hook(root)


def _install_keel_hook(root: Path) -> list[str]:
    hooks = subprocess.run(["git", "-C", str(root), "rev-parse", "--git-path", "hooks"],
                           capture_output=True, text=True)
    hooks_path = subprocess.run(["git", "-C", str(root), "config", "core.hooksPath"],
                                capture_output=True, text=True).stdout.strip()
    if hooks.returncode != 0:
        return ["不是 git 仓库，跳过 pre-commit"]
    hook_dir = Path(hooks_path) if hooks_path else root / hooks.stdout.strip()
    if not hook_dir.is_absolute():
        hook_dir = root / hook_dir
    hook = hook_dir / "pre-commit"
    block = (f"{HOOK_BEGIN}\n"
             "if command -v keel >/dev/null 2>&1; then keel check --staged || exit 1\n"
             'else echo "keel 未安装，跳过笔记结构检查（安装见 keel.toml 顶部注释）" >&2; fi\n'
             f"{HOOK_END}\n")
    text = hook.read_text() if hook.exists() else "#!/usr/bin/env sh\n"
    if HOOK_BEGIN in text:
        text = re.sub(re.escape(HOOK_BEGIN) + r".*?" + re.escape(HOOK_END) + r"\n", block, text, flags=re.S)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    hook_dir.mkdir(parents=True, exist_ok=True)
    hook.write_text(text)
    hook.chmod(0o755)
    return [f"pre-commit 已接入 keel check：{hook}"]


def install_obsidian_templates(root: Path) -> list[str]:
    """Obsidian 模板目录必须在库内：Keel 源码仓库直接用 work/keel/data/notes，其他项目生成 _keel/templates。"""
    if (root / "work" / "keel" / "data" / "notes").is_dir():
        folder = "work/keel/data/notes"
    else:
        folder = "_keel/templates"
        target = root / folder
        target.mkdir(parents=True, exist_ok=True)
        for src in TEMPLATES_DIR.glob("*.md"):
            shutil.copyfile(src, target / src.name)
    base = (DATA_DIR / "notes.base.tmpl").read_text().replace("{{notes_root}}", NOTES_ROOT)
    (root / "_keel").mkdir(exist_ok=True)
    (root / "_keel" / "notes.base").write_text(base)
    obsidian = root / ".obsidian"
    obsidian.mkdir(exist_ok=True)
    apply_obsidian_config(root, obsidian)
    ignore = root / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    if "_keel/" not in lines:
        ignore.write_text("\n".join([*lines, "", "# Keel 生成的 Obsidian 模板与视图", "_keel/"]).lstrip() + "\n")
    (obsidian / "templates.json").write_text(json.dumps({
        "folder": folder, "dateFormat": "YYYY-MM-DD"}, indent=2) + "\n")
    return [f"Obsidian 模板目录：{folder}；笔记视图：_keel/notes.base；"
            f"主题与插件：{', '.join(read_json(OBSIDIAN_DIR / 'community-plugins.json'))}"]


def read_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def apply_obsidian_config(root: Path, obsidian: Path) -> None:
    """合并 Keel 的库配置（决策 0008）：defaults/ 只补缺失的键，顶层文件的键每次覆盖；项目自己加的键和插件保留。"""
    config = tomllib.loads((root / CONFIG_NAME).read_text())
    for name in ("plugins", "themes"):
        for src in (OBSIDIAN_DIR / name).iterdir():
            dst = obsidian / name / src.name
            dst.mkdir(parents=True, exist_ok=True)
            for f in src.iterdir():
                if f.name == "data.json" and (dst / f.name).exists():
                    continue  # 保留各项目已调整的插件设置
                if name == "plugins" and src.name == "beads-pane" and f.name == "data.json":
                    settings = read_json(f)
                    settings.update(projects=[{"name": root.name, "path": str(root.resolve())}],
                                    activeProject=root.name, bdPath=shutil.which("bd") or "bd")
                    write_json(dst / f.name, settings)
                else:
                    shutil.copyfile(f, dst / f.name)
    for name in ("app.json", "appearance.json", "core-plugins.json"):
        merged = {**read_json(OBSIDIAN_DIR / "defaults" / name, {}),
                  **read_json(obsidian / name, {}), **read_json(OBSIDIAN_DIR / name, {})}
        if name == "app.json":
            merged["userIgnoreFilters"] = sorted({*merged.get("userIgnoreFilters", []),
                                                  *config.get("vault_exclude", [])})
        write_json(obsidian / name, merged)
    plugins = read_json(obsidian / "community-plugins.json", [])
    write_json(obsidian / "community-plugins.json",
               plugins + [p for p in read_json(OBSIDIAN_DIR / "community-plugins.json") if p not in plugins])


# ---------- 入口 ----------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keel", description="Keel：跨项目的 Agent 工作流底座")
    parser.add_argument("--version", action="version", version=f"keel {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="按 keel.toml 装入或更新共享 skill，并生成 .claude/skills 镜像")
    p_sync.add_argument("--force", action="store_true", help="覆盖项目内改过的共享 skill")
    sub.add_parser("skills", help="列出当前 Keel 版本自带的共享 skill")
    p_init = sub.add_parser("init", help="接入或在新机器上恢复项目：bd 任务库、规则、钩子、Obsidian、共享 skill，并提交")
    p_init.add_argument("--prefix", help="新任务库的编号前缀，默认用目录名")
    p_init.add_argument("--github", action="store_true", help="另外创建 GitHub 私有仓库并推送代码与任务库")
    p_init.add_argument("--no-commit", action="store_true", help="只写文件，不初始化任务库、不提交")
    p_check = sub.add_parser("check", help="提交前检查：笔记结构、疑似密钥与 .env、大文件、共享 skill")
    p_check.add_argument("--json", action="store_true")
    check_source = p_check.add_mutually_exclusive_group()
    check_source.add_argument("--staged", action="store_true", help="检查实际暂存内容（pre-commit 使用）")
    check_source.add_argument("--ref", help="检查指定提交，默认与它的父提交比较")
    p_check.add_argument("--since", help="检查此提交之后至 --ref（默认 HEAD）的每次提交，供 CI 使用")
    p_new = sub.add_parser("new", help="按类型模板新建笔记")
    p_new.add_argument("type")
    p_new.add_argument("title")
    p_new.add_argument("--slug")
    p_new.add_argument("--beads", nargs="*", default=[], help="关联的 bd 任务 ID")
    p_new.add_argument("--area", nargs="*", default=[], help="所属领域（keel.toml 可用 areas 限定取值）")
    p_new.add_argument("--protocol", help="report：引用的 protocol 文件名或路径")
    p_new.add_argument("--cause", help="incident：原因标签，用于发现重复问题")
    sub.add_parser("types", help="列出笔记类型")
    sub.add_parser("status", help="当前情况：进行中与阻塞的任务、里程碑、最近的记录和标签")
    p_local = sub.add_parser("local", help="查看运行产物大小，按保留期显式清理指定目录")
    lsub = p_local.add_subparsers(dest="local_cmd", required=True)
    lsub.add_parser("status", help="按一级条目显示 work/.local/ 文件大小")
    p_sweep = lsub.add_parser("sweep", help="自动回收已到期且声明可丢弃的运行，提醒未知内容和容量")
    p_sweep.add_argument("--daily", action="store_true", help="同一本地日最多执行一次")
    p_run = lsub.add_parser("run", help="新建运行目录或更新生命周期状态")
    p_run.add_argument("path")
    p_run.add_argument("--pid", type=int, help="运行进程或监督进程 PID；默认调用者父进程")
    p_run.add_argument("--reason", help="保留原因")
    p_run.add_argument("--review-on", help="保留复查日期 YYYY-MM-DD")
    p_run.add_argument("state", choices=["running", "completed", "disposable", "retained"])
    p_clean = lsub.add_parser("clean", help="预览清理指定运行产物，默认保留完成后的 30 天")
    p_clean.add_argument("paths", nargs="+", help="相对 work/.local/ 的任务或运行路径")
    p_clean.add_argument("--older-than", type=int, default=None, metavar="DAYS", help="只清理早于此天数的内容")
    p_clean.add_argument("--apply", action="store_true", help="显式执行；跳过 .keep/.active 标记及近期修改")
    p_secret = sub.add_parser("secret", help="共享密钥：只存一份，项目按需注入（基于 fnox + age）")
    ssub = p_secret.add_subparsers(dest="secret_cmd", required=True)
    p_sset = ssub.add_parser("set", help="从标准输入保存或更新一把共享密钥；--note 写说明（归属、用途）")
    p_sset.add_argument("name")
    p_sset.add_argument("--note", help="说明；只改说明时不必从标准输入给值")
    p_simp = ssub.add_parser("import", help="把 .env 中的值收进共享存储（不修改原文件）")
    p_simp.add_argument("path")
    p_simp.add_argument("--only", nargs="*", help="只导入这些变量")
    p_sadd = ssub.add_parser("use", help="本项目要用哪些密钥（只声明名称）；NAME=SOURCE 表示变量 NAME 取共享的 SOURCE")
    p_slist = ssub.add_parser("list", help="本项目声明的密钥及本机是否已有；--all 列出本机全部（均不显示值）")
    p_slist.add_argument("--all", action="store_true", help="本机全部共享密钥：说明、哪些项目在用")
    p_sadd.add_argument("names", nargs="+")
    sub.add_parser("doctor", help="检查工具、账号、存储与资产是否就绪")
    p_fetch = sub.add_parser("fetch", help="按 assets.toml 取回资产并校验")
    p_fetch.add_argument("names", nargs="*")
    p_asset = sub.add_parser("asset", help="登记与校验大文件资产")
    asub = p_asset.add_subparsers(dest="asset_cmd", required=True)
    p_add = asub.add_parser("add", help="登记一个逻辑资产（文件或整个目录）")
    p_add.add_argument("path")
    p_add.add_argument("--kind", required=True, choices=list(assets.KINDS),
                       help="; ".join(f"{k}: {v}" for k, v in assets.KINDS.items()))
    p_add.add_argument("--name", help="清单中的名称，默认取文件名")
    p_add.add_argument("--source", help="public：原始地址（https://… 或 hf://models/<repo>）")
    p_add.add_argument("--revision", help="public：原始来源的版本")
    p_add.add_argument("--command", help="generated：重新生成的命令")
    p_add.add_argument("--private", action="store_true", help="不可外传（shareable = false）")
    p_verify = asub.add_parser("verify", help="重算摘要；HF 资产会列出改动的文件")
    p_verify.add_argument("name")
    asub.add_parser("list", help="列出清单")
    args = parser.parse_args(argv)

    if args.cmd == "check" and (args.staged or args.ref or args.since):
        if args.staged and args.since:
            parser.error("--staged 不能与 --since 一起使用")
        from keel.snapshots import check_git
        findings = check_git(Path.cwd(), staged=args.staged, ref=args.ref, since=args.since)
        return print_findings(findings, args.json)

    if args.cmd == "init":
        root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                                   text=True).stdout.strip() or ".").resolve()
        for line in init_project(root, args.prefix, not args.no_commit, args.github):
            print(line)
        return 0

    if args.cmd == "secret" and args.secret_cmd == "set":
        value = "" if sys.stdin.isatty() else sys.stdin.read().rstrip("\n")
        if not value and args.note is None:
            raise SystemExit("从标准输入给值，例如 pbpaste | keel secret set <名称>；只改说明用 --note")
        if value:
            print(secrets.set_secret(args.name, value))
        if args.note is not None:
            secrets.describe(args.name, args.note)
            print(f"已更新 {args.name} 的说明")
        return 0
    if args.cmd == "secret" and args.secret_cmd == "list" and args.all:
        print("\n".join(secrets.inventory()))
        return 0
    project = load_project(Path.cwd())
    if args.cmd == "local":
        from keel import runtime
        if args.local_cmd == "sweep":
            lines = runtime.sweep(project.root, project.config, args.daily)
        elif args.local_cmd == "run":
            lines = runtime.transition(project.root, args.path, args.state, args.pid, args.reason, args.review_on)
        elif args.local_cmd == "status":
            lines = runtime.status(project.root)
        else:
            days, _ = runtime.policy(project.config)
            lines = runtime.clean(project.root, args.paths, days if args.older_than is None else args.older_than, args.apply)
        print("\n".join(lines))
        return 0
    if args.cmd in ("check", "status"):
        from keel import runtime
        try:
            maintenance = runtime.sweep(project.root, project.config, daily=True)
        except (runtime.LocalError, OSError) as error:
            maintenance = [f"运行产物检查未执行：{error}"]
        for line in maintenance:
            print(line, file=sys.stderr)
    if args.cmd == "types":
        for name, spec in project.types.items():
            print(f"{name:10} {NOTES_ROOT}/{spec['dir']:12} {spec['question']}")
        return 0
    if args.cmd == "status":
        from keel import status
        root = project.history_root or project.root
        notes = [(str(n.meta.get("date", "")), str(n.meta.get("type", "")), str(n.meta.get("status", "")),
                  n.path.relative_to(project.root).as_posix()) for n in collect_notes(project) if n.meta]
        print("\n".join(status.render(status.load_issues(root), notes, status.load_tags(root))))
        return 0
    if args.cmd == "sync":
        done = skills.sync(project.root, project.config, __version__, args.force)
        if "keel" in project.config.get("skills", []):
            done += install_agents_pointer(project.root)
        print("\n".join(done) or "共享 skill 已是最新")
        return 0
    if args.cmd == "skills":
        for name, path in skills.bundled().items():
            meta, _ = skills.parse_skill(path / "SKILL.md")
            print(f"{name:20} {str(meta.get('description', ''))[:80]}")
        return 0
    if args.cmd == "secret":
        if args.secret_cmd == "import":
            print("\n".join(secrets.import_dotenv(Path(args.path), args.only)))
        elif args.secret_cmd == "use":
            mappings = {**secrets.declared(project.config), **secrets.declared({"secrets": args.names})}
            names = [n if s == n else f"{n}={s}" for n, s in mappings.items()]
            text = (project.root / CONFIG_NAME).read_text()
            line = "secrets = [" + ", ".join(json.dumps(n) for n in names) + "]"
            table = re.search(r"^\s*\[", text, re.M)
            at = table.start() if table else len(text)
            top, n = re.subn(r"^secrets\s*=\s*\[[^\]]*\]", lambda _: line, text[:at], flags=re.M)
            (project.root / CONFIG_NAME).write_text((top if n else line + "\n" + top) + text[at:])
            print("\n".join(secrets.init(project.root, tomllib.loads((project.root / CONFIG_NAME).read_text())))
                  or "已声明")
        else:
            print("\n".join(secrets.status(project.root, project.config)))
        return 0
    if args.cmd == "doctor":
        secrets.declared(project.config)  # 配置错误先报错，避免不必要的远端查询。
        lock_version = skills.load_lock(project.root).get("keel_version")
        lines = [(True, f"keel {__version__}" + (f"（项目上次 sync 用 {lock_version}）" if lock_version else ""))]
        lines += assets.doctor(project.root, project.config)
        lines += secrets.doctor(project.root, project.config)
        for ok, line in lines:
            print(("✓ " if ok else "✗ ") + line)
        return 0 if all(ok for ok, _ in lines) else 1
    store = assets.Store(project.root, project.config)
    if args.cmd == "fetch":
        results = assets.fetch(store, args.names)
        for name, status, message in results:
            print(f"{status:8} {name}: {message}")
        return 0 if all(s in ("ok", "fetched") for _, s, _ in results) else 1
    if args.cmd == "asset":
        if args.asset_cmd == "add":
            lines = assets.add(store, args.path, args.kind, args.name, args.source, args.revision,
                               args.command, not args.private)
        elif args.asset_cmd == "verify":
            lines = assets.verify(store, args.name)
        else:
            lines = [f"{n:24} {e['kind']:11} {e['path']}  {assets.human(e['bytes'])}  {e.get('source') or ''}"
                     for n, e in store.load().items()] or ["清单为空"]
        print("\n".join(lines))
        return 0
    if args.cmd == "new":
        path = new_note(project, args.type, args.title, args.slug, args.beads,
                        args.area, args.protocol, args.cause)
        print(path.relative_to(project.root))
        return 0
    return print_findings(check(project), args.json)


def print_findings(findings: list[Finding], as_json: bool) -> int:
    if as_json:
        print(json.dumps([f.__dict__ for f in findings], ensure_ascii=False, indent=2))
    else:
        for f in findings:
            print(f, file=sys.stderr if f.level == "error" else sys.stdout)
        errors = sum(f.level == "error" for f in findings)
        print(f"keel check：{errors} 个错误，{len(findings) - errors} 个提醒")
    return 1 if any(f.level == "error" for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
