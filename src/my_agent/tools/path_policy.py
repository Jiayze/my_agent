"""只读工具的共享路径策略：工作区边界、敏感路径拒绝与输出脱敏

所有只读工具（list/read/search/git）必须经过本模块的同一套校验，
不在各工具内重复实现路径判断。

安全判定基于**规范路径**：先用 ``os.path.realpath`` 解析真实的 Windows
长文件名（消除 8.3 短别名、symlink、junction 等），确认仍在工作区根内，
再对规范相对路径执行敏感名策略。``ResolvedPath.relative`` 始终是规范
显示/安全路径。
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# Windows 文件属性：重解析点（symlink / junction / mount point）
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

_MAX_INPUT_PATH_LENGTH = 1024

# 永远不对外暴露的目录名（大小写不敏感）
SENSITIVE_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "_ext",
        "local_artifacts",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        ".nox",
        ".idea",
        ".vscode",
        "node_modules",
        "logs",
        "build",
        "dist",
        ".eggs",
    }
)

# 永远不对外暴露的扩展名
SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".p8",
    ".jks",
    ".keystore",
    ".kdbx",
    ".ppk",
    ".asc",
)

# 名称中包含这些片段即视为敏感（大小写不敏感）
SENSITIVE_NAME_FRAGMENTS = (
    "secret",
    "credential",
    "passwd",
    "password",
    "apikey",
    "api_key",
    "access_key",
    "private_key",
    "private-key",
    "id_rsa",
    "id_ed25519",
    "token",
)

# Windows 保留设备名（含带扩展名的形式）
_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")

# 为便于测试替换，保留模块级别名
_lstat = os.lstat
_open = os.open
_fstat = os.fstat


class PathPolicyError(ValueError):
    """路径被策略拒绝或目标不可用

    ``code`` 是稳定的机器可读错误码，``message`` 不含绝对路径。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class ResolvedPath:
    """通过策略校验后的工作区路径

    ``relative`` 是规范长文件名对应的 POSIX 相对路径，既是展示路径也是
    安全判定路径；``absolute`` 是规范绝对路径。
    """

    relative: str
    absolute: Path


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    """判断 stat 结果是否代表 symlink / junction / 其他 reparse point"""
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _is_reserved_device(segment: str) -> bool:
    """判断路径片段是否为 Windows 保留设备名"""
    stem = segment.casefold().split(".", 1)[0]
    return stem in _RESERVED_DEVICE_NAMES


def is_sensitive_name(name: str) -> bool:
    """判断单个文件/目录名是否属于默认拒绝的敏感名称"""
    lowered = name.casefold()
    if (
        lowered == ".env"
        or lowered == ".envrc"
        or lowered.startswith(".env.")
        or lowered.endswith(".env")
    ):
        return True
    if lowered in SENSITIVE_DIRECTORY_NAMES:
        return True
    if lowered.endswith(SENSITIVE_SUFFIXES):
        return True
    return any(fragment in lowered for fragment in SENSITIVE_NAME_FRAGMENTS)


def is_sensitive_relative(relative: str) -> bool:
    """判断工作区相对 POSIX 路径是否命中敏感目录或敏感文件名"""
    if relative in ("", "."):
        return False
    return any(is_sensitive_name(part) for part in relative.split("/") if part)


