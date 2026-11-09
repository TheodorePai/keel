"""管理本机运行产物的生命周期与安全回收。"""

from __future__ import annotations

import datetime as dt
import stat
from contextlib import contextmanager, nullcontext, ExitStack
import json
import math
import os
import re
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from keel.assets import Store, human
from keel.layout import LOCAL_PATH

if os.name == "nt":
    from keel import _runtime_windows as _windows
else:
    import fcntl
    _windows = None

MARKERS = {".keep", ".active"}
STATE = ".run.json"
STATES = {"running", "completed", "disposable", "retained"}
RECENT_SECONDS = 86400
AUDIT_BYTES = 256 * 1024
AUDIT_ENTRIES = 200


class LocalError(SystemExit):
    pass


def is_redirect(path: Path) -> bool:
    return _windows.is_redirect(path) if _windows else path.is_symlink()


def directory_guard(path: Path):
    return _windows.directory_guard(path) if _windows else nullcontext()


def mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False):
    if _windows:
        _windows.mkdir(path, parents=parents, exist_ok=exist_ok)
    else:
        path.mkdir(parents=parents, exist_ok=exist_ok)


def relative_path(name: str) -> Path:
    path = Path(name)
    first = path.parts[0] if path.parts else ""
    if _windows:
        first = first.casefold()
    if (path.anchor or not path.parts or ".." in path.parts or first == ".keel"
            or (_windows and any(":" in part or part.endswith((".", " "))
                                 or Path(part).is_reserved() for part in path.parts))):
        raise LocalError("只接受 .local 内明确的相对路径，不能使用盘符、管理目录或 ..")
    return path


def descendants(path: Path):
    """Enumerate without entering symbolic links or Windows reparse points."""
    with directory_guard(path):
        for child in path.iterdir():
            yield child
            if not is_redirect(child) and child.is_dir():
                yield from descendants(child)


@dataclass
class Usage:
    size: int
    latest: float
    protected: bool


def measure(path: Path) -> Usage:
    """不跟随软链接；mtime 仅用于近期写入保护，不决定完成后的保留期。"""
    info = path.lstat()
    directory = stat.S_ISDIR(info.st_mode) and not is_redirect(path)
    size = 0 if directory else info.st_size
    latest, protected = (0 if directory or path.name == STATE else info.st_mtime), path.name in MARKERS
    if directory:
        with directory_guard(path):
            for child in path.iterdir():
                usage = measure(child)
                size += usage.size
                latest = max(latest, usage.latest)
                protected = protected or usage.protected
    return Usage(size, latest, protected)


def base_dir(root: Path) -> Path:
    base = root / LOCAL_PATH
    if is_redirect(root / "work") or is_redirect(base):
        raise LocalError(f"拒绝操作软链接运行目录：{LOCAL_PATH}")
    if base.exists() and not base.is_dir():
        raise LocalError(f"{LOCAL_PATH} 应是目录")
    return base


def status(root: Path) -> list[str]:
    base = base_dir(root)
    if not base.exists():
        return [f"{LOCAL_PATH}/：0 B（不存在）"]
    lines, total = [], 0
    try:
        for path in sorted(base.iterdir()):
            usage = measure(path)
            total += usage.size
            suffix = "（保留标记）" if usage.protected else ""
            if is_redirect(path):
                suffix = "（软链接，未扫描目标）"
            lines.append(f"{human(usage.size):>10}  {path.name}{suffix}")
    except OSError as e:
        raise LocalError(f"无法读取运行产物：{e}") from e
    return [f"{LOCAL_PATH}/：文件总大小 {human(total)}（不跟随软链接）", *lines, *notices(base)]


def target_path(base: Path, name: str) -> Path:
    relative = relative_path(name)
    path = base
    for part in relative.parts:
        path /= part
        if is_redirect(path):
            raise LocalError(f"拒绝清理软链接路径：{name}")
    if not path.exists():
        raise LocalError(f"运行产物不存在：{name}")
    return path


