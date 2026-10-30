"""V0.4 写路径锁：按规范化真实路径划分的进程内锁与 fencing token

契约要点（agent_plan.md V0.4）：

- V0.4 单进程阶段使用进程内按路径锁；进入 V0.5.5 的 SSH 多进程阶段前，
  必须升级为应用状态库中的跨进程路径租约/锁。
- 锁键使用完成大小写和路径规范化后的真实目标路径。
- 获取锁、把 intent 切换为 executing、校验 fencing token 必须位于同一
  临界区；旧持有者失去锁（revoke/过期）后不能继续写入。
"""

from __future__ import annotations

import itertools
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass


class StaleFencingTokenError(RuntimeError):
    """fencing token 已失效：锁已被回收或转授，旧持有者不得继续写入"""


@dataclass
class _LockEntry:
    lock: threading.Lock
    current_token: int
    revoked: bool


class PathLockManager:
    """按规范化路径划分的进程内写锁，每个持有者获得单调递增 token"""

    def __init__(self) -> None:
        """初始化空锁表"""
        self._mutex = threading.Lock()
        self._entries: dict[str, _LockEntry] = {}
        self._tokens = itertools.count(1)

    @staticmethod
    def lock_key(canonical_path: str) -> str:
        """大小写与分隔符规范化后的锁键"""
        return os.path.normcase(canonical_path)

    @contextmanager
    def acquire(self, canonical_path: str) -> Iterator[int]:
        """持有指定路径的锁并返回 fencing token

        token 在锁真正被持有的临界区内分配；同一时刻同一路径只有一个
        持有者可以拿到有效 token。
        """
        key = self.lock_key(canonical_path)
        with self._mutex:
            entry = self._entries.get(key)
            if entry is None:
                entry = _LockEntry(
                    lock=threading.Lock(),
                    current_token=0,
                    revoked=False,
                )
                self._entries[key] = entry
        entry.lock.acquire()
        try:
            with self._mutex:
                token = next(self._tokens)
                entry.current_token = token
                entry.revoked = False
            yield token
        finally:
            # 释放锁后立即失效本 token：verify 在临界区之外必须失败
            with self._mutex:
                entry.current_token = 0
            entry.lock.release()

    def verify(self, canonical_path: str, token: int) -> None:
        """校验 fencing token；失效时抛出 StaleFencingTokenError"""
        key = self.lock_key(canonical_path)
        with self._mutex:
            entry = self._entries.get(key)
            if entry is None or entry.revoked or entry.current_token != token:
                raise StaleFencingTokenError(
                    "fencing token is no longer valid for this path"
                )

    def revoke(self, canonical_path: str) -> None:
        """吊销当前持有者的 token（启动恢复与测试使用）

        正在等待或已获得旧 token 的持有者之后调用 ``verify`` 会失败。
        """
        key = self.lock_key(canonical_path)
        with self._mutex:
            entry = self._entries.get(key)
            if entry is not None:
                entry.revoked = True
