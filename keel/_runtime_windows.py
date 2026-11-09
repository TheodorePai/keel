"""Windows runtime primitives. Imported only on Windows; no optional dependencies.

Directory handles deny rename/delete while a path is being used. Files are opened
as reparse points and inspected before use; deleting by handle never follows one.
"""

from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
from contextlib import contextmanager, ExitStack
import msvcrt
import os
from pathlib import Path
import stat


class FileInfo(C.Structure):
    _fields_ = [("attributes", W.DWORD), ("created", W.FILETIME),
                ("accessed", W.FILETIME), ("written", W.FILETIME),
                ("volume", W.DWORD), ("size_high", W.DWORD), ("size_low", W.DWORD),
                ("links", W.DWORD), ("index_high", W.DWORD), ("index_low", W.DWORD)]


class Overlapped(C.Structure):
    _fields_ = [("internal", C.c_size_t), ("internal_high", C.c_size_t),
                ("offset", W.DWORD), ("offset_high", W.DWORD), ("event", W.HANDLE)]


class Disposition(C.Structure):
    _fields_ = [("delete", W.BYTE)]


_kernel = C.WinDLL("kernel32", use_last_error=True)


def _api(name, args, result=W.BOOL):
    function = getattr(_kernel, name)
    function.argtypes, function.restype = args, result
    return function