def retained_paths(root: Path) -> list[Path]:
    entries = Store(root, {}).load()
    paths = []
    for entry in entries.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not entry["path"]:
            raise LocalError("资产清单路径无效，未清理；先运行 keel check 修复")
        paths.append((root / entry["path"]).resolve())
    tracked = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--", LOCAL_PATH],
                             capture_output=True, text=True)
    if tracked.returncode != 0:
        raise LocalError("无法确认 Git 跟踪状态，未清理")
    paths += [(root / p).resolve() for p in filter(None, tracked.stdout.split("\0"))]
    return paths


def metadata_dir(root: Path) -> Path:
    base = base_dir(root)
    mkdir(base, parents=True, exist_ok=True)
    path = base / ".keel"
    if is_redirect(path):
        raise LocalError("Keel 本地管理目录是软链接，未清理")
    try:
        mkdir(path)
    except FileExistsError:
        if not path.is_dir() or is_redirect(path / "manager.json"):
            raise LocalError("Keel 本地管理目录无效，未清理")
        try:
            if read_json(path / "manager.json") != {"version": 1}:
                raise ValueError("manager version")
        except (OSError, ValueError) as e:
            raise LocalError("已有 .local/.keel 状态不明，未清理") from e
    else:
        write_json(path / "manager.json", {"version": 1})
    return path


