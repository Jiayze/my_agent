"""V0.4 副作用 intent 存储：SQLite 短事务状态机与 grant 数据模型

契约要点（agent_plan.md 决策 16 / V0.4）：

- 副作用先记账、后执行：intent 在快照之前创建（prepared），快照持久化后
  进入 snapshot_ready，原子切换为 executing 后才允许真实写入，完成后
  记 done。每个转移都带期望前置状态，防止乱序。
- 只有进入 executing 之前的 intent 可以安全丢弃；任何 executing 在启动
  恢复时标记为 unknown（结果未知），绝不自动重试。
- grant（目录外一次性授权）在 V0.4 只交付数据模型与校验逻辑，不开放
  目录外执行。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intents (
    request_id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    request_id TEXT PRIMARY KEY,
    canonical_target TEXT NOT NULL,
    operation TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS shell_audits (
    request_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    duration_ms REAL,
    created_at TEXT NOT NULL
);
"""


def utc_now_iso() -> str:
    """返回 UTC 时间戳（ISO 8601，仅用于诊断与审计展示）"""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class IntentRecord:
    """一次副作用操作的持久化意图"""

    request_id: str
    operation: str
    relative_path: str
    canonical_path: str
    content_hash: str
    snapshot_id: str
    status: str
    created_at: str
    updated_at: str
    error: str | None = None


@dataclass(frozen=True)
class SnapshotRecord:
    """一次文件快照的元数据；数据体存放在快照目录"""

    snapshot_id: str
    request_id: str
    kind: str  # created | overwritten
    relative_path: str
    size: int


@dataclass(frozen=True)
class GrantRecord:
    """目录外一次性授权（V0.4 仅数据模型，不开放执行）"""

    request_id: str
    canonical_target: str
    operation: str
    content_hash: str
    status: str  # pending | approved | used | expired | rejected | revoked
    created_at: str
    expires_at: str
    used_at: str | None = None


def validate_grant(
    grant: GrantRecord,
    *,
    canonical_target: str,
    operation: str,
    content_hash: str,
    now: datetime,
) -> str | None:
    """校验 grant 是否可执行；返回 None 表示通过，否则返回拒绝原因码

    grant 与目标必须以同一种 canonical identity 比较：路径身份、内容哈希
    或操作类型任一变化即令旧 grant 失效；授权单次有效、过期失效。
    """
    if grant.status != "approved":
        return "GRANT_NOT_APPROVED"
    if grant.used_at is not None:
        return "GRANT_ALREADY_USED"
    try:
        expires_at = datetime.fromisoformat(grant.expires_at)
    except ValueError:
        return "GRANT_EXPIRED"
    if now >= expires_at:
        return "GRANT_EXPIRED"
    if os.path.normcase(grant.canonical_target) != os.path.normcase(canonical_target):
        return "GRANT_TARGET_MISMATCH"
    if grant.operation != operation:
        return "GRANT_OPERATION_MISMATCH"
    if grant.content_hash != content_hash:
        return "GRANT_CONTENT_MISMATCH"
    return None


