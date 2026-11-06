"""A narrow local adapter. Run with the interpreter of the installed Keel."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import json
import hashlib
import os
import re
import signal
import subprocess
import sys
import tomllib


def now():
    return datetime.now(timezone.utc).isoformat()


def run(root, args, timeout=18):
    proc = subprocess.Popen(args, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True,
                            env={**os.environ, "BD_NON_INTERACTIVE": "1", "NO_COLOR": "1"})
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise ValueError("读取超时，请稍后重试") from None
    if proc.returncode:
        raise ValueError("工具暂时无法读取此来源，请稍后重试")
    return out


def text(value):
    return value.strip() if isinstance(value, str) else ""


def plain(value, limit=320):
    value = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text(value))
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value.replace("**", "").replace("`", "")).strip()[:limit]


def issues(raw):
    if not isinstance(raw, list):
        raise ValueError("任务来源格式有误，请重试或让 Agent 检查")
    result = []
    for item in raw:
        if not isinstance(item, dict) or not text(item.get("id")) or not text(item.get("title")):
            raise ValueError("任务来源含不完整记录，暂时无法确认状态")
        if item.get("status") not in {"open", "in_progress", "blocked", "deferred", "closed", "pinned", "hooked"}:
            raise ValueError("任务来源状态无法识别，暂时无法确认状态")
        result.append({key: text(item.get(key)) for key in ["id", "title", "status", "issue_type", "description", "acceptance_criteria", "close_reason", "closed_at", "updated_at"]} | {
            "labels": [v for v in item.get("labels", []) if isinstance(v, str)] if isinstance(item.get("labels"), list) else [],
        })
        result[-1]["revision"] = fingerprint({key: result[-1][key] for key in ["title", "description", "acceptance_criteria", "status", "close_reason"]})
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def explain(root, data):
    """Optional agent-written presentation, valid only for the exact source revision."""
    path = root / ".agents/skills/moi-keel/presentation.json"
    if not path.exists():
        return data
    try:
        copies = json.loads(path.read_text())
        if not isinstance(copies, dict):
            raise ValueError("说明文件格式有误")
        rows = [(f"note:{r['path']}", r) for r in data["reports"]]
        if data["tasks"]:
            for kind in ["active", "human", "blocked", "ready", "milestones"]:
                rows += [(f"beads:{r['id']}", r) for r in data["tasks"][kind]]
        for key, row in rows:
            copy = copies.get(key)
            if not isinstance(copy, dict):
                continue
            if copy.get("revision") != row["revision"]:
                row["explanation_stale"] = True
                continue
            title, summary = text(copy.get("title")), text(copy.get("summary"))
            if title and len(title) <= 120 and len(summary) <= 500:
                row["display_title"], row["display_summary"] = title, summary
    except (ValueError, OSError):
        data["errors"].append({"source": "通俗说明", "message": "通俗说明暂时无法读取，当前显示原始记录"})
    return data


def query(root, *args, timeout=18):
    return issues(json.loads(run(root, ["bd", "--readonly", *args, "--json"], timeout=timeout)))


def discover_projects(root):
    """Existing registry plus the OS index; never crawl the disk or write a project list."""
    from keel.secrets import registry_path
    root = root.resolve()
    candidates, warnings = [root], []
    try:
        registry = registry_path()
        if registry.exists():
            candidates += [Path(line) for line in registry.read_text().splitlines() if line.strip()]
    except OSError:
        warnings.append("本机项目登记暂时无法读取")
    try:
        indexed = run(root, ["mdfind", "-onlyin", str(Path.home()), 'kMDItemFSName == "keel.toml"'], timeout=5)
        candidates += [Path(line).parent for line in indexed.splitlines() if Path(line).name == "keel.toml"]
    except (ValueError, OSError):
        warnings.append("本机文件索引暂时不可用，当前仅显示已知项目")
    found = set()
    for candidate in candidates:
        if not candidate.is_absolute() or set(candidate.parts) & {".git", "node_modules", ".venv", ".local"}:
            continue
        try:
            found.add(candidate.resolve())
        except (OSError, RuntimeError):
            warnings.append("有项目地址暂时无法确认")
    ordered = sorted(found, key=lambda p: (p != root, str(p).casefold()))
    if len(ordered) > 24:
        warnings.append("项目较多，本次只显示前 24 个")
    return ordered[:24], warnings


def machine_project(path, current):
    row = {"name": path.name, "path": str(path), "current": path == current,
           "state": "unavailable", "tasks": None, "sync_version": "", "error": ""}
    try:
        config = path / "keel.toml"
        if not config.is_file():
            row["error"] = "目录或接入配置已不存在"
            return row
        if config.is_symlink() or config.stat().st_size > 160_000:
            raise ValueError("接入配置无法读取")
        tomllib.loads(config.read_text())
        row["state"] = "connected"
        lock = path / "keel.lock"
        if lock.is_file() and not lock.is_symlink() and lock.stat().st_size <= 160_000:
            try:
                row["sync_version"] = text(tomllib.loads(lock.read_text()).get("keel_version"))
            except (ValueError, OSError):
                row["error"] = "同步版本暂时无法读取"
        if not (path / ".beads").is_dir():
            row["task_error"] = "尚未接入任务记录"
            return row
        try:
            tasks = query(path, "list", "--all", "--limit", "0", timeout=8)
            pending = [t for t in tasks if t["status"] != "closed" and t["issue_type"] != "epic"]
            active = [t for t in pending if t["status"] == "in_progress"]
            updates = []
            for task in tasks:
                try:
                    stamp = datetime.fromisoformat(task["updated_at"].replace("Z", "+00:00"))
                    if stamp.tzinfo:
                        updates.append(stamp)
                except ValueError:
                    pass
            row["tasks"] = {"open": len(pending), "active": len(active),
                            "human": sum(t["status"] != "closed" and "human" in t["labels"] for t in tasks),
                            "blocked": sum(t["status"] == "blocked" for t in pending),
                            "updated_at": max(updates).isoformat() if updates else ""}
        except (ValueError, OSError, TypeError):
            row["task_error"] = "任务记录暂时无法读取，可刷新重试"
    except (ValueError, OSError):
        row["state"] = "invalid"
        row["error"] = "接入配置暂时无法读取，请让 Agent 检查"
    return row


def machine_usage(root):
    from keel import __version__
    paths, warnings = discover_projects(root)
    with ThreadPoolExecutor(max_workers=6) as pool:
        rows = list(pool.map(lambda p: machine_project(p, root), paths))
    return {"captured_at": now(), "version": __version__, "projects": rows, "warnings": warnings,
            "scope": "来自当前项目、本机项目登记和文件索引；未被索引的项目可能未列出。"}


def task_summary(all_tasks, ready, blocked):
    active = [i for i in all_tasks if i["status"] == "in_progress" and i["issue_type"] != "epic"]
    human = [i for i in all_tasks if i["status"] != "closed" and "human" in i["labels"]]
    milestones = sorted((i for i in all_tasks if i["status"] == "closed" and i["issue_type"] == "epic"),
                        key=lambda i: i["closed_at"], reverse=True)[:4]
    return {"active": active, "human": human, "blocked": blocked,
            "ready": [i for i in ready if i["issue_type"] != "epic"][:5],
            "milestones": milestones, "open_count": sum(i["status"] != "closed" and i["issue_type"] != "epic" for i in all_tasks)}


def read_note(root, name):
    if not isinstance(name, str) or not name or len(name) > 500:
        raise ValueError("资料地址无效")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "docs" or relative.suffix != ".md":
        raise ValueError("只能查看项目资料内的 Markdown")
    docs = root / "docs"
    path = root / relative
    if docs.is_symlink() or not path.resolve().is_relative_to(docs.resolve()):
        raise ValueError("资料地址超出项目范围")
    if not path.is_file() or path.stat().st_size > 160_000:
        raise ValueError("这份资料暂时无法直接展开，请让 Agent 解释")
    from keel.cli import parse_frontmatter
    meta, body, error = parse_frontmatter(path.read_text())
    if error:
        raise ValueError("资料格式暂时无法识别")
    return meta, body


def reports(root):
    result = []
    for path in sorted((root / "docs" / "reports").glob("*.md")):
        name = path.relative_to(root).as_posix()
        meta, body = read_note(root, name)
        if meta.get("type") != "report" or meta.get("status") != "final":
            continue
        title = re.search(r"^# (.+)$", body, re.M)
        conclusion = re.search(r"^## .*结论.*\n+([\s\S]*?)(?=\n## |\Z)", body, re.M)
        paragraphs = (conclusion.group(1) if conclusion else body).strip().split("\n\n")
        summary = next((plain(p) for p in paragraphs if p.strip() and not p.startswith("#")), "打开依据查看实测结果")
        result.append({"path": name, "title": plain(title.group(1)) if title else path.stem,
                       "date": text(meta.get("date")), "summary": summary, "revision": fingerprint(body)})
    return sorted(result, key=lambda r: (r["date"], r["path"]), reverse=True)[:4]


def overview(root):
    data = {"captured_at": now(), "project": root.name, "tasks": None, "reports": [], "errors": []}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(query, root, *args) for args in [
            ("list", "--all", "--limit", "0"), ("ready", "--limit", "20"), ("blocked",),
        ]]
        try:
            data["tasks"] = task_summary(*(f.result() for f in futures))
        except (ValueError, OSError, TypeError) as e:
            data["errors"].append({"source": "任务", "message": str(e)})
    try:
        data["reports"] = reports(root)
    except (ValueError, OSError) as e:
        data["errors"].append({"source": "成果资料", "message": str(e)})
    data["captured_at"] = now()
    return explain(root, data)


def dispatch(root, action, arg=None):
    if action == "overview":
        return overview(root)
    if action == "machine":
        return machine_usage(root)
    if action == "note":
        _, body = read_note(root, arg)
        row = {"path": arg, "revision": fingerprint(body)}
        explain(root, {"reports": [row], "tasks": None, "errors": []})
        assets = []
        for label, link in re.findall(r"!?\[([^\]]*)\]\(([^)]+)\)", body):
            path = (root / arg).parent / link
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"} and path.is_file() and path.resolve().is_relative_to(root):
                assets.append({"title": label or "成果截图", "path": path.resolve().relative_to(root).as_posix()})
        return {"title": row.get("display_title", "成果依据"), "summary": row.get("display_summary", "原始实测记录和证据如下，可让 Agent 结合当前情况解释。"), "body": body[:40_000], "source": arg, "assets": assets[:4]}
    if action == "task":
        if not isinstance(arg, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", arg):
            raise ValueError("任务地址无效")
        rows = query(root, "show", arg)
        if len(rows) != 1 or rows[0]["id"] != arg:
            raise ValueError("没有找到这项任务")
        task = rows[0]
        explain(root, {"reports": [], "tasks": {"active": [task], "human": [], "blocked": [], "ready": [], "milestones": []}, "errors": []})
        return {"title": task.get("display_title", task["title"]), "summary": task.get("display_summary", plain(task["description"] or task["close_reason"])), "body": "\n\n".join(filter(None, [task["description"], task["acceptance_criteria"], task["close_reason"]])), "source": f"Beads · {arg}"}
    if action == "check":
        from keel.cli import load_project, check
        # Calling the check API bypasses the CLI's automatic daily sweep.
        findings = check(load_project(root))
        errors = sum(f.level == "error" for f in findings)
        warnings = len(findings) - errors
        summary = f"发现 {errors} 项需要处理的问题。" if errors else "项目结构检查通过。"
        summary += f"另有 {warnings} 项提醒，可展开查看依据。" if warnings else "没有额外提醒。"
        return {"title": "项目检查", "summary": summary, "ok": errors == 0, "body": f"{errors} 项错误，{warnings} 项提醒。\n\n" + ("\n".join(str(f) for f in findings) or "项目检查通过。"), "source": "已安装 Keel · check API", "captured_at": now()}
    if action == "local":
        from keel.runtime import status, base_dir
        body = "\n".join(status(root))
        base = base_dir(root)
        entries = [p.name for p in sorted(base.iterdir()) if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")] if base.exists() else []
        size = re.search(r"文件总大小 ([^（]+)", body)
        return {"title": "本机空间", "summary": f"项目运行文件共占用约 {size.group(1).strip() if size else '0 B'}。可以选择目录查看清理预览，文件会继续保留。", "body": body, "entries": entries, "source": "本机运行产物", "captured_at": now()}
    if action == "preview":
        if not isinstance(arg, str) or not arg or arg.startswith("."):
            raise ValueError("请选择一个明确的运行目录")
        from keel.runtime import clean, policy
        from keel.cli import load_project
        days, _ = policy(load_project(root).config)
        return {"title": "清理预览", "summary": "预览已生成，本次没有删除任何文件。展开依据查看哪些内容可以清理或需要保留。", "body": "\n".join(clean(root, [arg], days=days, apply=False)), "source": "Keel · 仅预览，不删除", "captured_at": now()}
    if action == "doctor":
        # Keep diagnostics as a fixed command, with a bounded process group.
        proc = subprocess.Popen(["keel", "doctor"], cwd=root, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ValueError("连接检查超时，请稍后重试") from None
        if proc.returncode not in (0, 1) or not out.strip():
            raise ValueError("连接检查未能执行，请让 Agent 检查环境")
        return {"title": "连接检查", "summary": "工具、账号和存储连接检查通过。" if proc.returncode == 0 else "部分连接需要处理，展开依据查看具体问题。", "ok": proc.returncode == 0, "body": out[-24000:], "source": "Keel · 工具与账号连接", "captured_at": now()}
    raise ValueError("不支持此操作")


def main():
    try:
        request = json.loads(sys.stdin.read(4096))
        if not isinstance(request, dict):
            raise ValueError("请求格式无效")
        root = Path.cwd().resolve()
        if not (root / "keel.toml").is_file():
            raise ValueError("当前目录尚未接入 Keel")
        result = dispatch(root, request.get("action"), request.get("arg"))
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
    except (ValueError, OSError, SystemExit, ImportError) as e:
        print(json.dumps({"ok": False, "error": str(e) or "暂时无法完成，请重试"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
