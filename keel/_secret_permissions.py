"""Private age key permissions, using POSIX modes or a protected Windows DACL."""

import os
from pathlib import Path


def protect(path: Path, *, directory: bool = False) -> None:
    if os.name == "nt":
        flags = "OICI" if directory else ""
        _set_acl(path, f"D:P(A;{flags};FA;;;{_user_sid()})(A;{flags};FA;;;SY)", directory=directory)
    else:
        path.chmod(0o700 if directory else 0o600)


def private_key(path: Path) -> bool:
    try:
        if os.name != "nt":
            return path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o777 == 0o600
        return _private_key(path)
    except OSError:
        return False


if os.name == "nt":
    import ctypes as C
    from ctypes import wintypes as W
    from contextlib import contextmanager

    class FileInfo(C.Structure):
        _fields_ = [("attributes", W.DWORD), ("created", W.FILETIME),
                    ("accessed", W.FILETIME), ("written", W.FILETIME),
                    ("volume", W.DWORD), ("size_high", W.DWORD), ("size_low", W.DWORD),
                    ("links", W.DWORD), ("index_high", W.DWORD), ("index_low", W.DWORD)]

    class Acl(C.Structure):
        _fields_ = [("revision", W.BYTE), ("reserved", W.BYTE), ("size", W.WORD),
                    ("count", W.WORD), ("reserved2", W.WORD)]

    class Ace(C.Structure):
        _fields_ = [("type", W.BYTE), ("flags", W.BYTE), ("size", W.WORD), ("mask", W.DWORD)]

    _kernel = C.WinDLL("kernel32", use_last_error=True)
    _advapi = C.WinDLL("advapi32", use_last_error=True)

    def _api(library, name, args, result=W.BOOL):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result
        return function

    P = C.c_void_p
    _create = _api(_kernel, "CreateFileW", [W.LPCWSTR, W.DWORD, W.DWORD, P, W.DWORD, W.DWORD, W.HANDLE], W.HANDLE)
    _close = _api(_kernel, "CloseHandle", [W.HANDLE])
    _free = _api(_kernel, "LocalFree", [P], P)
    _current = _api(_kernel, "GetCurrentProcess", [], W.HANDLE)
    _info = _api(_kernel, "GetFileInformationByHandle", [W.HANDLE, C.POINTER(FileInfo)])
    _token = _api(_advapi, "OpenProcessToken", [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)])
    _token_info = _api(_advapi, "GetTokenInformation", [W.HANDLE, C.c_int, P, W.DWORD, C.POINTER(W.DWORD)])
    _sid_text = _api(_advapi, "ConvertSidToStringSidW", [P, C.POINTER(P)])
    _from_sddl = _api(_advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [W.LPCWSTR, W.DWORD, C.POINTER(P), P])
    _dacl = _api(_advapi, "GetSecurityDescriptorDacl", [P, C.POINTER(W.BOOL), C.POINTER(P), C.POINTER(W.BOOL)])
    _set_security = _api(_advapi, "SetSecurityInfo", [W.HANDLE, C.c_int, W.DWORD, P, P, P, P], W.DWORD)
    _get_security = _api(_advapi, "GetSecurityInfo", [W.HANDLE, C.c_int, W.DWORD, P, P, C.POINTER(P), P, C.POINTER(P)], W.DWORD)
    _control = _api(_advapi, "GetSecurityDescriptorControl", [P, C.POINTER(W.WORD), C.POINTER(W.DWORD)])
    _ace = _api(_advapi, "GetAce", [P, W.DWORD, C.POINTER(P)])

    def _check(result):
        if not result:
            raise C.WinError(C.get_last_error())

    def _sid_string(sid) -> str:
        text = P()
        _check(_sid_text(sid, C.byref(text)))
        try:
            return C.wstring_at(text)
        finally:
            _free(text)

    def _user_sid() -> str:
        token = W.HANDLE()
        _check(_token(_current(), 8, C.byref(token)))  # TOKEN_QUERY
        try:
            needed = W.DWORD()
            _token_info(token, 1, None, 0, C.byref(needed))  # TokenUser
            if not needed.value:
                raise C.WinError(C.get_last_error())
            buffer = C.create_string_buffer(needed.value)
            _check(_token_info(token, 1, buffer, len(buffer), C.byref(needed)))
            return _sid_string(C.cast(buffer, C.POINTER(P))[0])
        finally:
            _close(token)

    @contextmanager
    def _handle(path: Path, *, directory: bool = False, write: bool = False):
        # Open the object itself, rejecting links before changing its permissions.
        access = 0x20000 | 0x80 | (0x40000 if write else 0)  # READ_CONTROL | READ_ATTRIBUTES | WRITE_DAC
        handle = _create(str(path.absolute()), access, 3, None, 3, 0x02000000 | 0x00200000, None)
        if handle == P(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            info = FileInfo()
            _check(_info(handle, C.byref(info)))
            if (info.attributes & 0x400 or bool(info.attributes & 0x10) != directory
                    or (not directory and info.links != 1)):
                raise OSError("age 私钥或配置目录必须是普通文件/目录，不能使用重解析点或硬链接私钥")
            yield handle
        finally:
            _close(handle)

    def _set_acl(path: Path, sddl: str, *, directory: bool = False):
        descriptor = P()
        _check(_from_sddl(sddl, 1, C.byref(descriptor), None))
        try:
            present, defaulted, dacl = W.BOOL(), W.BOOL(), P()
            _check(_dacl(descriptor, C.byref(present), C.byref(dacl), C.byref(defaulted)))
            if not present.value or not dacl:
                raise OSError("拒绝设置空访问控制列表")
            with _handle(path, directory=directory, write=True) as handle:
                error = _set_security(handle, 1, 4 | 0x80000000, None, None, dacl, None)
                if error:
                    raise C.WinError(error)
        finally:
            _free(descriptor)

    def _private_key(path: Path) -> bool:
        with _handle(path) as handle:
            owner, dacl, descriptor = P(), P(), P()
            error = _get_security(handle, 1, 5, C.byref(owner), None, C.byref(dacl), None, C.byref(descriptor))
            if error:
                raise C.WinError(error)
            try:
                control, revision = W.WORD(), W.DWORD()
                _check(_control(descriptor, C.byref(control), C.byref(revision)))
                if not dacl or not control.value & 0x1000:  # SE_DACL_PROTECTED
                    return False
                user, has_user = _user_sid(), False
                if not owner or _sid_string(owner) not in (user, "S-1-5-18", "S-1-5-32-544"):
                    return False
                for index in range(C.cast(dacl, C.POINTER(Acl)).contents.count):
                    pointer = P()
                    _check(_ace(dacl, index, C.byref(pointer)))
                    ace = C.cast(pointer, C.POINTER(Ace)).contents
                    # Only explicit, unconditional allow entries for this user
                    # and SYSTEM are accepted. Unknown policies fail closed.
                    if ace.type != 0 or ace.flags:
                        return False
                    sid = _sid_string(pointer.value + C.sizeof(Ace))
                    if sid not in (user, "S-1-5-18"):
                        return False
                    if sid == user and ace.mask & 0x1F01FF == 0x1F01FF:
                        has_user = True
                return has_user
            finally:
                _free(descriptor)