class IntentStore:
    """基于 SQLite 的 intent/快照/grant 存储

    每个操作使用独立短连接与短事务，``PRAGMA synchronous=FULL`` 保证
    ``executing`` 转移落盘（崩溃恢复语义依赖它）。
    """

    def __init__(self, db_path: Path) -> None:
        """打开存储并初始化 schema"""
        self._db_path = db_path
        self._lock = threading.Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    # ---------- intents ----------

    def create_intent(self, record: IntentRecord) -> None:
        """持久化一个 prepared intent"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.operation,
                    record.relative_path,
                    record.canonical_path,
                    record.content_hash,
                    record.snapshot_id,
                    record.status,
                    record.created_at,
                    record.updated_at,
                    record.error,
                ),
            )

    def _transition(
        self,
        request_id: str,
        from_status: str,
        to_status: str,
        error: str | None = None,
    ) -> bool:
        """原子状态转移；前置状态不符时返回 False"""
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE intents SET status=?, updated_at=?, error=? "
                "WHERE request_id=? AND status=?",
                (to_status, utc_now_iso(), error, request_id, from_status),
            )
            return cursor.rowcount == 1

    def mark_snapshot_ready(self, request_id: str) -> bool:
        """prepared → snapshot_ready"""
        return self._transition(request_id, "prepared", "snapshot_ready")

    def mark_executing(self, request_id: str) -> bool:
        """snapshot_ready → executing（fsync 落盘后才会执行真实写入）"""
        return self._transition(request_id, "snapshot_ready", "executing")

    def mark_done(self, request_id: str) -> bool:
        """executing → done"""
        return self._transition(request_id, "executing", "done")

    def mark_failed(self, request_id: str, error: str) -> bool:
        """executing → failed（结果未知方向之一）"""
        return self._transition(request_id, "executing", "failed", error=error)

    def mark_unknown(self, request_id: str) -> bool:
        """executing → unknown（启动恢复：结果未知，等待人工检查）"""
        return self._transition(request_id, "executing", "unknown")

    def mark_rolled_back(self, request_id: str, from_status: str) -> bool:
        """done/failed/unknown → rolled_back（确定性回滚完成后落库）"""
        if from_status not in ("done", "failed", "unknown"):
            return False
        return self._transition(request_id, from_status, "rolled_back")

    def mark_rejected(self, request_id: str) -> bool:
        """prepared → rejected（策略拒绝或审批未通过，从未执行）"""
        return self._transition(request_id, "prepared", "rejected")

    def mark_command_executing(self, request_id: str) -> bool:
        """prepared → executing（host_command 专用：无快照阶段）

        写文件的 snapshot_ready 阶段对命令不存在；审批通过即进入 executing。
        """
        return self._transition(request_id, "prepared", "executing")

    # ---------- shell 审计 ----------

    def record_shell_audit(
        self,
        request_id: str,
        *,
        command: str,
        decision: str,
        decided_by: str,
        status: str,
        exit_code: int | None = None,
        timed_out: bool = False,
        duration_ms: float | None = None,
        created_at: str | None = None,
    ) -> None:
        """写入一条命令执行审计记录（策略/审批/结果终态各写一次）"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO shell_audits VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id,
                    command,
                    decision,
                    decided_by,
                    status,
                    exit_code,
                    1 if timed_out else 0,
                    duration_ms,
                    created_at or utc_now_iso(),
                ),
            )

    def list_shell_audits(self) -> list[dict[str, object]]:
        """返回全部命令审计记录（按写入顺序）"""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id, command, decision, decided_by, status, "
                "exit_code, timed_out, duration_ms, created_at FROM shell_audits"
            ).fetchall()
        keys = (
            "request_id",
            "command",
            "decision",
            "decided_by",
            "status",
            "exit_code",
            "timed_out",
            "duration_ms",
            "created_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def discard_intent(self, request_id: str) -> None:
        """删除尚未进入 executing 的 intent（安全丢弃）"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM intents WHERE request_id=? AND status IN "
                "('prepared', 'snapshot_ready')",
                (request_id,),
            )

    def get_intent(self, request_id: str) -> IntentRecord | None:
        """按 request_id 查询 intent"""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT request_id, operation, relative_path, canonical_path, "
                "content_hash, snapshot_id, status, created_at, updated_at, error "
                "FROM intents WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return IntentRecord(*row)

    def list_intents_by_status(self, *statuses: str) -> list[IntentRecord]:
        """按状态查询 intent 列表"""
        placeholders = ",".join("?" for _ in statuses)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT request_id, operation, relative_path, canonical_path, "
                "content_hash, snapshot_id, status, created_at, updated_at, error "
                f"FROM intents WHERE status IN ({placeholders})",
                statuses,
            ).fetchall()
        return [IntentRecord(*row) for row in rows]

    # ---------- snapshots ----------

    def register_snapshot(
        self,
        snapshot_id: str,
        request_id: str,
        *,
        kind: str,
        relative_path: str,
        size: int,
    ) -> None:
        """持久化快照元数据"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?)",
                (snapshot_id, request_id, kind, relative_path, size),
            )

    def get_snapshot(self, snapshot_id: str) -> SnapshotRecord | None:
        """按 snapshot_id 查询快照元数据"""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id, request_id, kind, relative_path, size "
                "FROM snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        return SnapshotRecord(*row)

    def list_snapshots(self) -> list[SnapshotRecord]:
        """返回全部快照元数据"""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT snapshot_id, request_id, kind, relative_path, size "
                "FROM snapshots",
            ).fetchall()
        return [SnapshotRecord(*row) for row in rows]

    def delete_snapshot(self, snapshot_id: str) -> None:
        """删除快照元数据"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            )

    # ---------- grants（V0.4 数据模型，不开放执行） ----------

    def upsert_grant(self, record: GrantRecord) -> None:
        """写入或更新 grant 记录"""
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.request_id,
                    record.canonical_target,
                    record.operation,
                    record.content_hash,
                    record.status,
                    record.created_at,
                    record.expires_at,
                    record.used_at,
                ),
            )

    def get_grant(self, request_id: str) -> GrantRecord | None:
        """按 request_id 查询 grant"""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT request_id, canonical_target, operation, content_hash, "
                "status, created_at, expires_at, used_at FROM grants "
                "WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        return GrantRecord(*row)

    # ---------- 维护 ----------

    def vacuum_orphan_snapshot_rows(self, known_request_ids: set[str]) -> list[str]:
        """删除没有 intent 引用的快照记录，返回被删除的 snapshot_id"""
        orphans: list[str] = []
        for snapshot in self.list_snapshots():
            if snapshot.request_id not in known_request_ids:
                orphans.append(snapshot.snapshot_id)
                self.delete_snapshot(snapshot.snapshot_id)
        return orphans
