"""共享的有界子进程执行工具

所有需要启动子进程的组件（只读 rg/git 工具与命令执行服务）共用本模块：
运行中就有界的输出捕获、超时/超限即终止整棵进程树、固定 taskkill 路径、
最小化子进程环境。调用方拿不到原始进程对象，只拿结构化结果。
"""

from __future__ import annotations

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
    """有界子进程结果"""

    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool
    truncated: bool


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
    argv: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    max_output_bytes: int = MAX_SUBPROCESS_OUTPUT_BYTES,
) -> ProcessResult:
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
        env=env if env is not None else child_environment(),
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

    try:
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
            terminate_process_tree(process)
    except KeyboardInterrupt:
        # 取消不是普通失败：先终止整棵进程树再向上传播，
        # 绝不让子进程在解释器让出后继续运行
        terminate_process_tree(process)
        drain_readers(process, threads)
        raise

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
    )
