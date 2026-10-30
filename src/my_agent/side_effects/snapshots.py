"""V0.4 文件快照存储与确定性回滚服务

- 快照存放在应用状态目录内（不在 Agent 可写的工作目录中），Agent 无法
  通过写入工具覆盖快照或伪造回滚数据。
- 回滚是确定性服务行为：从快照恢复旧内容，或删除本次创建的新文件；
  不自动执行 ``git revert``/``reset``，也不向 Agent 暴露删除工具。
"""

from __future__ import annotations

import os
from pathlib import Path

from my_agent.side_effects.intent_store import SnapshotRecord

_CHUNK_BYTES = 256 * 1024


class SnapshotStoreError(RuntimeError):
    """快照无法写入或恢复时抛出"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class SnapshotStore:
    """快照文件与元数据的管理"""

    def __init__(self, directory: Path) -> None:
        """初始化快照目录"""
        self._directory = directory
        directory.mkdir(parents=True, exist_ok=True)

    def data_path(self, snapshot_id: str) -> Path:
        """返回快照数据文件的存储路径"""
        return self._directory / f"{snapshot_id}.bin"

    def save_file(self, snapshot_id: str, source: Path) -> int:
        """把现有文件内容复制为快照，返回快照字节数

        源文件不可读或体积超过快照上限时抛出异常（fail closed，不写入）。
        """
        data_path = self.data_path(snapshot_id)
        try:
            with (
                open(source, "rb") as reader,
                open(
                    data_path,
                    "wb",
                ) as writer,
            ):
                size = 0
                while True:
                    chunk = reader.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    writer.write(chunk)
                    size += len(chunk)
        except OSError as error:
            try:
                data_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SnapshotStoreError(
                "SNAPSHOT_UNAVAILABLE",
                "existing file could not be snapshotted",
            ) from error
        return size

    def write_bytes(self, snapshot_id: str, data: bytes) -> None:
        """直接写入快照数据（测试与恢复辅助）"""
        self.data_path(snapshot_id).write_bytes(data)

    def restore(self, snapshot: SnapshotRecord, target: Path) -> None:
        """确定性回滚：恢复旧内容或删除本次创建的文件

        ``overwritten`` 用临时文件 + 原子替换恢复；``created`` 删除本次
        创建的文件（仅由确定性回滚服务执行，不作为 Agent 能力暴露）。
        """
        if snapshot.kind == "created":
            try:
                target.unlink()
            except FileNotFoundError:
                return
            except OSError as error:
                raise SnapshotStoreError(
                    "ROLLBACK_FAILED",
                    "created file could not be removed during rollback",
                ) from error
            return

        data_path = self.data_path(snapshot.snapshot_id)
        try:
            data = data_path.read_bytes()
        except OSError as error:
            raise SnapshotStoreError(
                "ROLLBACK_FAILED",
                "snapshot data could not be read",
            ) from error
        temp_path = target.parent / f"{target.name}.rollback-{snapshot.snapshot_id[:8]}"
        try:
            with open(temp_path, "wb") as writer:
                writer.write(data)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temp_path, target)
        except OSError as error:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise SnapshotStoreError(
                "ROLLBACK_FAILED",
                "snapshot could not be restored",
            ) from error

    def delete_file(self, snapshot_id: str) -> None:
        """删除快照数据文件（元数据由 IntentStore 负责）"""
        try:
            self.data_path(snapshot_id).unlink(missing_ok=True)
        except OSError as error:
            raise SnapshotStoreError(
                "SNAPSHOT_UNAVAILABLE",
                "snapshot data file could not be removed",
            ) from error

    def list_files(self) -> list[str]:
        """列出快照目录中的数据文件名（用于孤儿清理）"""
        return [entry.name for entry in self._directory.iterdir() if entry.is_file()]