def _split_user_path(raw: str) -> list[str]:
    """把用户输入拆成规范化片段，拒绝所有可疑形态"""
    if not isinstance(raw, str):
        raise PathPolicyError("PATH_INVALID", "path must be a string")
    if not raw:
        raise PathPolicyError("PATH_INVALID", "path must not be empty")
    if len(raw) > _MAX_INPUT_PATH_LENGTH:
        raise PathPolicyError("PATH_INVALID", "path is too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise PathPolicyError("PATH_INVALID", "path contains control characters")
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise PathPolicyError(
            "PATH_ABSOLUTE_DENIED",
            "UNC and device paths are not allowed",
        )
    if _DRIVE_PREFIX.match(raw):
        raise PathPolicyError(
            "PATH_ABSOLUTE_DENIED",
            "drive-qualified and drive-relative paths are not allowed",
        )
    if ":" in raw:
        # NTFS Alternate Data Streams 也包含冒号
        raise PathPolicyError("PATH_INVALID", "path must not contain ':'")

    normalized = raw.replace("\\", "/")
    if normalized.startswith("/"):
        raise PathPolicyError("PATH_ABSOLUTE_DENIED", "absolute paths are not allowed")

    segments: list[str] = []
    for segment in normalized.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise PathPolicyError("PATH_TRAVERSAL_DENIED", "'..' is not allowed")
        if segment != segment.rstrip(" ."):
            raise PathPolicyError(
                "PATH_INVALID",
                "path segments must not end with a dot or space",
            )
        if _is_reserved_device(segment):
            raise PathPolicyError(
                "PATH_INVALID", "reserved device names are not allowed"
            )
        segments.append(segment)
    return segments


def _relative_posix(parts: list[str]) -> str:
    """把片段序列转换为工作区相对 POSIX 路径"""
    return "/".join(parts) if parts else "."


def _ensure_within_root(root: Path, target: Path) -> None:
    """用 commonpath 做真实边界判定，绝不使用字符串前缀比较"""
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(root)), os.path.normcase(str(target))]
        )
    except ValueError as error:  # 不同盘符
        raise PathPolicyError(
            "PATH_ESCAPE_DENIED", "path escapes the workspace"
        ) from error
    if common != os.path.normcase(str(root)):
        raise PathPolicyError("PATH_ESCAPE_DENIED", "path escapes the workspace")


def _relative_to_root(root: Path, canonical: Path) -> str:
    """把规范绝对路径转换为工作区相对 POSIX 路径并复核边界"""
    _ensure_within_root(root, canonical)
    relative = os.path.relpath(str(canonical), str(root))
    if relative == ".":
        return "."
    return relative.replace(os.sep, "/")


def canonical_relative_if_within(root: Path, target: Path) -> str | None:
    """解析目标真实路径；越界或不可解析时返回 ``None``

    用于遍历过程中逐项规范化：先得到真实长文件名，再计算相对路径，
    供调用方执行敏感名策略和输出。
    """
    try:
        canonical = Path(os.path.realpath(target))
        return _relative_to_root(root, canonical)
    except (OSError, PathPolicyError):
        return None


def _canonical_root(root: Path) -> Path:
    """校验并返回工作区根的真实规范路径"""
    try:
        root_stat = _lstat(root)
    except OSError as error:
        raise PathPolicyError(
            "WORKSPACE_UNAVAILABLE", "workspace is not accessible"
        ) from error
    if _is_reparse_point(root_stat):
        raise PathPolicyError(
            "PATH_SYMLINK_DENIED", "workspace root is a reparse point"
        )
    if not stat.S_ISDIR(root_stat.st_mode):
        raise PathPolicyError("WORKSPACE_UNAVAILABLE", "workspace is not a directory")
    return Path(os.path.realpath(root))


def resolve_workspace_path(
    root: Path,
    raw: str,
    *,
    expect: str = "any",
    must_exist: bool = True,
) -> ResolvedPath:
    """把用户路径解析为工作区内真实规范路径并执行完整策略

    ``expect`` 取 ``"any"`` / ``"file"`` / ``"dir"``；``must_exist=False``
    允许最终组件不存在（例如 git diff 的已删除文件），但仍逐级校验父目录。

    解析顺序：词法校验 → 逐级 lstat 拒绝 reparse point → realpath 得到
    真实长文件名 → 工作区根包含性复核 → 对**规范相对路径**执行敏感名策略。
    """
    if expect not in ("any", "file", "dir"):
        raise ValueError("expect must be 'any', 'file' or 'dir'")

    root_real = _canonical_root(Path(root))
    parts = _split_user_path(raw)
    lexical_relative = _relative_posix(parts)
    if is_sensitive_relative(lexical_relative):
        raise PathPolicyError("PATH_SENSITIVE_DENIED", "path is excluded by policy")

    current = root_real
    final_exists = True
    for index, part in enumerate(parts):
        current = current / part
        is_last = index == len(parts) - 1
        try:
            file_stat = _lstat(current)
        except FileNotFoundError as error:
            if is_last and not must_exist:
                final_exists = False
                break
            raise PathPolicyError("FS_NOT_FOUND", "path does not exist") from error
        except PermissionError as error:
            raise PathPolicyError(
                "FS_PERMISSION_DENIED", "path is not accessible"
            ) from error
        except OSError as error:
            raise PathPolicyError(
                "FS_UNAVAILABLE", "path could not be accessed"
            ) from error

        if _is_reparse_point(file_stat):
            raise PathPolicyError(
                "PATH_SYMLINK_DENIED",
                "symbolic links and reparse points are not allowed",
            )
        if not is_last and not stat.S_ISDIR(file_stat.st_mode):
            raise PathPolicyError(
                "FS_NOT_DIRECTORY", "path component is not a directory"
            )
        if is_last:
            if expect == "dir" and not stat.S_ISDIR(file_stat.st_mode):
                raise PathPolicyError("FS_NOT_DIRECTORY", "path is not a directory")
            if expect == "file" and not stat.S_ISREG(file_stat.st_mode):
                raise PathPolicyError("FS_NOT_FILE", "path is not a regular file")

    if not parts and expect == "file":
        raise PathPolicyError("FS_NOT_FILE", "path is not a regular file")

    if final_exists:
        canonical = Path(os.path.realpath(current))
    else:
        # 最终组件不存在：规范化父目录后拼回原始名称
        canonical = Path(os.path.realpath(current.parent)) / current.name

    relative = _relative_to_root(root_real, canonical)
    if is_sensitive_relative(relative):
        raise PathPolicyError("PATH_SENSITIVE_DENIED", "path is excluded by policy")
    return ResolvedPath(relative=relative, absolute=canonical)


