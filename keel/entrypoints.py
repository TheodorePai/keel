"""Agent 入口迁移：只删除已核实的 Beads 默认文本，未知内容交给用户处理。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


# bd 1.3.2 的两种 minimal 模板（是否包含 Dolt 同步步骤）及 Codex 生成块。
# 校验正文的完整摘要，不因有 BEGIN/END 标记就认定里面没有用户规则。
BEADS_BODIES = {
    "1105d64605151672f5a7fcdc662a575b71366599c39bb96461ecf9cfa34578bb",
    "46cd31e7c400d4ca8e14da6339a23f8f5519ba42e86888043b1dcabf3347d1c7",
    "1c6eedb8db3298668df46399c47e04a055b6f016ae528cc776643b98c93a1c5c",
}
BEADS_BLOCK = re.compile(
    r"^<!-- BEGIN BEADS (?P<kind>INTEGRATION|CODEX SETUP)[^\n]*-->\n"
    r"(?P<body>.*?)^<!-- END BEADS (?P=kind) -->[ \t]*(?:\n|$)", re.M | re.S)
AGENTS_SCAFFOLD_LENGTH = 1916
AGENTS_SCAFFOLD_HASH = "bb0ffb2162a220014252fa2e15884ef387a03a870d46751ef955c261f4f936ac"
CLAUDE_SCAFFOLD_HASH = "07d88b819739ae54de998fcaf14e331cc3fe7b98741f65253f8e52fc3ee11e4f"

GIT_AUTHORITY = (
    "完成当前任务的可验证里程碑并通过项目要求的检查后，主动进行本地 Git 提交，"
    "只提交本次任务的改动，不夹带已有或他人的改动。"
    "Git 推送和 Dolt 远端同步按用户、上层指令或项目规则的已有授权执行；"
    "未约定目标或范围时再确认。已有授权无需重复确认；用户或项目另有限制时遵从其约定。")
LEGACY_GIT_AUTHORITIES = (
    "Git 提交、推送和 Dolt 远端同步须有用户、上层指令或项目规则授权。",
    "未经用户明确要求，不执行 Git 提交、推送或 Dolt 远端同步。",
)
BEADS_POINTER = "任务跟踪按 `.agents/skills/beads/SKILL.md` 执行；上下文缺失时运行 `bd prime`。"
WORK_MODE_TARGET = ".agents/skills/keel/references/work-mode.md"
KNOWLEDGE_RULE = (
    "工作验证或改变了以后还要用的事实时，结束前更新相应笔记与入口，或引用其所属来源；"
    "结论不只留在任务记录或临时目录。收尾按[知识收尾](.agents/skills/keel/references/knowledge-upkeep.md)核对。")
LEGACY_WORK_MODE_POINTER = (
    "按当前需求开展工作，只加载相关说明。AGENTS.md 保留职责、边界和必要的 skill 调用条件；"
    "具体流程、资料与脚本各有归属。接入项目或维护这些约定时，读取"
    f"[工作模式]({WORK_MODE_TARGET})。")
SKILL_CAPTURE_RULE = (
    "遇到经实际验证、值得复用的操作流程或常见错误处理方法时，主动补充已有 skill；"
    "没有合适的再创建项目 skill。能用代码、测试或检查防止复发的，同时落实。")
WORK_MODE_POINTER = (
    "按当前需求开展工作，只加载相关说明。AGENTS.md 保留职责、边界和必要的 skill 调用条件；"
    "具体流程、资料与脚本各有归属。\n\n"
    f"- {SKILL_CAPTURE_RULE}提炼与验收按[工作模式]({WORK_MODE_TARGET})执行；"
    "接入项目或维护这些约定时也读取该说明。\n"
    f"- {KNOWLEDGE_RULE}")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def with_git_authority(text: str) -> str:
    """只迁移独立的已知默认条款；保留项目定制、代码示例及注释。"""
    protected = re.compile(r"<!--.*?-->|^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*(?:\n|$)", re.M | re.S)
    visible = protected.sub(lambda m: re.sub(r"[^\n]", " ", m[0]), text)
    rules = "|".join(re.escape(rule) for rule in (GIT_AUTHORITY, *LEGACY_GIT_AUTHORITIES))
    matches = list(re.finditer(rf"^(?P<prefix>- )?(?:{rules})[ \t]*$", visible, re.M))
    if not matches:
        return text.rstrip() + "\n\n" + GIT_AUTHORITY + "\n"
    for index, match in reversed(list(enumerate(matches))):
        replacement = (match["prefix"] or "") + GIT_AUTHORITY if index == 0 else ""
        text = text[:match.start()] + replacement + text[match.end():]
    return text


def install_git_authority(root: Path) -> list[str]:
    path = root / "AGENTS.md"
    original = path.read_text()
    updated = with_git_authority(original)
    if updated == original:
        return []
    path.write_text(updated)
    return ["AGENTS.md 已更新本地提交与远端同步的授权约定"]


def without_beads_defaults(text: str, name: str) -> str:
    def remove(match: re.Match) -> str:
        if digest(match["body"].strip()) not in BEADS_BODIES:
            raise SystemExit(f"{name} 的 Beads 生成块含未知或修改过的内容；原文未改动，"
                             "请将自定义规则移出生成块后重试 keel init")
        return ""

    cleaned = BEADS_BLOCK.sub(remove, text)
    if re.search(r"<!-- (?:BEGIN|END) BEADS (?:INTEGRATION|CODEX SETUP)", cleaned):
        raise SystemExit(f"{name} 的 Beads 生成标记不完整；原文未改动，请手动整理后重试 keel init")
    return cleaned


def prepare(root: Path) -> dict[str, str]:
    """先验证两个入口，调用者在任何初始化副作用之前执行。"""
    texts = {}
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"{name} 必须是普通文件；已有内容未改动")
        try:
            original = path.read_text()
        except UnicodeError as e:
            raise SystemExit(f"{name} 必须是文本文件；原文未改动") from e
        cleaned = without_beads_defaults(original, name)
        if name == "CLAUDE.md":
            if cleaned not in ("@AGENTS.md", "@AGENTS.md\n"):
                scaffold = re.sub(r"\n{3,}", "\n\n", cleaned.strip())
                if cleaned == original or digest(scaffold) != CLAUDE_SCAFFOLD_HASH:
                    raise SystemExit("CLAUDE.md 必须只含一行 @AGENTS.md；已有内容未改动，"
                                     "请将规则迁入 AGENTS.md 后重试 keel init")
            cleaned = "@AGENTS.md\n"
        elif cleaned != original:
            # 默认 AGENTS 前言可以后接项目规则；只去掉精确匹配的前缀。
            prefix = cleaned.lstrip()[:AGENTS_SCAFFOLD_LENGTH]
            if digest(prefix) == AGENTS_SCAFFOLD_HASH:
                cleaned = "# AGENTS.md\n\n" + cleaned.lstrip()[AGENTS_SCAFFOLD_LENGTH:].lstrip()
            cleaned = cleaned.rstrip() + "\n"
        texts[name] = cleaned
    return texts


def install_migrations(root: Path, texts: dict[str, str]) -> list[str]:
    done = []
    for name, text in texts.items():
        path = root / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            done.append(f"已迁移 {name} 的 Beads 默认入口；保留项目规则")
    return done


def with_knowledge_rule(text: str) -> str:
    """在 skill 提炼条款后补知识收尾条款；找不到时追加到末尾。"""
    if KNOWLEDGE_RULE in text or "references/knowledge-upkeep.md" in text:
        return text
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if SKILL_CAPTURE_RULE in line:
            lines.insert(i + 1, "- " + KNOWLEDGE_RULE)
            return "\n".join(lines)
    return text.rstrip() + "\n\n- " + KNOWLEDGE_RULE + "\n"
