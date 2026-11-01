"""共享的有界子进程执行工具

所有需要启动子进程的组件（只读 rg/git 工具与命令执行服务）共用本模块：
运行中就有界的输出捕获、超时/超限即终止整棵进程树、固定 taskkill 路径、
最小化子进程环境。调用方拿不到原始进程对象，只拿结构化结果。

Windows 进程树契约（返工 2026-11-01）：

- ``args`` 传**列表**时按 C 运行时规则序列化（rg/git 等固定参数场景）；
  传**字符串**时原样作为 ``lpCommandLine`` 交给 CreateProcess——供
  ``cmd /d /s /c`` 使用，避免一份 shell 文本被两套转义规则改写。
- 子进程以 ``CREATE_SUSPENDED`` 创建，先挂入 **Kill-on-close Job Object**
  再恢复执行：全部后代默认继承进入 Job，无 breakaway 通道。
- 完成条件是**根进程、输出读取线程与整个 Job 的活动进程数**三者全部
  到达终态；只等根 PID 不再被视为完成。
- 超时/输出超限/Ctrl+C：先 ``TerminateJobObject`` 再 taskkill 兜底，
  并等待整棵树退出后才返回。
- 本进程意外崩溃时，Job 句柄由操作系统关闭，Kill-on-close 兑现
  "树随之终止"，不留孤儿副作用进程。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 单次子进程 stdout/stderr 各自的硬上限：读取线程边读边丢弃，
# 达到上限立即终止进程树，绝不把无限输出留在内存或管道里。
MAX_SUBPROCESS_OUTPUT_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_KILL_TIMEOUT_SECONDS = 5
_DRAIN_JOIN_SECONDS = 2

_TREE_DRAIN_POLL_SECONDS = 0.02


def child_environment() -> dict[str, str]:
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


def taskkill_executable() -> Path | None:
    """返回固定的 System32\\taskkill.exe，避免依赖 PATH 解析"""
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not system_root:
        return None
    candidate = Path(system_root) / "System32" / "taskkill.exe"
    return candidate if candidate.is_file() else None


def terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """终止子进程及其派生的子进程；taskkill 不可用时回退到 kill"""
    if os.name == "nt":
        taskkill = taskkill_executable()
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
                    env=child_environment(),
                )
            except (OSError, subprocess.SubprocessError):
                pass
    try:
        process.kill()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Windows Job Object：整棵进程树的生命周期接管
# ---------------------------------------------------------------------------

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JobObjectExtendedLimitInformation = 9
_JobObjectBasicAccountingInformation = 1
_TH32CS_SNAPTHREAD = 0x00000004
_THREAD_SUSPEND_RESUME = 0x0002


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


def _kernel32() -> ctypes.WinDLL:
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateJobObjectW.restype = ctypes.c_void_p
    library.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    library.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    library.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    library.AssignProcessToJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    library.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    library.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    library.Thread32First.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
    library.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.POINTER(_THREADENTRY32)]
    library.OpenThread.restype = ctypes.c_void_p
    library.OpenThread.argtypes = [
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    library.ResumeThread.argtypes = [ctypes.c_void_p]
    return library


def _create_kill_on_close_job() -> int | None:
    """创建 Kill-on-close 的 Job 对象；失败返回 None（调用方降级）"""
    library = _kernel32()
    job = library.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not library.SetInformationJobObject(
        job,
        _JobObjectExtendedLimitInformation,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
        None,
    ):
        library.CloseHandle(job)
        return None
    return job


def _assign_process_to_job(job: int, process_handle: int | None) -> bool:
    """把已创建（挂起）的根进程挂入 Job；后代默认全部继承"""
    if not process_handle:
        return False
    return bool(_kernel32().AssignProcessToJobObject(job, process_handle))


def _job_active_processes(job: int) -> int | None:
    """查询 Job 内活动进程数；查询失败返回 None（按未排水处理）"""
    info = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    ok = _kernel32().QueryInformationJobObject(
        job,
        _JobObjectBasicAccountingInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
        None,
    )
    if not ok:
        return None
    return int(info.ActiveProcesses)


def _terminate_job(job: int) -> None:
    """终止 Job 内的全部进程（比 taskkill /T 更可靠）"""
    _kernel32().TerminateJobObject(job, 1)


def _close_job(job: int) -> None:
    _kernel32().CloseHandle(job)


def _resume_process_threads(pid: int) -> bool:
    """恢复以 CREATE_SUSPENDED 创建的进程的全部线程并验证成功

    返回 ``False`` 表示没有任何线程被成功恢复或任一恢复调用失败；
    调用方必须清理尚未运行的根进程，不得继续执行。
    """
    library = _kernel32()
    snapshot = library.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot:
        return False
    resumed = False
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        found = library.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = library.OpenThread(
                    _THREAD_SUSPEND_RESUME,
                    False,
                    entry.th32ThreadID,
                )
                if thread:
                    # ResumeThread 返回之前的挂起计数；-1（0xFFFFFFFF）为失败
                    result = library.ResumeThread(thread)
                    library.CloseHandle(thread)
                    if result == 0xFFFFFFFF:
                        return False
                    resumed = True
            found = library.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        library.CloseHandle(snapshot)
    return resumed


def _terminate_tree(process: subprocess.Popen[bytes], job: int | None) -> bool:
    """终止整棵进程树：Job 终止为主，taskkill /T 与 kill 兜底

    返回 Job 终止调用是否成功；最终以 ``_await_tree_exit`` 的确认为准。
    """
    job_terminated = True
    if job is not None:
        try:
            job_terminated = bool(_terminate_job(job))
        except OSError:
            job_terminated = False
    terminate_process_tree(process)
    return job_terminated


def _tree_drained(job: int | None) -> bool:
    """整棵树是否已到终态；查询失败按未排水处理（宁可等到超时）"""
    if job is None:
        return True
    active = _job_active_processes(job)
    return active is not None and active == 0


def _await_tree_exit(process: subprocess.Popen[bytes], job: int | None) -> bool:
    """终止后有界等待整棵树真正退出

    返回 ``True`` 表示已确认根进程退出且 Job 活动进程数归零；
    返回 ``False`` 表示期限耗尽仍无法确认——调用方必须保留
    unknown/终止未确认语义，绝不能当作已确认处理。
    """
    deadline = time.monotonic() + _KILL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _tree_drained(job) and process.poll() is not None:
            return True
        time.sleep(_TREE_DRAIN_POLL_SECONDS)
    return False


def _cleanup_unstarted_root(process: subprocess.Popen[bytes]) -> None:
    """清理尚未开始执行的挂起根进程及其管道，不留挂起残留"""
    try:
        process.kill()
    except OSError:
        pass
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
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
class ProcessResult:
    """有界子进程结果

    ``tree_exit_confirmed`` 为 ``False`` 表示终止后未能确认整棵树到达
    终态（等待耗尽/查询失败），调用方必须按"结果未知"处理，不得报告
    普通成功或失败。
    """

    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    truncated: bool
    tree_exit_confirmed: bool = True


class ProcessTreeControlError(RuntimeError):
    """进程树控制机制（Job Object/挂起/恢复）不可用

    此时命令**尚未开始执行**（根进程仍处于挂起态并已被清理）；
    调用方必须 fail closed 返回明确失败，绝不能降级为无 Job 执行。
    """


def drain_readers(
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


def run_bounded(
    args: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> ProcessResult:
    """执行固定命令，运行中就有界捕获 stdout/stderr

    ``args`` 为列表时按 C 运行时规则序列化（固定参数场景）；
    为字符串时原样作为 Windows 命令行交给 CreateProcess（``cmd /s /c``
    场景），绝不经过 ``list2cmdline`` 的二次转义。

    完成条件：根进程退出 **且** 读取线程结束 **且** 整棵进程树
    （Job 活动进程数归零）到达终态。超时/超限/Ctrl+C 先终止整棵树并
    等待其退出，然后才返回。
    """
    if isinstance(args, str) and os.name != "nt":
        raise ValueError("raw command-line strings are only supported on Windows")

    use_job = os.name == "nt"
    # 微软文档：CREATE_SUSPENDED = 0x00000004。subprocess 模块**没有**
    # 暴露该常量（Python 3.13.2 实测），getattr 缺省为 0 会让进程先运行、
    # 后绑定 Job，存在接管竞态——必须使用字面常量。
    _CREATE_SUSPENDED = 0x00000004
    creationflags = _CREATE_SUSPENDED if use_job else 0
    process = subprocess.Popen(
        args,
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env if env is not None else child_environment(),
        bufsize=0,
        creationflags=creationflags,
    )

    job: int | None = None
    overflow = threading.Event()
    threads: list[threading.Thread] = []
    tree_exit_confirmed = True
    try:
        if use_job:
            # Job 是兑现生命周期契约的必需机制：创建/绑定/恢复任一失败
            # 都在恢复执行前 fail closed，清理挂起的根进程，绝不降级
            job = _create_kill_on_close_job()
            if job is None:
                raise ProcessTreeControlError(
                    "could not create the job object; command not executed"
                )
            if not _assign_process_to_job(job, process._handle):
                raise ProcessTreeControlError(
                    "could not assign the root process to the job object; "
                    "command not executed"
                )
            if not _resume_process_threads(process.pid):
                raise ProcessTreeControlError(
                    "could not resume the suspended root process; command not executed"
                )

        assert process.stdout is not None and process.stderr is not None
        stdout_collector = _StreamCollector(process.stdout, max_output_bytes)
        stderr_collector = _StreamCollector(process.stderr, max_output_bytes)
        threads = [
            threading.Thread(
                target=stdout_collector.run, args=(overflow,), daemon=True
            ),
            threading.Thread(
                target=stderr_collector.run, args=(overflow,), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()

        timed_out = False
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                process_done = process.poll() is not None
                readers_done = all(not thread.is_alive() for thread in threads)
                tree_done = _tree_drained(job)
                if process_done and readers_done and tree_done:
                    break
                if overflow.is_set():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                time.sleep(0.01)

            if timed_out or overflow.is_set():
                _terminate_tree(process, job)
                # 返回前必须确认整棵树已到终态；无法确认时由调用方按
                # "结果未知"处理，绝不带活进程报告普通结果
                tree_exit_confirmed = _await_tree_exit(process, job)
        except KeyboardInterrupt:
            # 取消不是普通失败：先终止整棵进程树并确认退出，再向上传播；
            # 无法确认时向上携带 tree_exit_unconfirmed 标记
            _terminate_tree(process, job)
            confirmed = _await_tree_exit(process, job)
            drain_readers(process, threads)
            if confirmed:
                raise
            cancelled = KeyboardInterrupt(
                "process tree exit unconfirmed after interrupt"
            )
            cancelled.tree_exit_unconfirmed = True  # type: ignore[attr-defined]
            raise cancelled from None

        drain_readers(process, threads)
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

        return ProcessResult(
            stdout=stdout_collector.data,
            stderr=stderr_collector.data,
            returncode=process.returncode,
            timed_out=timed_out,
            truncated=stdout_collector.truncated
            or stderr_collector.truncated
            or overflow.is_set(),
            tree_exit_confirmed=tree_exit_confirmed,
        )
    except ProcessTreeControlError:
        # fail closed：根进程仍处于挂起态、命令从未执行——清理残留并向上传播
        _cleanup_unstarted_root(process)
        raise
    finally:
        # 正常路径树已排水，关闭即无副作用；异常/崩溃路径由
        # Kill-on-close 兜底终止整棵树
        if job is not None:
            try:
                _close_job(job)
            except OSError:
                pass