_create = _api("CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p,
                              W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
_close = _api("CloseHandle", [W.HANDLE])
_get_info = _api("GetFileInformationByHandle", [W.HANDLE, C.POINTER(FileInfo)])
_lock = _api("LockFileEx", [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, W.DWORD,
                           C.POINTER(Overlapped)])
_unlock = _api("UnlockFileEx", [W.HANDLE, W.DWORD, W.DWORD, W.DWORD,
                               C.POINTER(Overlapped)])
_set_info = _api("SetFileInformationByHandle", [W.HANDLE, C.c_int, C.c_void_p, W.DWORD])
_open_process = _api("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE)
_process_times = _api("GetProcessTimes", [W.HANDLE, *[C.POINTER(W.FILETIME)] * 4])
_wait = _api("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD)

_ATTR_READ = 0x80
_DELETE = 0x10000
_DIRECTORY = 0x10
_REPARSE = 0x400
_OPEN_FLAGS = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
_INVALID = C.c_void_p(-1).value


def _check(result):
    if not result:
        raise C.WinError(C.get_last_error())


def is_redirect(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except FileNotFoundError:
        return False


@contextmanager
def _handle(path: Path, access: int = _ATTR_READ, creation: int = 3):
    # Sharing read/write permits concurrent status reads and lock contenders.
    # Omitting FILE_SHARE_DELETE pins the opened object to its current name.
    handle = _create(str(path), access, 3, None, creation, _OPEN_FLAGS, None)
    if handle == _INVALID:
        raise C.WinError(C.get_last_error())
    try:
        yield handle
    finally:
        _close(handle)


def _info(handle) -> FileInfo:
    info = FileInfo()
    _check(_get_info(handle, C.byref(info)))
    return info


def _identity(info: FileInfo) -> tuple[int, int]:
    return info.volume, (info.index_high << 32) | info.index_low


def _ordinary(info: FileInfo, *, directory: bool):
    if info.attributes & _REPARSE or bool(info.attributes & _DIRECTORY) != directory:
        raise OSError("拒绝重解析点或不符合预期的文件类型")
    if not directory and info.links != 1:
        raise OSError("运行记录或锁文件不能有多个硬链接")


def _absolute(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path.drive.startswith("\\\\"):
        raise OSError("运行产物管理暂不支持网络或设备路径")
    return path


@contextmanager
def directory_guard(path: Path):
    """Pin every ancestor, rejecting junctions as well as symbolic links."""
    path = _absolute(path)
    with ExitStack() as stack:
        for component in [*reversed(path.parents), path]:
            handle = stack.enter_context(_handle(component))
            _ordinary(_info(handle), directory=True)
        yield


def mkdir(path: Path, *, parents: bool = False, exist_ok: bool = False):
    path = _absolute(path)
    with ExitStack() as stack:
        for component in [*reversed(path.parents), path]:
            if component == path:
                component.mkdir(exist_ok=exist_ok)
            elif parents:
                component.mkdir(exist_ok=True)
            handle = stack.enter_context(_handle(component))
            _ordinary(_info(handle), directory=True)


def open_record(path: Path, flags: int) -> int:
    """Return an owned Python fd for a checked regular file, never a link target."""
    access = _ATTR_READ | (0x40000000 if flags & (os.O_WRONLY | os.O_RDWR) else 0x80000000)
    if flags & os.O_RDWR:
        access |= 0x80000000
    creation = 1 if flags & os.O_EXCL else 4 if flags & os.O_CREAT else 3
    handle = _create(str(_absolute(path)), access, 3, None, creation, _OPEN_FLAGS, None)
    if handle == _INVALID:
        raise C.WinError(C.get_last_error())
    try:
        _ordinary(_info(handle), directory=False)
        fd = msvcrt.open_osfhandle(handle, (flags & (os.O_WRONLY | os.O_RDWR)) | os.O_BINARY)
    except BaseException:
        _close(handle)
        raise
    return fd  # open_osfhandle transfers handle ownership to the fd.


@contextmanager
def exclusive_lock(path: Path):
    with directory_guard(path.parent):
        fd = open_record(path, os.O_RDWR | os.O_CREAT)
        with os.fdopen(fd, "r+b") as stream:
            overlapped = Overlapped()
            _check(_lock(msvcrt.get_osfhandle(stream.fileno()), 3, 0, 1, 0, C.byref(overlapped)))
            try:
                yield
            finally:
                # Closing the fd also releases the lock if this call fails.
                _unlock(msvcrt.get_osfhandle(stream.fileno()), 0, 1, 0, C.byref(overlapped))


def identity(path: Path) -> tuple[int, int]:
    with directory_guard(path.parent), _handle(path) as handle:
        info = _info(handle)
        if info.attributes & _REPARSE:
            raise OSError("拒绝重解析点清理目标")
        return _identity(info)


def remove_tree(path: Path, expected: tuple[int, int]):
    """Delete opened objects, keeping ancestors pinned and never traversing links."""
    def remove(entry: Path, expected_id=None):
        with _handle(entry, _ATTR_READ | _DELETE) as handle:
            info = _info(handle)
            if expected_id is not None:
                _ordinary(info, directory=True)
                if _identity(info) != expected_id:
                    raise OSError("清理目标已被替换，停止删除")
            if info.attributes & _DIRECTORY and not info.attributes & _REPARSE:
                with os.scandir(entry) as children:
                    names = [child.name for child in children]
                for name in names:
                    remove(entry / name)
            disposition = Disposition(1)
            _check(_set_info(handle, 4, C.byref(disposition), C.sizeof(disposition)))

    with directory_guard(path.parent):
        remove(_absolute(path), expected)


@contextmanager
def _process(pid: int):
    handle = _open_process(0x1000 | 0x100000, False, pid)  # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        yield handle
    finally:
        _close(handle)


def process_identity(pid: int) -> str | None:
    if not 0 < pid <= 0xFFFFFFFF:
        return None
    try:
        with _process(pid) as handle:
            if _wait(handle, 0) != 258:  # Only a live process can own a new run.
                return None
            created, exited, kernel, user = (W.FILETIME() for _ in range(4))
            _check(_process_times(handle, *(C.byref(t) for t in (created, exited, kernel, user))))
            return f"win32:{(created.dwHighDateTime << 32) | created.dwLowDateTime}"
    except OSError:
        return None


def process_exited(pid: int) -> bool | None:
    if not 0 < pid <= 0xFFFFFFFF:
        return None
    try:
        with _process(pid) as handle:
            return True if _wait(handle, 0) == 0 else None
    except OSError as error:
        # ERROR_INVALID_PARAMETER means the valid, nonzero PID no longer exists.
        # Access denied and other errors provide no evidence that it has exited.
        return True if error.winerror == 87 else None
