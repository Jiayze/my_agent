"""V0.3 只读仓库工具：list/read/search/git_status/git_diff

所有工具显式声明 ``effect=read_only``，只接受工作区相对路径；
工作区根由 ``AgentConfig.workspace_root`` 在装配时注入，模型无法修改。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.tools import BaseTool, tool

from my_agent.tools.path_policy import (
    PathPolicyError,
    canonical_relative_if_within,
    collect_regular_files,
    is_sensitive_relative,
    open_verified_binary,
    redact_text,
    resolve_workspace_path,
    truncate_utf8,
)

MAX_LIST_ENTRIES = 100
MAX_READ_FILE_BYTES = 256 * 1024
MAX_READ_LINES = 200
MAX_LINE_CHARS = 2000
MAX_RESPONSE_BYTES = 64 * 1024
MAX_SEARCH_RESULTS = 50
MAX_SEARCH_FILES = 2000
MAX_SEARCH_DEPTH = 20
MAX_SEARCH_MATCHES_PER_FILE = 5
SEARCH_PATTERN_MAX_CHARS = 256
SEARCH_TIMEOUT_SECONDS = 10
GIT_TIMEOUT_SECONDS = 10
GIT_MAX_BYTES = 64 * 1024
MAX_GIT_ENTRIES = 200

# 单次子进程 stdout/stderr 各自的硬上限：读取线程边读边丢弃，
# 达到上限立即终止进程树，绝不把无限输出留在内存或管道里。
MAX_SUBPROCESS_OUTPUT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_KILL_TIMEOUT_SECONDS = 5
_DRAIN_JOIN_SECONDS = 2

_FILTER_KEY_PATTERN = re.compile(r"^filter\.[A-Za-z0-9._-]+\.(clean|process|smudge)$")

# 部署已知的 CodeBuddy vendor ripgrep 位置（相对 %APPDATA%），仅作候选之一
_VENDOR_RG_RELATIVE = Path(
    "npm/node_modules/@tencent-ai/codebuddy-code/vendor/ripgrep/x64-win32/rg.exe"
)
_KNOWN_GIT_PATH = Path(r"C:\Program Files\Git\cmd\git.exe")


def _json(payload: dict[str, Any]) -> str:
    """以确定性格式序列化工具结果"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(code: str, message: str) -> str:
    """构造不含绝对路径和密钥的结构化错误结果"""
    return _json({"ok": False, "error": code, "message": message})


def _policy_error(error: PathPolicyError) -> str:
    """把路径策略异常转换为模型可读的结构化错误"""
    return _error(error.code, error.message)


def _child_environment() -> dict[str, str]:
    """构造子进程最小环境，避免继承调用方任意变量"""
    allowed = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "COMSPEC",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
    )
    return {name: os.environ[name] for name in allowed if name in os.environ}


def _taskkill_executable() -> Path | None:
    """返回固定的 System32\\taskkill.exe，避免依赖 PATH 解析"""
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        return None
    candidate = Path(system_root) / "System32" / "taskkill.exe"
    return candidate if candidate.is_file() else None


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """终止子进程及其派生的子进程；taskkill 不可用时回退到 kill"""
    if os.name == "nt":
        taskkill = _taskkill_executable()
        if taskkill is not None:
            try:
                subprocess.run(
                    [str(taskkill), "/F", "/T", "/PID", str(process.pid)],
                    shell=False,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_KILL_TIMEOUT_SECONDS,
                    env=_child_environment(),
                )
            except (OSError, subprocess.SubprocessError):
                pass
    try:
        process.kill()
    except OSError:
        pass


