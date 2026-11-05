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

GIT_AUTHORITY = "Git 提交、推送和 Dolt 远端同步须有用户、上层指令或项目规则授权。"
BEADS_POINTER = "任务跟踪按 `.agents/skills/beads/SKILL.md` 执行；上下文缺失时运行 `bd prime`。"
WORK_MODE_TARGET = ".agents/skills/keel/references/work-mode.md"
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
    "接入项目或维护这些约定时也读取该说明。")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


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
    migrated = False
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
        migrated |= cleaned != original
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
    if migrated and GIT_AUTHORITY not in texts.get("AGENTS.md", ""):
        texts["AGENTS.md"] = texts.get("AGENTS.md", "# AGENTS.md\n").rstrip() + "\n\n" + GIT_AUTHORITY + "\n"
    return texts


def install_migrations(root: Path, texts: dict[str, str]) -> list[str]:
    done = []
    for name, text in texts.items():
        path = root / name
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            done.append(f"已迁移 {name} 的 Beads 默认入口；保留项目规则")
    return done
