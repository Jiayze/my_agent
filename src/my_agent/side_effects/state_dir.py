"""V0.4 应用状态目录：仅当前用户可访问的隔离存储

快照、intent/审批状态等应用自身状态放在独立状态目录（默认
``%LOCALAPPDATA%\\my_agent\\state``），创建时设置仅当前用户 SID 的
Windows DACL，派生文件继承同一 ACL。该目录不注册为任何 Agent 工具的
根目录，Agent 无法通过写入工具覆盖快照或伪造状态。

目录创建或 DACL 设置失败一律 fail closed：写入工具不得装配。
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_SDDL_REVISION_1 = 1
_SE_FILE_OBJECT = 0
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000


class StateDirectoryError(RuntimeError):
    """状态目录无法创建或无法设置独占 DACL"""


def default_state_root() -> Path:
    """返回默认状态目录：%LOCALAPPDATA%\\my_agent\\state"""
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "my_agent" / "state"
    return Path.home() / ".local" / "state" / "my_agent"


def _current_user_sid_string() -> str | None:
    """通过进程令牌查询当前用户 SID 的字符串形式"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes.wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # GetCurrentProcess 返回伪句柄；必须显式声明 restype，否则被截断为 int
        kernel32.GetCurrentProcess.restype = ctypes.wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.wintypes.HANDLE),
        ]
        advapi32.GetTokenInformation.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]

        token_handle = ctypes.wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(),
            _TOKEN_QUERY,
            ctypes.byref(token_handle),
        ):
            return None
        try:
            return_length = ctypes.wintypes.DWORD()
            advapi32.GetTokenInformation(
                token_handle,
                _TOKEN_USER,
                None,
                0,
                ctypes.byref(return_length),
            )
            buffer = ctypes.create_string_buffer(return_length.value)
            if not advapi32.GetTokenInformation(
                token_handle,
                _TOKEN_USER,
                buffer,
                return_length.value,
                ctypes.byref(return_length),
            ):
                return None
            # TOKEN_USER 的第一个成员是 SID_AND_ATTRIBUTES.Sid 指针：
            # 从缓冲区起始处读取该指针值，而不是把缓冲区地址当作 SID
            sid_pointer = ctypes.c_void_p.from_buffer(buffer)
            sid_string = ctypes.c_void_p()
            if not advapi32.ConvertSidToStringSidW(
                sid_pointer,
                ctypes.byref(sid_string),
            ):
                return None
            try:
                return ctypes.c_wchar_p(sid_string.value).value
            finally:
                kernel32.LocalFree(sid_string)
        finally:
            kernel32.CloseHandle(token_handle)
    except Exception:
        logger.debug("could not query current user SID", exc_info=True)
        return None


def _apply_user_only_dacl(path: Path, sid: str) -> None:
    """把目录 DACL 设置为仅当前用户完全控制，并禁用继承"""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.wintypes.BOOL),
    ]
    sddl = f"D:P(A;OICI;FA;;;{sid})"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        ctypes.c_wchar_p(sddl),
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise StateDirectoryError("could not build the user-only security descriptor")
    try:
        # ConvertString 返回 self-relative 格式的 SD，其 Dacl 字段是偏移量；
        # 用 GetSecurityDescriptorDacl 取出解析后的绝对 ACL 指针
        present = ctypes.wintypes.BOOL()
        defaulted = ctypes.wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise StateDirectoryError("could not read the DACL from the descriptor")
        if not present.value or not dacl.value:
            raise StateDirectoryError("security descriptor has no DACL")
        # SDDL 中的 "P:" 已把 SE_DACL_PROTECTED 写入描述符 Control 位，
        # 因此 SetFileSecurityW 应用 DACL 的同时也会禁用继承。
        # （SetNamedSecurityInfoW 在部分受限环境中返回 error 87。）
        advapi32.SetFileSecurityW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        if not advapi32.SetFileSecurityW(
            str(path),
            _DACL_SECURITY_INFORMATION,
            descriptor,
        ):
            raise StateDirectoryError(
                f"could not apply the user-only DACL (error {ctypes.get_last_error()})"
            )
    finally:
        kernel32.LocalFree(descriptor)


def ensure_state_directory(path: Path) -> Path:
    """创建状态目录并施加仅当前用户的访问控制；失败时抛出异常"""
    resolved = path.expanduser()
    try:
        resolved.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise StateDirectoryError(
            "could not create the application state directory"
        ) from error
    if sys.platform == "win32":
        sid = _current_user_sid_string()
        if sid is None:
            raise StateDirectoryError("could not determine the current user SID")
        try:
            _apply_user_only_dacl(resolved, sid)
        except OSError as error:
            raise StateDirectoryError("could not apply the user-only DACL") from error
    else:
        try:
            os.chmod(resolved, 0o700)
        except OSError as error:
            raise StateDirectoryError(
                "could not restrict the state directory permissions"
            ) from error
    logger.info("application state directory ready: %s", resolved)
    return resolved