class _StreamCollector:
    """在读取线程中收集有上限的字节流，超限时设置 overflow 事件"""

    def __init__(self, stream: Any, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.buffer = bytearray()
        self.truncated = False

    @property
    def data(self) -> bytes:
        return bytes(self.buffer)

    def run(self, overflow: threading.Event) -> None:
        try:
            while True:
                chunk = self.stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                room = self.limit - len(self.buffer)
                if room > 0:
                    self.buffer.extend(chunk[:room])
                if len(chunk) > room:
                    self.truncated = True
                    overflow.set()
                # 超限后继续读并丢弃，避免子进程因管道写满而死锁；
                # 主线程会尽快终止进程树。
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.stream.close()
            except (OSError, ValueError):
                pass


@dataclass(frozen=True)
class _ProcessResult:
    """有界子进程结果"""

    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    truncated: bool


def _drain_readers(
    process: subprocess.Popen[bytes], threads: list[threading.Thread]
) -> None:
    """有界等待读取线程；孙进程仍持有管道句柄时强制关闭流"""
    for thread in threads:
        thread.join(timeout=_DRAIN_JOIN_SECONDS)
    if any(thread.is_alive() for thread in threads):
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        for thread in threads:
            thread.join(timeout=1)


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> _ProcessResult:
    """以 shell=False 执行固定命令，运行中就有界捕获 stdout/stderr

    超时或任一输出流达到上限时立即终止进程树；终止后只做有界 join/close，
    不再调用无超时的 ``communicate``。
    """
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env if env is not None else _child_environment(),
        bufsize=0,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_collector = _StreamCollector(process.stdout, max_output_bytes)
    stderr_collector = _StreamCollector(process.stderr, max_output_bytes)
    overflow = threading.Event()
    threads = [
        threading.Thread(target=stdout_collector.run, args=(overflow,), daemon=True),
        threading.Thread(target=stderr_collector.run, args=(overflow,), daemon=True),
    ]
    for thread in threads:
        thread.start()

    timed_out = False
    deadline = time.monotonic() + timeout_seconds
    while True:
        process_done = process.poll() is not None
        readers_done = all(not thread.is_alive() for thread in threads)
        if process_done and readers_done:
            break
        if overflow.is_set():
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.01)

    if timed_out or overflow.is_set():
        _terminate_process_tree(process)

    _drain_readers(process, threads)
    try:
        process.wait(timeout=_KILL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass

    return _ProcessResult(
        stdout=stdout_collector.data,
        stderr=stderr_collector.data,
        returncode=process.returncode,
        timed_out=timed_out,
        truncated=stdout_collector.truncated
        or stderr_collector.truncated
        or overflow.is_set(),
    )


def _probe_version(executable: Path, pattern: str, minimum_major: int) -> bool:
    """执行 ``--version`` 并校验主版本号"""
    if not executable.is_absolute() or not executable.is_file():
        return False
    try:
        result = _run_bounded(
            [str(executable), "--version"],
            cwd=executable.parent,
            timeout_seconds=5,
            max_output_bytes=8192,
        )
    except OSError:
        return False
    if result.timed_out:
        return False
    match = re.search(pattern, result.stdout.decode("utf-8", errors="replace"))
    return bool(match) and int(match.group(1)) >= minimum_major


def resolve_ripgrep(explicit: Path | str | None = None) -> Path | None:
    """启动时一次解析 ripgrep 绝对路径；模型无法参与或更改

    候选顺序为显式注入 → CodeBuddy vendor → ``PATH``。无论来源如何，
    最终都固化为绝对路径并在进程生命周期内复用；模型不能传入路径或选项。
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / _VENDOR_RG_RELATIVE)
    which = shutil.which("rg")
    if which:
        candidates.append(Path(which))

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if _probe_version(resolved, r"ripgrep\s+(\d+)\.", 14):
            return resolved
    return None


def resolve_git(explicit: Path | str | None = None) -> Path | None:
    """启动时一次解析 git 绝对路径；模型无法参与或更改"""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    which = shutil.which("git")
    if which:
        candidates.append(Path(which))
    candidates.append(_KNOWN_GIT_PATH)

    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if _probe_version(resolved, r"git version\s+(\d+)\.", 2):
            return resolved
    return None


def _git_environment() -> dict[str, str]:
    """构造 git 子进程环境，屏蔽系统/全局配置与交互提示"""
    environment = _child_environment()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "NUL",
            "GIT_CONFIG_SYSTEM": "NUL",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
            "GIT_ASKPASS": "",
        }
    )
    return environment


def _filter_overrides(git: Path, repo: Path) -> list[str]:
    """枚举仓库本地 filter 驱动并生成空值覆盖参数"""
    try:
        result = _run_bounded(
            [
                str(git),
                "config",
                "--local",
                "--name-only",
                "--get-regexp",
                r"^filter\..*\.(clean|process|smudge)$",
            ],
            cwd=repo,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
        )
    except OSError:
        return []
    if result.timed_out:
        return []

    overrides: list[str] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        key = line.strip()
        if not _FILTER_KEY_PATTERN.match(key):
            continue
        overrides.extend(["-c", f"{key}="])
    return overrides


def _git_base_args(git: Path, repo: Path) -> list[str]:
    """构造所有 git 调用共享的固定安全参数"""
    return [
        str(git),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.pager=cat",
        "-c",
        "diff.external=",
        "-c",
        "diff.noprefix=false",
        "-c",
        "diff.mnemonicPrefix=false",
        *_filter_overrides(git, repo),
    ]


@dataclass(frozen=True)
class _GitLayout:
    """工作区与 Git 顶层目录的相对位置

    ``workspace_prefix``：workspace 相对 top-level 的前缀（top 为祖先时）；
    ``top_prefix``：top-level 相对 workspace 的前缀（top 在 workspace 内时）。
    二者恰有一个非 ``None``，相等时为 ``"."``。
    """

    top: Path
    workspace_prefix: str | None
    top_prefix: str | None


def _is_within(parent: Path, child: Path) -> bool:
    """大小写不敏感的目录包含判断"""
    try:
        common = os.path.commonpath(
            [os.path.normcase(str(parent)), os.path.normcase(str(child))]
        )
    except ValueError:
        return False
    return common == os.path.normcase(str(parent))


def _relative_posix(parent: Path, child: Path) -> str:
    """计算规范相对 POSIX 路径"""
    relative = os.path.relpath(str(child), str(parent))
    if relative == ".":
        return "."
    return relative.replace(os.sep, "/")


def _git_layout(git: Path, workspace_root: Path, cwd: Path) -> _GitLayout | None:
    """用固定 ``git rev-parse --show-toplevel`` 计算仓库顶层"""
    try:
        result = _run_bounded(
            [str(git), "rev-parse", "--show-toplevel"],
            cwd=cwd,
            timeout_seconds=GIT_TIMEOUT_SECONDS,
            env=_git_environment(),
            max_output_bytes=8192,
        )
    except OSError:
        return None
    if result.timed_out or result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    top = Path(os.path.realpath(text))
    workspace = Path(os.path.realpath(workspace_root))
    if os.path.normcase(str(top)) == os.path.normcase(str(workspace)):
        return _GitLayout(top=top, workspace_prefix=".", top_prefix=".")
    if _is_within(top, workspace):
        return _GitLayout(
            top=top,
            workspace_prefix=_relative_posix(top, workspace),
            top_prefix=None,
        )
    if _is_within(workspace, top):
        return _GitLayout(
            top=top,
            workspace_prefix=None,
            top_prefix=_relative_posix(workspace, top),
        )
    return None


def _to_workspace_path(layout: _GitLayout, git_path: str) -> str | None:
    """把 Git 顶层相对路径映射为工作区相对路径；越界返回 ``None``"""
    candidate = PurePosixPath(git_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    if layout.workspace_prefix is not None:
        prefix = layout.workspace_prefix
        if prefix == ".":
            return candidate.as_posix()
        try:
            return candidate.relative_to(prefix).as_posix()
        except ValueError:
            return None
    prefix = layout.top_prefix or "."
    if prefix == ".":
        return candidate.as_posix()
    return f"{prefix}/{candidate.as_posix()}"


def _to_top_path(layout: _GitLayout, workspace_path: str) -> str | None:
    """把工作区相对路径映射为 Git 顶层相对路径；无法映射返回 ``None``"""
    if workspace_path in ("", "."):
        return layout.workspace_prefix if layout.workspace_prefix is not None else "."
    if layout.workspace_prefix is not None:
        prefix = layout.workspace_prefix
        if prefix == ".":
            return workspace_path
        return f"{prefix}/{workspace_path}"
    prefix = layout.top_prefix or "."
    if prefix == ".":
        return workspace_path
    if workspace_path == prefix:
        return "."
    if workspace_path.startswith(prefix + "/"):
        return workspace_path[len(prefix) + 1 :]
    return None


def _literal_pathspec(path: str) -> str:
    """构造 literal pathspec，避免通配符/魔数解释文件名"""
    return f":(literal){path}"


def _decode(data: bytes) -> str:
    """把子进程输出解码为文本，非法字节替换而不是抛出"""
    return data.decode("utf-8", errors="replace")


def build_repo_read_tools(
    workspace_root: Path | str,
    *,
    rg_path: Path | str | None = None,
    git_path: Path | str | None = None,
    on_read: Callable[[str, Path, int, int], None] | None = None,
) -> tuple[BaseTool, ...]:
    """构造绑定到固定工作区的五个只读工具

    ``workspace_root`` 只能由应用装配层注入；``rg_path`` / ``git_path``
    仅用于测试或部署显式指定，模型无法访问这些参数。``on_read`` 是可选的
    读取账本回调（V0.4 先读后写版本守卫的数据来源），参数为
    ``(相对路径, 绝对路径, 字节数, mtime_ns)``，由装配层注入，
    模型无法访问。
    """
    root = Path(workspace_root).expanduser().resolve()
    root_real = Path(os.path.realpath(root))
    rg = resolve_ripgrep(rg_path)
    git = resolve_git(git_path)

    @tool
    def list_directory(relative_path: str = ".", offset: int = 0) -> str:
        """只读工具：列出工作区内一个目录的直接子项（非递归）。

        参数 relative_path 为工作区相对路径，默认 "."。offset 用于分页。
        最多返回 100 项，按名称稳定排序；敏感路径、符号链接与 reparse point
        不会出现在结果中。每个子项都先解析真实长文件名再做敏感策略，
        因此 8.3 短别名不会泄露。返回 name/type/size、truncated 与 next_offset。
        """
        try:
            resolved = resolve_workspace_path(root, relative_path, expect="dir")
        except PathPolicyError as error:
            return _policy_error(error)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return _error("INVALID_ARGUMENT", "offset must be a non-negative integer")

        try:
            with os.scandir(resolved.absolute) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except PermissionError:
            return _error("FS_PERMISSION_DENIED", "directory is not accessible")
        except OSError:
            return _error("FS_UNAVAILABLE", "directory could not be read")

        items: list[dict[str, Any]] = []
        for entry in entries:
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(entry_stat.st_mode) or getattr(
                entry_stat, "st_file_attributes", 0
            ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                continue
            child_relative = canonical_relative_if_within(root_real, Path(entry.path))
            if child_relative is None or is_sensitive_relative(child_relative):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                entry_type = "dir"
            elif stat.S_ISREG(entry_stat.st_mode):
                entry_type = "file"
            else:
                entry_type = "other"
            items.append(
                {
                    "name": PurePosixPath(child_relative).name,
                    "path": child_relative,
                    "type": entry_type,
                    "size": entry_stat.st_size if entry_type == "file" else None,
                }
            )

        window = items[offset : offset + MAX_LIST_ENTRIES]
        next_offset = (
            offset + len(window) if offset + len(window) < len(items) else None
        )
        return _json(
            {
                "ok": True,
                "path": resolved.relative,
                "entries": window,
                "truncated": next_offset is not None,
                "next_offset": next_offset,
            }
        )

    @tool
    def read_text_file(
        relative_path: str,
        start_line: int = 1,
        max_lines: int = MAX_READ_LINES,
    ) -> str:
        """只读工具：按行读取工作区内的 UTF-8 文本文件。

        relative_path 为工作区相对路径；start_line 从 1 开始，单次最多 200 行，
        每行最多 2000 字符，文件最大 256 KiB，总响应受字节上限约束。
        打开文件时校验打开前后的文件身份与规范路径，降低 TOCTOU 风险
        （不声称操作系统级 sandbox）。返回带行号的文本、eof、truncated 与
        next_start_line。二进制、非 UTF-8、超限、目录、不存在或无权限都会
        返回明确错误码。
        """
        try:
            resolved = resolve_workspace_path(root, relative_path, expect="file")
        except PathPolicyError as error:
            return _policy_error(error)
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or start_line < 1
        ):
            return _error("INVALID_ARGUMENT", "start_line must be an integer >= 1")
        if (
            isinstance(max_lines, bool)
            or not isinstance(max_lines, int)
            or not 1 <= max_lines <= MAX_READ_LINES
        ):
            return _error(
                "INVALID_ARGUMENT",
                f"max_lines must be an integer between 1 and {MAX_READ_LINES}",
            )

        try:
            with open_verified_binary(root, resolved) as handle:
                raw = handle.read(MAX_READ_FILE_BYTES + 1)
                if on_read is not None:
                    read_stat = os.fstat(handle.fileno())
                    on_read(
                        resolved.relative,
                        resolved.absolute,
                        read_stat.st_size,
                        read_stat.st_mtime_ns,
                    )
        except PathPolicyError as error:
            return _policy_error(error)
        except PermissionError:
            return _error("FS_PERMISSION_DENIED", "file is not accessible")
        except OSError:
            return _error("FS_UNAVAILABLE", "file could not be read")
        if len(raw) > MAX_READ_FILE_BYTES:
            return _error(
                "FS_TOO_LARGE",
                f"file exceeds the {MAX_READ_FILE_BYTES} byte limit",
            )
        if b"\x00" in raw[:8192]:
            return _error("FS_BINARY", "file appears to be binary")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _error("FS_NOT_UTF8", "file is not valid UTF-8")

        lines = text.splitlines()
        start_index = start_line - 1
        window = lines[start_index : start_index + max_lines]
        payload_lines: list[dict[str, Any]] = []
        used = 0
        truncated = False
        for offset, line in enumerate(window):
            clipped = line[:MAX_LINE_CHARS]
            if len(line) > MAX_LINE_CHARS:
                truncated = True
            item = {"number": start_line + offset, "text": redact_text(clipped)}
            cost = len(_json(item).encode("utf-8")) + 1
            if used + cost > MAX_RESPONSE_BYTES - 512:
                truncated = True
                break
            used += cost
            payload_lines.append(item)

        consumed = len(payload_lines)
        eof = start_index + consumed >= len(lines)
        next_start_line = None if eof else start_line + consumed
        return _json(
            {
                "ok": True,
                "path": resolved.relative,
                "start_line": start_line,
                "lines": payload_lines,
                "eof": eof,
                "truncated": truncated or not eof,
                "next_start_line": next_start_line,
            }
        )

    @tool
    def search_text(
        query: str,
        relative_path: str = ".",
        offset: int = 0,
    ) -> str:
        """只读工具：在工作区内做固定字符串全文搜索。

        query 为字面量（非正则），最多 256 字符；relative_path 为工作区相对路径。
        使用固定的 ripgrep 绝对路径与固定参数，模型不能指定任何选项。
        跳过符号链接与敏感路径，限制扫描文件数、深度、每文件匹配数与总结果数。
        返回 path、line、脱敏后的 text、truncated、scan_truncated 与 next_offset。
        ``next_offset`` 只对已返回的匹配分页；``scan_truncated=True`` 表示
        仍有文件未被扫描，不能通过 offset 续扫。
        """
        if not isinstance(query, str) or not query:
            return _error("INVALID_ARGUMENT", "query must be a non-empty string")
        if len(query) > SEARCH_PATTERN_MAX_CHARS:
            return _error(
                "INVALID_ARGUMENT",
                f"query must be at most {SEARCH_PATTERN_MAX_CHARS} characters",
            )
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return _error("INVALID_ARGUMENT", "offset must be a non-negative integer")
        if rg is None:
            return _error("RG_NOT_FOUND", "ripgrep executable is not available")
        try:
            resolved = resolve_workspace_path(root, relative_path)
        except PathPolicyError as error:
            return _policy_error(error)

        files, scan_truncated = collect_regular_files(
            root,
            resolved.absolute,
            max_files=MAX_SEARCH_FILES,
            max_depth=MAX_SEARCH_DEPTH,
        )
        if not files:
            return _json(
                {
                    "ok": True,
                    "query": query,
                    "matches": [],
                    "truncated": scan_truncated,
                    "scan_truncated": scan_truncated,
                    "output_truncated": False,
                    "next_offset": None,
                    "timed_out": False,
                }
            )

        argv = [
            str(rg),
            "--no-config",
            "--no-ignore",
            "--hidden",
            "--color",
            "never",
            "--no-heading",
            "--line-number",
            "--with-filename",
            "--max-count",
            str(MAX_SEARCH_MATCHES_PER_FILE),
            "-F",
            "-e",
            query,
            "--",
            *files,
        ]
        try:
            process_result = _run_bounded(
                argv,
                cwd=root,
                timeout_seconds=SEARCH_TIMEOUT_SECONDS,
            )
        except OSError:
            return _error("RG_FAILED", "search process could not be started")

        matches: list[dict[str, Any]] = []
        for line in _decode(process_result.stdout).splitlines():
            if line.count(":") < 2:
                continue
            path_text, line_text, content = line.split(":", 2)
            if is_sensitive_relative(path_text):
                continue
            try:
                line_number = int(line_text)
            except ValueError:
                continue
            matches.append(
                {
                    "path": path_text,
                    "line": line_number,
                    "text": redact_text(content[:MAX_LINE_CHARS]),
                }
            )

        window = matches[offset : offset + MAX_SEARCH_RESULTS]
        next_offset = (
            offset + len(window) if offset + len(window) < len(matches) else None
        )
        truncated = (
            next_offset is not None
            or process_result.timed_out
            or scan_truncated
            or process_result.truncated
        )
        payload = {
            "ok": True,
            "query": query,
            "matches": window,
            "truncated": truncated,
            "scan_truncated": scan_truncated,
            "output_truncated": process_result.truncated,
            "next_offset": next_offset,
            "timed_out": process_result.timed_out,
        }
        if process_result.stderr:
            payload["stderr"] = redact_text(
                truncate_utf8(_decode(process_result.stderr), 1024).strip()
            )
        return _json(payload)

    @tool
    def git_status(repo_path: str = ".") -> str:
        """只读工具：返回工作区内 Git 仓库的 porcelain 状态。

        repo_path 为工作区相对路径，默认 "."。仅执行固定 git status 子命令，
        不使用 Shell，不接受任意参数。X=暂存区状态、Y=工作区状态、
        "??"=未跟踪。通过固定 ``rev-parse --show-toplevel`` 定位仓库顶层，
        只输出工作区内的路径（工作区是更大仓库的子目录时不会泄露同级路径），
        输出映射为工作区相对路径。禁用 fsmonitor/untracked cache/pager、
        外部 diff 与仓库本地 filter 驱动，敏感路径不会出现在结果中。
        """
        if git is None:
            return _error("GIT_NOT_FOUND", "git executable is not available")
        try:
            resolved = resolve_workspace_path(root, repo_path, expect="dir")
        except PathPolicyError as error:
            return _policy_error(error)

        layout = _git_layout(git, root, resolved.absolute)
        if layout is None:
            return _error("GIT_NOT_REPO", "path is not inside a Git repository")
        repo_top_path = _to_top_path(layout, resolved.relative)
        pathspec = _literal_pathspec("." if repo_top_path is None else repo_top_path)

        argv = [
            *_git_base_args(git, layout.top),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=all",
            "--",
            pathspec,
        ]
        try:
            process_result = _run_bounded(
                argv,
                cwd=layout.top,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=_git_environment(),
            )
        except OSError:
            return _error("GIT_FAILED", "git process could not be started")
        if process_result.timed_out:
            return _error("GIT_TIMEOUT", "git status timed out")
        if process_result.returncode != 0:
            return _error("GIT_FAILED", "git status failed")

        raw_entries: list[dict[str, str]] = []
        chunks = _decode(process_result.stdout).split("\x00")
        index = 0
        while index < len(chunks):
            chunk = chunks[index]
            index += 1
            if not chunk or len(chunk) < 4:
                continue
            status_code = chunk[:2]
            path_text = chunk[3:]
            mapped = _to_workspace_path(layout, path_text)
            if mapped is None:
                continue
            if status_code[0] in ("R", "C") and index < len(chunks):
                # 重命名/复制条目后紧跟原路径；两条都必须在工作区内
                original = chunks[index]
                index += 1
                mapped_original = _to_workspace_path(layout, original)
                if mapped_original is None or is_sensitive_relative(mapped_original):
                    continue
                if is_sensitive_relative(mapped):
                    continue
            elif is_sensitive_relative(mapped):
                continue
            raw_entries.append({"status": status_code, "path": redact_text(mapped)})

        truncated = len(raw_entries) > MAX_GIT_ENTRIES or process_result.truncated
        return _json(
            {
                "ok": True,
                "repo_path": resolved.relative,
                "entries": raw_entries[:MAX_GIT_ENTRIES],
                "truncated": truncated,
            }
        )

    @tool
    def git_diff(
        repo_path: str = ".",
        paths: list[str] | None = None,
        staged: bool = False,
    ) -> str:
        """只读工具：返回工作区内 Git 仓库的 unified diff。

        repo_path 为工作区相对路径；paths 为可选的相对路径列表；staged=True
        查看暂存区与 HEAD 的差异。通过固定 ``rev-parse --show-toplevel``
        在仓库顶层执行，把工作区相对路径转换为 literal 顶层 pathspec，
        强制 ``--src-prefix=a/ --dst-prefix=b/``，因此子目录、空格文件名和
        本地 diff.noprefix/mnemonicPrefix 配置都不会让 diff 静默消失。
        仅执行固定 git diff 子命令，强制 --no-ext-diff --no-textconv
        --no-color --no-relative，禁用仓库本地 filter 驱动。敏感路径的整段
        diff 会被丢弃，内容做密钥脱敏并受字节上限约束。
        """
        if git is None:
            return _error("GIT_NOT_FOUND", "git executable is not available")
        try:
            resolved = resolve_workspace_path(root, repo_path, expect="dir")
        except PathPolicyError as error:
            return _policy_error(error)

        layout = _git_layout(git, root, resolved.absolute)
        if layout is None:
            return _error("GIT_NOT_REPO", "path is not inside a Git repository")

        selected: list[str] = []
        for raw_path in paths or []:
            try:
                candidate = resolve_workspace_path(root, raw_path, must_exist=False)
            except PathPolicyError as error:
                return _policy_error(error)
            top_path = _to_top_path(layout, candidate.relative)
            if top_path is None:
                return _error(
                    "GIT_PATH_OUTSIDE_REPO",
                    "selected path is outside the Git repository",
                )
            selected.append(top_path)

        if selected:
            pathspecs = [_literal_pathspec(path) for path in selected]
        else:
            repo_top_path = _to_top_path(layout, resolved.relative)
            pathspecs = [
                _literal_pathspec("." if repo_top_path is None else repo_top_path)
            ]

        argv = [
            *_git_base_args(git, layout.top),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--no-relative",
            "--src-prefix=a/",
            "--dst-prefix=b/",
        ]
        if staged:
            argv.append("--cached")
        argv.append("--")
        argv.extend(pathspecs)

        try:
            process_result = _run_bounded(
                argv,
                cwd=layout.top,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
                env=_git_environment(),
            )
        except OSError:
            return _error("GIT_FAILED", "git process could not be started")
        if process_result.timed_out:
            return _error("GIT_TIMEOUT", "git diff timed out")
        if process_result.returncode != 0:
            return _error("GIT_FAILED", "git diff failed")

        text = _decode(process_result.stdout)
        kept_sections: list[str] = []
        unparsed_sections = 0
        for section in re.split(r"(?m)^(?=diff --git )", text):
            if not section.startswith("diff --git "):
                continue
            header = section.splitlines()[0]
            parsed = _split_diff_header(header)
            if parsed is None:
                unparsed_sections += 1
                continue
            workspace_paths: list[str] = []
            outside = False
            for path_text in parsed:
                mapped = _to_workspace_path(layout, path_text)
                if mapped is None:
                    outside = True
                    break
                workspace_paths.append(mapped)
            if outside:
                continue
            if any(is_sensitive_relative(path) for path in workspace_paths):
                continue
            kept_sections.append(section)

        diff_text = redact_text("".join(kept_sections))
        truncated = (
            len(diff_text.encode("utf-8")) > GIT_MAX_BYTES or process_result.truncated
        )
        if truncated:
            diff_text = truncate_utf8(diff_text, GIT_MAX_BYTES)
        return _json(
            {
                "ok": True,
                "repo_path": resolved.relative,
                "diff": diff_text,
                "truncated": truncated,
                "unparsed_sections": unparsed_sections,
            }
        )

    for repo_tool in (
        list_directory,
        read_text_file,
        search_text,
        git_status,
        git_diff,
    ):
        repo_tool.metadata = {"effect": "read_only"}

    return (list_directory, read_text_file, search_text, git_status, git_diff)


_C_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    '"': '"',
    "\\": "\\",
}


def _parse_c_quoted(text: str) -> tuple[str, str] | None:
    """解析 Git 的 C 风格引号路径，返回 ``(解码路径, 剩余文本)``"""
    if not text.startswith('"'):
        return None
    characters: list[str] = []
    index = 1
    while index < len(text):
        character = text[index]
        if character == '"':
            return "".join(characters), text[index + 1 :]
        if character == "\\":
            index += 1
            if index >= len(text):
                return None
            escape = text[index]
            if escape in _C_ESCAPES:
                characters.append(_C_ESCAPES[escape])
                index += 1
            elif escape in "01234567":
                digits = text[index : index + 3]
                if len(digits) != 3 or any(digit not in "01234567" for digit in digits):
                    return None
                characters.append(chr(int(digits, 8)))
                index += 3
            else:
                characters.append(escape)
                index += 1
        else:
            characters.append(character)
            index += 1
    return None


def _split_diff_header(header: str) -> tuple[str, str] | None:
    """解析 ``diff --git a/... b/...``，支持空格与 C 风格引号路径"""
    prefix = "diff --git "
    if not header.startswith(prefix):
        return None
    remainder = header[len(prefix) :]
    if remainder.startswith('"'):
        parsed = _parse_c_quoted(remainder)
        if parsed is None:
            return None
        first, tail = parsed
        if not tail.startswith(" "):
            return None
        tail = tail[1:]
        if not tail.startswith('"'):
            return None
        parsed_second = _parse_c_quoted(tail)
        if parsed_second is None:
            return None
        second, tail = parsed_second
        if tail:
            return None
    else:
        # 未加引号时路径可能含空格；--no-renames 下 a/ 与 b/ 路径相同，
        # 取使两侧相等的 " b/" 分隔点。
        split_index: int | None = None
        for index in range(len(remainder)):
            if not remainder.startswith(" b/", index):
                continue
            left = remainder[:index]
            right = remainder[index + 1 :]
            if (
                left.startswith("a/")
                and right.startswith("b/")
                and left[2:] == right[2:]
            ):
                split_index = index
                break
        if split_index is None:
            return None
        first = remainder[:split_index]
        second = remainder[split_index + 1 :]

    if not first.startswith("a/") or not second.startswith("b/"):
        return None
    return first[2:], second[2:]