def _same_file_identity(before: os.stat_result, after: os.stat_result) -> bool:
    """比较打开前后的文件身份（类型、设备、inode、reparse 属性）"""
    if stat.S_IFMT(before.st_mode) != stat.S_IFMT(after.st_mode):
        return False
    for attribute in ("st_dev", "st_ino"):
        before_value = getattr(before, attribute, None)
        after_value = getattr(after, attribute, None)
        if before_value is None or after_value is None:
            continue
        if before_value != after_value:
            return False
    before_attributes = getattr(before, "st_file_attributes", 0)
    after_attributes = getattr(after, "st_file_attributes", 0)
    if after_attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    if (before_attributes ^ after_attributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return True


def lexical_relative_path(root: Path, raw: str) -> str:
    """只做词法校验并返回工作区相对 POSIX 路径，不触碰文件系统

    校验内容与 ``resolve_workspace_path`` 的词法阶段一致：分段规则、
    ``..``/绝对路径/盘符拒绝与敏感名策略。调用方用于存在性检查之前的
    策略判定（例如受保护名称），随后仍须执行完整的真实路径解析。
    """
    _canonical_root(Path(root))
    parts = _split_user_path(raw)
    relative = _relative_posix(parts)
    if is_sensitive_relative(relative):
        raise PathPolicyError("PATH_SENSITIVE_DENIED", "path is excluded by policy")
    return relative


def create_missing_parents(root: Path, raw: str) -> list[str]:
    """为工作区内的新文件创建缺失的父目录，返回新创建的相对目录列表

    只能用于已经通过 ``resolve_workspace_path`` 词法校验的路径（无 ``..``、
    绝对路径、盘符、敏感名等）：词法校验先于本函数执行，因此逐级 mkdir
    的每个片段都已确认为干净名称。已存在的组件必须是目录。
    """
    root_real = _canonical_root(Path(root))
    parts = _split_user_path(raw)
    if is_sensitive_relative(_relative_posix(parts)):
        raise PathPolicyError("PATH_SENSITIVE_DENIED", "path is excluded by policy")

    created: list[str] = []
    current = root_real
    for index, part in enumerate(parts[:-1]):
        current = current / part
        try:
            component_stat = _lstat(current)
        except FileNotFoundError:
            current.mkdir(parents=False, exist_ok=True)
            created.append(_relative_posix(parts[: index + 1]))
            continue
        except OSError as error:
            raise PathPolicyError(
                "FS_UNAVAILABLE", "path could not be accessed"
            ) from error
        if _is_reparse_point(component_stat) or not stat.S_ISDIR(
            component_stat.st_mode
        ):
            raise PathPolicyError(
                "FS_NOT_DIRECTORY", "path component is not a directory"
            )
    return created


def open_verified_binary(root: Path, resolved: ResolvedPath) -> BinaryIO:
    """打开已解析文件，并尽量缩小 lstat/open 的 TOCTOU 窗口

    步骤：打开前 lstat 记录身份 → 以 no-follow（平台支持时）打开 →
    fstat 比较 dev/inode/文件属性 → 重新解析规范路径与祖先链。

    仍存在极小的残余竞态（open 与 fstat 之间），本实现不声称提供
    操作系统级 sandbox。
    """
    try:
        before = _lstat(resolved.absolute)
    except PermissionError as error:
        raise PathPolicyError(
            "FS_PERMISSION_DENIED", "file is not accessible"
        ) from error
    except OSError as error:
        raise PathPolicyError("FS_UNAVAILABLE", "file could not be accessed") from error

    if _is_reparse_point(before):
        raise PathPolicyError(
            "PATH_SYMLINK_DENIED", "symbolic links and reparse points are not allowed"
        )
    if not stat.S_ISREG(before.st_mode):
        raise PathPolicyError("FS_NOT_FILE", "path is not a regular file")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = _open(resolved.absolute, flags)
    except OSError as error:
        if error.errno in (errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)):
            raise PathPolicyError(
                "PATH_SYMLINK_DENIED",
                "symbolic links and reparse points are not allowed",
            ) from error
        if isinstance(error, PermissionError):
            raise PathPolicyError(
                "FS_PERMISSION_DENIED", "file is not accessible"
            ) from error
        raise PathPolicyError("FS_UNAVAILABLE", "file could not be opened") from error

    descriptor_open = True
    try:
        after = _fstat(descriptor)
        if not _same_file_identity(before, after):
            raise PathPolicyError(
                "FS_CHANGED", "file changed while it was being opened"
            )
        revalidated = resolve_workspace_path(root, resolved.relative, expect="file")
        if revalidated.relative != resolved.relative or os.path.normcase(
            str(Path(os.path.realpath(revalidated.absolute)))
        ) != os.path.normcase(str(Path(os.path.realpath(resolved.absolute)))):
            raise PathPolicyError(
                "FS_CHANGED", "file changed while it was being opened"
            )
        handle = os.fdopen(descriptor, "rb")
        descriptor_open = False
        return handle
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass


