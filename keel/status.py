"""keel status：把 bd 任务、里程碑（epic）、最近的记录类笔记和 git 标签汇成一页（决策 0005）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

RECORD_TYPES = ("decision", "report", "incident")
RECENT = 5


def load_issues(root: Path) -> list[dict] | None:
    if not (root / ".beads").exists() or not shutil.which("bd"):
        return None
    try:
        out = subprocess.run(["bd", "list", "--all", "--json", "--limit", "0"], cwd=root,
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "BD_NON_INTERACTIVE": "1"})
        return json.loads(out.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def load_tags(root: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "tag", "--sort=-creatordate",
                          "--format=%(refname:short)  %(creatordate:short)"],
                         capture_output=True, text=True)
    return out.stdout.split("\n")[:3] if out.returncode == 0 and out.stdout.strip() else []


def _day(value: str | None) -> str:
    return (value or "")[:10]


def render(issues: list[dict] | None, notes: list[tuple[str, str, str, str]], tags: list[str]) -> list[str]:
    """notes: (date, type, status, 相对路径)。"""
    lines: list[str] = []

    def section(title: str, rows: list[str], empty: str = "无") -> None:
        lines.append(f"## {title}")
        lines.extend(rows or [f"  {empty}"])
        lines.append("")

    if issues is None:
        section("任务", [], "无法读取 bd 任务库")
    else:
        active = [i for i in issues if i.get("status") == "in_progress"]
        blocked = [i for i in issues if i.get("status") == "blocked"]
        epics = [i for i in issues if i.get("issue_type") == "epic"]
        section("进行中", [f"  {i['id']}  {i['title']}" + (f"  ({i['assignee']})" if i.get("assignee") else "")
                         for i in active])
        section("阻塞", [f"  {i['id']}  {i['title']}" for i in blocked])
        section("未完成的里程碑", [f"  {i['id']}  {i['title']}" for i in epics if i.get("status") != "closed"])
        done = sorted((i for i in epics if i.get("status") == "closed"),
                      key=lambda i: i.get("closed_at") or "", reverse=True)[:RECENT]
        section("最近达成的里程碑", [f"  {_day(i.get('closed_at'))}  {i['id']}  {i['title']}"
                                   + (f"\n      {i['close_reason']}" if i.get("close_reason") else "")
                                   for i in done])
        open_count = sum(i.get("status") != "closed" for i in issues)
        lines.append(f"未关闭任务 {open_count} 个，其中进行中 {len(active)}、阻塞 {len(blocked)}；可开始的任务用 bd ready 查看")
        lines.append("")
    recent = sorted((n for n in notes if n[1] in RECORD_TYPES), reverse=True)[:RECENT]
    section("最近的记录", [f"  {d}  {t:8} {s:10} {p}" for d, t, s, p in recent])
    section("最近的标签", [f"  {t}" for t in tags])
    return lines[:-1]