@contextmanager
def local_lock(root: Path):
    """状态变更、每日判定与删除共享进程锁；忙时只提示。"""
    try:
        metadata = metadata_dir(root)
    except OSError as e:
        raise LocalError(f"无法安全访问运行产物管理目录，未清理：{e}") from e
    if _windows:
        with ExitStack() as stack:
            try:
                stack.enter_context(_windows.exclusive_lock(metadata / "lock"))
            except OSError as e:
                raise LocalError(f"其他 Keel 正在处理运行产物或无法安全锁定，本次停止：{e}") from e
            yield metadata
        return
    try:
        fd = os.open(metadata / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise LocalError(f"无法锁定运行产物，未清理：{e}") from e
    with os.fdopen(fd, "a") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_nlink != 1:
            raise LocalError("本地管理锁无效，未清理")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise LocalError("其他 Keel 正在处理运行产物或无法取得锁，本次跳过") from e
        yield metadata


def transition(root: Path, name: str, state: str, pid: int | None = None,
               reason: str | None = None, review_on: str | None = None) -> list[str]:
    with local_lock(root), ExitStack() as guards:
        return _transition(root, name, state, pid, reason, review_on, guards=guards)


def clean(root: Path, names: list[str], days: int = 30, apply: bool = False) -> list[str]:
    with local_lock(root):
        return _clean(root, names, days, apply)


def policy(config: dict) -> tuple[int, int | None]:
    local = config.get("local", {})
    if not isinstance(local, dict) or set(local) - {"retention_days", "budget_mb"}:
        raise LocalError("[local] 只接受 retention_days 和 budget_mb")
    days, budget = local.get("retention_days", 30), local.get("budget_mb")
    if type(days) is not int or days < 0:
        raise LocalError("local.retention_days 必须是非负整数")
    if budget is not None and (type(budget) not in (int, float) or not math.isfinite(budget) or budget <= 0):
        raise LocalError("local.budget_mb 必须是有限正数")
    budget_bytes = None if budget is None else budget * 1024 * 1024
    if type(budget_bytes) is float and not math.isfinite(budget_bytes):
        raise LocalError("local.budget_mb 超出可表示范围")
    return days, None if budget_bytes is None else int(budget_bytes)


def candidates(base: Path) -> list[str]:
    """只发现声明过的目录；父容器不自动获得删除资格。"""
    found = []
    for directory, dirs, files in os.walk(base, followlinks=False,
                                          onerror=lambda error: (_ for _ in ()).throw(error)):
        path = Path(directory)
        dirs[:] = [d for d in dirs if not is_redirect(path / d) and not (path == base and d == ".keel")]
        if path != base and (path / STATE).exists():
            found.append(path.relative_to(base).as_posix())
            dirs[:] = []
    return found


def sweep(root: Path, config: dict, daily: bool = False) -> list[str]:
    days, budget = policy(config)
    base = base_dir(root)
    if not base.exists():
        return [".local 不存在，无需回收"]
    with local_lock(root) as metadata:
        stamp = metadata / "daily.json"
        now = time.time()
        if is_redirect(stamp):
            raise LocalError("每日检查记录是软链接，未清理")
        if stamp.exists():
            try:
                previous = read_json(stamp)
                last = previous["checked_at"]
                if type(last) not in (int, float) or not math.isfinite(last) or last < 0 or last > now:
                    raise ValueError("time")
            except (OSError, ValueError, TypeError, KeyError) as e:
                raise LocalError("每日检查记录无效或时钟回退，未清理") from e
            if daily and dt.datetime.fromtimestamp(last).date() == dt.datetime.fromtimestamp(now).date():
                return []
        audit = Audit(root)
        names = candidates(base)
        # 在任何删除前核对全局资产/Git 信息；失败不记成功日期，便于修复后重试。
        retained_paths(root)
        audit.add("sweep_started", reason=f"retention_days={days}")
        lines = []
        for name in names:
            lines += _clean(root, [name], days, apply=True)
        for name in unclassified(base, names):
            reason = "状态不明（旧目录或未登记内容），仅提示"
            lines.append(f"保留 {name}：{reason}")
            # 每次 _clean 都会更新同一日志，重新读取以免覆盖前面的删除记录。
            audit = Audit(root)
            audit.add("retained", name, reason=reason)
        lines += notices(base)
        total = measure(base).size
        lines.append(f".local 文件总大小 {human(total)}；保留期 {days} 天")
        if budget is not None and total > budget:
            lines.append(f"提醒：超出容量预算 {human(budget)}，剩余内容受保护；需复查或转存")
        Audit(root).add("sweep_finished", size=total, reason="；".join(lines[-2:]))
        write_json(stamp, {"checked_at": now})
        return lines


def read_bytes(path: Path, limit: int) -> bytes:
    with directory_guard(path.parent):
        fd = (_windows.open_record(path, os.O_RDONLY) if _windows else
              os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("记录必须是普通文件")
            return stream.read(limit)


def read_json(path: Path) -> object:
    raw = read_bytes(path, 65537)
    if len(raw) > 65536:
        raise ValueError("记录过大")
    return json.loads(raw)


class Audit:
    """最多 200 条且不超过 256 KiB；每条是一次决策或删除结果。"""

    def __init__(self, root: Path):
        self.path = metadata_dir(root) / "cleanup.jsonl"
        self.entries = []
        if os.path.lexists(self.path):
            try:
                raw = read_bytes(self.path, AUDIT_BYTES + 1)
                if len(raw) > AUDIT_BYTES:
                    raise ValueError("log too large")
                for line in raw.splitlines():
                    event = json.loads(line)
                    if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                        raise ValueError("invalid event")
                    self.entries.append(line + b"\n")
            except (OSError, ValueError) as e:
                raise LocalError("清理日志无效，未删除；先检查 .local/.keel/cleanup.jsonl") from e

    def add(self, event: str, path: str = "", size: int = 0, reason: str = "") -> None:
        entry = {"at": time.time(), "event": event, "path": path, "file_bytes": size, "reason": reason[:2000]}
        line = (json.dumps(entry, ensure_ascii=False) + "\n").encode()
        if len(line) > AUDIT_BYTES:
            raise LocalError("单条清理记录过大，未删除")
        self.entries.append(line)
        self.entries = self.entries[-AUDIT_ENTRIES:]
        total = sum(map(len, self.entries))
        while total > AUDIT_BYTES:
            total -= len(self.entries.pop(0))
        try:
            write_bytes(self.path, b"".join(self.entries))
        except OSError as e:
            raise LocalError("无法写入清理日志，停止删除") from e


def unclassified(base: Path, managed: list[str]) -> list[str]:
    """合并旧子树的提醒，同时不漏掉受管运行旁边的旧兄弟目录。"""
    found = []
    def visit(directory: Path):
        for child in sorted(directory.iterdir()):
            name = child.relative_to(base).as_posix()
            if child == base / ".keel" or name in managed:
                continue
            if any(item.startswith(name + "/") for item in managed) and child.is_dir() and not is_redirect(child):
                visit(child)
            else:
                found.append(name)
    visit(base)
    return found


def read_state(path: Path) -> dict | None:
    record = path / STATE
    if not record.is_file() or is_redirect(record):
        return None
    try:
        data = read_json(record)
        if not isinstance(data, dict) or type(data.get("version")) is not int or data.get("version") != 1 or data.get("state") not in STATES:
            return None
        for field in ("started_at", "completed_at"):
            value = data.get(field)
            if value is None and field == "completed_at" and data["state"] == "running":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                return None
        if data["state"] != "running" and data["completed_at"] < data["started_at"]:
            return None
        return data
    except (OSError, ValueError, TypeError):
        return None


def write_bytes(path: Path, data: bytes) -> None:
    """原子替换，不通过软链接写入；旧日志不会在失败时被截断。"""
    with directory_guard(path.parent):
        _write_bytes(path, data)


def _write_bytes(path: Path, data: bytes) -> None:
    if is_redirect(path):
        raise LocalError(f"拒绝写入软链接记录：{path.name}")
    temporary = path.with_name(f".{path.name}.pending")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = (_windows.open_record(temporary, flags) if _windows else
              os.open(temporary, flags | os.O_NOFOLLOW, 0o600))
    except FileExistsError as error:
        raise LocalError(f"存在未完成的记录写入 {temporary.name}，需核查后恢复；未删除") from error
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: dict) -> None:
    write_bytes(path, (json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n").encode())


def process_identity(pid: int) -> str | None:
    """使用系统报告的启动时间识别进程；失败时不猜测。"""
    if type(pid) is not int or pid <= 0:
        return None
    if _windows:
        return _windows.process_identity(pid)
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                                capture_output=True, text=True, timeout=5,
                                env={**os.environ, "LC_ALL": "C"})
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def process_exited(pid: int) -> bool | None:
    if _windows:
        return _windows.process_exited(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        pass
    return None


def process_status(owner: object) -> str:
    if (not isinstance(owner, dict) or type(owner.get("pid")) is not int
            or owner["pid"] <= 0 or not isinstance(owner.get("started"), str) or not owner["started"]):
        return "unknown"
    if (_windows and not re.fullmatch(r"win32:\d+", owner["started"])) or (
            not _windows and owner["started"].startswith("win32:")):
        return "unknown"
    current = process_identity(owner["pid"])
    if current is not None:
        return "alive" if current == owner["started"] else "exited"
    return "exited" if process_exited(owner["pid"]) is True else "unknown"


def keep_reason(path: Path) -> str | None:
    marker = path / ".keep"
    if not os.path.lexists(marker):
        return None
    try:
        if is_redirect(marker) or not marker.is_file():
            raise ValueError("not a regular marker")
        data = read_json(marker)
        if not isinstance(data, dict) or not isinstance(data.get("reason"), str) or not data["reason"].strip():
            raise ValueError("missing reason")
        review = dt.date.fromisoformat(data["review_on"])
        due = "复查已到期" if review <= dt.date.today() else "等待复查"
        return f".keep 保留：{data['reason']}（{data['review_on']}，{due}）"
    except (OSError, ValueError, KeyError, TypeError):
        return ".keep 标记缺少有效保留原因或复查日期，需补齐；继续保留"


def notices(base: Path) -> list[str]:
    lines = []
    if not base.exists():
        return lines
    for directory, dirs, files in os.walk(base, followlinks=False):
        path = Path(directory)
        dirs[:] = [d for d in dirs if not is_redirect(path / d) and not (path == base and d == ".keel")]
        if (path / STATE).exists():
            data = read_state(path)
            state = data["state"] if data else "状态不明（无效运行记录）"
            if data and state == "running" and process_status(data.get("owner")) != "alive":
                state = "状态不明（运行进程已退出或无法核对）"
            lines.append(f"运行 {path.relative_to(base).as_posix()}：{state}")
        for reason in (keep_reason(path), marker_reason(path)):
            if reason:
                lines.append(f"提醒 {path.relative_to(base).as_posix()}：{reason}")
    return lines


def marker_reason(path: Path) -> str | None:
    marker = path / ".active"
    if not os.path.lexists(marker):
        return None
    try:
        owner = read_json(marker) if marker.is_file() and not is_redirect(marker) else None
    except (OSError, ValueError):
        owner = None
    state = process_status(owner)
    return {"alive": ".active 标记的进程仍在运行", "exited": "状态不明：.active 标记的进程已退出，需确认中断产物",
            "unknown": "状态不明：无法核对 .active 标记的进程身份"}[state]


def _transition(root: Path, name: str, state: str, pid: int | None = None,
               reason: str | None = None, review_on: str | None = None, *, guards: ExitStack) -> list[str]:
    if state not in STATES:
        raise LocalError("无效运行状态")
    if state == "retained":
        try:
            if not reason or not reason.strip() or len(reason) > 1000:
                raise ValueError("reason")
            dt.date.fromisoformat(review_on)
        except (ValueError, TypeError) as e:
            raise LocalError("保留必须提供原因（1–1000 字符）和复查日期 YYYY-MM-DD") from e
    base = base_dir(root)
    # begin 仅创建全新目录；不能把旧目录自动认领为可丢弃。
    if state == "running":
        relative = relative_path(name)
        mkdir(base, parents=True, exist_ok=True)
        path = base
        for part in relative.parts:
            path /= part
            if is_redirect(path):
                raise LocalError("拒绝软链接运行路径")
        if any((parent / STATE).exists() for parent in path.parents if parent.is_relative_to(base)):
            raise LocalError("运行目录不能嵌套")
        owner_pid = os.getppid() if pid is None else pid
        started = process_identity(owner_pid)
        if started is None:
            raise LocalError("无法读取运行进程启动时间；未创建运行记录")
        mkdir(path, parents=True, exist_ok=False)
        guards.enter_context(directory_guard(path))
        owner = {"pid": owner_pid, "started": started}
        write_json(path / ".active", owner)
        data = {"version": 1, "state": state, "started_at": time.time(), "completed_at": None, "owner": owner}
    else:
        path = target_path(base, name)
        guards.enter_context(directory_guard(path))
        data = read_state(path)
        if data is None:
            raise LocalError("状态不明，不能自动认领旧目录；先转存并检查成果")
        if state == "completed":
            if data["state"] != "running":
                raise LocalError("仅运行中的目录可以标记完成")
            if process_status(data.get("owner")) != "exited":
                raise LocalError("运行进程仍在运行或身份不明，先确认所有服务停止")
            marker = path / ".active"
            if is_redirect(marker):
                raise LocalError("活动标记是软链接，状态不明")
            if marker.exists():
                try:
                    marker_owner = read_json(marker)
                except (OSError, ValueError) as e:
                    raise LocalError("活动标记损坏，需先核查") from e
                if marker_owner != data.get("owner"):
                    raise LocalError("活动标记与运行记录不一致，需先核查")
                marker.unlink()
            data["completed_at"] = time.time()
        elif data["state"] == "running":
            raise LocalError("先确认服务停止并标记 completed，再声明 disposable 或 retained")
        data["state"] = state
    if state == "retained":
        write_json(path / ".keep", {"reason": reason.strip(), "review_on": review_on})
    elif state == "disposable" and data.get("retention_managed"):
        # 只有明确重新声明可丢弃，才解除由本命令创建的保留；旧手写标记不动。
        if is_redirect(path / ".keep"):
            raise LocalError("保留标记是软链接，需先核查")
        (path / ".keep").unlink(missing_ok=True)
    data["retention_managed"] = state == "retained"
    write_json(path / STATE, data)
    return [f"{name}：{state}"]


def eligibility(path: Path, days: int, now: float, usage: Usage) -> str | None:
    data = read_state(path)
    if data is None:
        return "状态不明（没有有效运行记录），仅提示"
    if data["state"] == "running":
        activity = process_status(data.get("owner"))
        return "运行中" if activity == "alive" else "状态不明：运行进程已退出或身份无法核对，需确认中断产物"
    if any(child.name == STATE for child in descendants(path) if child.parent != path):
        return "状态不明：目录内有其他运行记录，需逐次核查"
    if data["state"] != "disposable":
        return f"运行状态为 {data['state']}，未声明可丢弃"
    if data["completed_at"] > now or data["started_at"] > now:
        return "状态不明（记录时间在未来）"
    if data["completed_at"] > now - days * 86400:
        return f"完成后尚未满 {days} 天"
    if usage.latest >= now - RECENT_SECONDS or usage.latest > data["completed_at"]:
        return "最近 24 小时有修改或完成后仍有写入，需重新确认状态"
    if process_status(data.get("owner")) != "exited":
        return "状态不明：原生产进程仍在运行或无法核对，继续保留"
    return None


def protection(root: Path, path: Path, days: int, now: float, usage: Usage) -> str | None:
    base = base_dir(root)
    resolved = path.resolve()
    if any(p.is_relative_to(resolved) or resolved.is_relative_to(p) for p in retained_paths(root)):
        return "仍有登记资产或 Git 跟踪内容，先迁移并更新登记"
    ancestors = [p for p in path.parents if p == base or p.is_relative_to(base)]
    if usage.protected or any(os.path.lexists(p / marker) for p in ancestors for marker in MARKERS):
        directories = [path, *[p for p in descendants(path) if p.is_dir() and not is_redirect(p)], *ancestors]
        reasons = [reason for p in directories for reason in (keep_reason(p), marker_reason(p)) if reason]
        return "；".join(reasons) if reasons else "存在 .keep 或 .active 标记"
    return eligibility(path, days, now, usage)


def tree_identity(path: Path):
    if _windows:
        return _windows.identity(path)
    info = path.lstat()
    return info.st_dev, info.st_ino


def remove_tree(path: Path, identity):
    if _windows:
        _windows.remove_tree(path, identity)
    else:
        if tree_identity(path) != identity:
            raise OSError("清理目标已被替换，停止删除")
        shutil.rmtree(path)


def _clean(root: Path, names: list[str], days: int = 30, apply: bool = False) -> list[str]:
    if type(days) is not int or days < 0:
        raise LocalError("保留天数必须是非负整数")
    base = base_dir(root)
    paths = list(dict.fromkeys(target_path(base, name) for name in names))
    if any(p != other and p.is_relative_to(other) for p in paths for other in paths):
        raise LocalError("清理目标不能互相包含；请选择任务目录或其中的运行目录")
    lines = []
    audit = Audit(root) if apply else None
    try:
        retained_paths(root)  # 在任何删除前验证清单与 Git 均可读取。
        for path in paths:
            name = path.relative_to(base).as_posix()
            identity = tree_identity(path)
            usage = measure(path)
            reason = protection(root, path, days, time.time(), usage)
            if reason:
                lines.append(f"保留 {name}：{reason}")
                if audit:
                    audit.add("retained", name, usage.size, reason)
            elif apply:
                # 先写意图；崩溃或部分删除仍有可追溯记录。随后重新核对保护条件。
                state = read_state(path)
                audit.add("delete_started", name, usage.size)
                target_path(base, name)
                again = measure(path)
                reason = protection(root, path, days, time.time(), again)
                if again != usage or read_state(path) != state or reason:
                    reason = reason or "清理前状态或内容改变，需重新检查"
                    audit.add("retained", name, again.size, reason)
                    lines.append(f"保留 {name}：{reason}")
                    continue
                try:
                    remove_tree(path, identity)
                except OSError as error:
                    audit.add("delete_incomplete", name, reason=f"删除中断，需核查剩余内容：{error}")
                    raise
                audit.add("deleted", name, usage.size)
                lines.append(f"已清理 {name}：{human(usage.size)}")
            else:
                lines.append(f"可清理 {name}：{human(usage.size)}")
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise LocalError(f"无法清理运行产物：{e}") from e
    if not apply:
        lines.append("仅预览；核对成果已转存、服务已停止后，加 --apply 执行清理")
    return lines