_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # BEGIN 之后若缺少 END，一直脱敏到文本末尾（fail closed）
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
            r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"), "Bearer [REDACTED]"),
    (
        # 覆盖 SECRET_KEY / AWS_SECRET_ACCESS_KEY / API_KEY / ACCESS_KEY
        # 以及 JSON 风格 "api_key": "value" 的赋值形态
        re.compile(
            r"(?i)([\"']?[A-Za-z0-9_.-]*"
            r"(?:secret|token|password|passwd|api[_-]?key|access[_-]?key"
            r"|private[_-]?key|credential|authorization)"
            r"[A-Za-z0-9_.-]*[\"']?)"
            r"(\s*[:=]\s*)([\"']?)([^\s\"',;]{4,})"
        ),
        r"\1\2\3[REDACTED]",
    ),
)


def redact_text(text: str) -> str:
    """对工具输出中的常见密钥形态做确定性脱敏"""
    redacted = text
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节上限截断且不产生半个字符"""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def collect_regular_files(
    root: Path,
    start: Path,
    *,
    max_files: int = 2000,
    max_depth: int = 20,
) -> tuple[list[str], bool]:
    """共享的有界文件遍历：跳过敏感路径、链接与过深目录

    返回 ``(files, scan_truncated)``；``scan_truncated`` 表示扫描未完成
    （达到文件数上限、深度上限或目录不可读），此时结果不完整。
    所有工作区扫描类工具共用本实现，路径边界与敏感策略由本模块统一执行。
    """
    root_real = Path(os.path.realpath(root))
    if start.is_file():
        relative = canonical_relative_if_within(root_real, start)
        if relative is None or is_sensitive_relative(relative):
            return [], False
        return [relative], False

    collected: list[str] = []
    truncated = False
    stack: list[tuple[Path, int]] = [(start, 0)]
    while stack and len(collected) < max_files:
        directory, depth = stack.pop()
        if depth > max_depth:
            truncated = True
            continue
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError:
            truncated = True
            continue
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                truncated = True
                continue
            if (
                stat.S_ISLNK(entry_stat.st_mode)
                or getattr(entry_stat, "st_file_attributes", 0)
                & _FILE_ATTRIBUTE_REPARSE_POINT
            ):
                continue
            relative = canonical_relative_if_within(root_real, Path(entry.path))
            if relative is None or is_sensitive_relative(relative):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                stack.append((Path(entry.path), depth + 1))
            elif stat.S_ISREG(entry_stat.st_mode):
                collected.append(relative)
                if len(collected) >= max_files:
                    truncated = True
                    break
    return sorted(collected), truncated
