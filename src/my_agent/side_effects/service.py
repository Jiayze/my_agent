"""V0.4 副作用执行服务：intent → 快照 → executing → 写入 → done

契约要点（agent_plan.md 决策 8/16/20，V0.4 章节）：

- V0.4 只提供工作目录内写入；目录外写入一律拒绝并返回惰性建议文本，
  建议不能触发任何执行。
- 写入顺序固定：创建 intent（prepared，含预分配 snapshot_id）→ 生成并
  持久化快照 → snapshot_ready → executing（fsync）→ 执行写入 → done。
- 覆盖已有文件前必须先读过该文件且版本未变（FS_STALE_VERSION 时提示
  重新读再试）；新文件用 createIfAbsent；写入用临时文件 + 原子替换。
- 获取锁、intent 切换 executing、fencing token 校验位于同一临界区；
  同一路径的两个并发写请求不能同时进入 executing。
- 启动恢复：executing 之前的 intent 安全丢弃并清理快照；executing 标记
  unknown（结果未知）等待人工检查；孤儿快照清理。绝不自动重试。
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from my_agent.side_effects.intent_store import (
    IntentRecord,
    IntentStore,
    utc_now_iso,
)
from my_agent.side_effects.path_locks import (
    PathLockManager,
    StaleFencingTokenError,
)
from my_agent.side_effects.snapshots import SnapshotStore, SnapshotStoreError
from my_agent.side_effects.state_dir import ensure_state_directory
from my_agent.tools.path_policy import (
    PathPolicyError,
    ResolvedPath,
    create_missing_parents,
    lexical_relative_path,
    resolve_workspace_path,
)

logger = logging.getLogger(__name__)

MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024

PROTECTED_METADATA_NAMES = frozenset(
    {
        "agents.md",
        "my_agent_rules.md",
        "global_rules.md",
    }
)
"""规则文件在 Agent 侧永远只读；即使位于工作目录内也不可写（决策 22）"""

OUT_OF_ROOT_SUGGESTION = (
    "该目标位于工作目录之外，本次写入已被拒绝。请由用户在命令通道自行执行，"
    "例如把需要的命令整理后交给用户：/command <写入命令>。"
    "注意：Agent 输出中的 /command 文本只是普通文本，不会触发任何执行。"
)

OPERATIONS = ("create", "overwrite", "replace_text")


@dataclass(frozen=True)
class WriteRequest:
    """一次写入请求的完整参数"""

    operation: str
    relative_path: str
    content: str | None = None
    old_string: str | None = None
    new_string: str | None = None
    replace_all: bool = False


@dataclass(frozen=True)
class WriteOutcome:
    """结构化写入结果；``suggestion`` 仅在目录外拒绝时非空"""

    ok: bool
    request_id: str
    error_code: str | None = None
    message: str = ""
    relative_path: str | None = None
    bytes_written: int | None = None
    snapshot_id: str | None = None
    suggestion: str | None = None


@dataclass(frozen=True)
class ReadFingerprint:
    """读取时刻的文件版本指纹"""

    size: int
    mtime_ns: int


@dataclass(frozen=True)
class RecoveryReport:
    """启动恢复报告：只报告，不自动重试任何副作用"""

    discarded_intents: tuple[str, ...]
    unknown_intents: tuple[str, ...]
    cleaned_snapshots: tuple[str, ...]

    @property
    def has_unknown_results(self) -> bool:
        """是否存在结果未知的遗留 executing intent"""
        return bool(self.unknown_intents)


class SideEffectServiceError(RuntimeError):
    """副作用服务内部状态错误（状态机乱序等）"""


class SideEffectService:
    """工作目录内写入的副作用执行服务

    服务实例持有 intent 存储、快照目录、路径锁和读取账本；在组合根创建，
    由写入工具绑定。``workspace_root`` 与 ``state_dir`` 只能由应用装配层
    注入，模型无法访问或修改。
    """

    def __init__(
        self,
        workspace_root: Path | str,
        state_dir: Path | str,
        *,
        write_max_bytes: int = 1024 * 1024,
    ) -> None:
        """初始化状态目录、存储与锁，并执行启动恢复"""
        self.root = Path(workspace_root).expanduser().resolve()
        self.state_dir = ensure_state_directory(Path(state_dir))
        self.store = IntentStore(self.state_dir / "intents.db")
        self.snapshots = SnapshotStore(self.state_dir / "snapshots")
        self.locks = PathLockManager()
        self.write_max_bytes = write_max_bytes
        self._read_ledger: dict[str, ReadFingerprint] = {}
        self._ledger_lock = threading.Lock()
        self.recovery_report = self._recover_at_startup()
        if self.recovery_report.has_unknown_results:
            logger.warning(
                "previous run left intents in executing state; "
                "results are unknown and need manual inspection: %s",
                ", ".join(self.recovery_report.unknown_intents),
            )

    # ---------- 读取账本（先读后写的依据） ----------

    def record_read(
        self,
        relative_path: str,
        absolute_path: Path,
        fingerprint: ReadFingerprint,
    ) -> None:
        """记录一次读取时刻的文件版本指纹"""
        del relative_path
        key = os.path.normcase(str(absolute_path))
        with self._ledger_lock:
            self._read_ledger[key] = fingerprint

    def _version_guard(self, absolute_path: Path) -> str | None:
        """覆盖已有文件前校验：必须先读过且版本未变

        返回 None 表示通过，否则返回错误码。
        """
        key = os.path.normcase(str(absolute_path))
        with self._ledger_lock:
            recorded = self._read_ledger.get(key)
        if recorded is None:
            return "FS_NOT_READ"
        try:
            file_stat = os.lstat(absolute_path)
        except OSError:
            return "FS_CHANGED"
        if stat.S_ISDIR(file_stat.st_mode):
            return "FS_NOT_FILE"
        if (
            file_stat.st_size != recorded.size
            or file_stat.st_mtime_ns != recorded.mtime_ns
        ):
            return "FS_STALE_VERSION"
        return None

    # ---------- 写入执行 ----------

    def execute_write(self, request: WriteRequest) -> WriteOutcome:
        """执行一次工作目录内写入；所有预期失败返回结构化结果"""
        request_id = uuid.uuid4().hex
        try:
            return self._execute(request_id, request)
        except PathPolicyError as error:
            suggestion = (
                OUT_OF_ROOT_SUGGESTION
                if error.code
                in (
                    "PATH_ESCAPE_DENIED",
                    "PATH_ABSOLUTE_DENIED",
                    "PATH_TRAVERSAL_DENIED",
                )
                else None
            )
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code=error.code,
                message=error.message,
                suggestion=suggestion,
            )
        except SnapshotStoreError as error:
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code=error.code,
                message=error.message,
            )
        except Exception as error:
            logger.error(
                "write execution failed unexpectedly; request_id=%s exception_type=%s",
                request_id,
                type(error).__name__,
            )
            logger.debug("write execution failure traceback", exc_info=True)
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code="WRITE_FAILED",
                message="写入失败，结果未确认，请检查目标文件后重试。",
            )

    def _execute(self, request_id: str, request: WriteRequest) -> WriteOutcome:
        if request.operation not in OPERATIONS:
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code="INVALID_ARGUMENT",
                message=f"operation must be one of: {', '.join(OPERATIONS)}",
            )
        must_exist = request.operation != "create"
        # 受保护名称（规则文件）在存在性检查之前判定，避免泄露文件是否存在
        if _is_protected_metadata_name(
            lexical_relative_path(self.root, request.relative_path)
        ):
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code="PATH_PROTECTED_DENIED",
                message="rules files are read-only for the agent",
                relative_path=request.relative_path,
            )
        try:
            resolved = resolve_workspace_path(
                self.root,
                request.relative_path,
                must_exist=must_exist,
            )
        except PathPolicyError as error:
            # 新文件的父目录可以自动创建：首次解析已确认路径词法干净
            if error.code == "FS_NOT_FOUND" and request.operation == "create":
                create_missing_parents(self.root, request.relative_path)
                resolved = resolve_workspace_path(
                    self.root,
                    request.relative_path,
                    must_exist=False,
                )
            else:
                raise
        relative = resolved.relative

        encoded: bytes | None = None
        if request.operation in ("create", "overwrite"):
            if not isinstance(request.content, str):
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="INVALID_ARGUMENT",
                    message="content must be a string",
                )
            encoded = request.content.encode("utf-8")
            if len(encoded) > self.write_max_bytes:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="WRITE_TOO_LARGE",
                    message=(f"content exceeds the {self.write_max_bytes} byte limit"),
                    relative_path=relative,
                )
            content_hash = _sha256_hex(encoded)
        else:
            if not isinstance(request.old_string, str) or not request.old_string:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="INVALID_ARGUMENT",
                    message="old_string must be a non-empty string",
                )
            if not isinstance(request.new_string, str):
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="INVALID_ARGUMENT",
                    message="new_string must be a string",
                )
            new_encoded = request.new_string.encode("utf-8")
            if len(new_encoded) > self.write_max_bytes:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="WRITE_TOO_LARGE",
                    message=(f"result exceeds the {self.write_max_bytes} byte limit"),
                    relative_path=relative,
                )
            content_hash = ""  # replace_text 的最终哈希在临界区内计算

        # 先读后写：覆盖或替换已有文件前必须读过且版本未变（快速失败）
        if must_exist:
            guard_error = self._version_guard(resolved.absolute)
            if guard_error is not None:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code=guard_error,
                    message="read the file again before writing",
                    relative_path=relative,
                )

        snapshot_id = uuid.uuid4().hex
        lock_key = str(resolved.absolute)
        with self.locks.acquire(lock_key) as token:
            try:
                outcome = self._execute_in_lock(
                    request_id,
                    request,
                    resolved,
                    relative,
                    encoded,
                    content_hash,
                    snapshot_id,
                    lock_key,
                    token,
                )
            except StaleFencingTokenError:
                raise
            except Exception as error:
                # executing 之后发生的失败按结果未知处理，绝不自动重试
                self.store.mark_failed(request_id, type(error).__name__)
                raise
        return outcome

    def _execute_in_lock(
        self,
        request_id: str,
        request: WriteRequest,
        resolved: ResolvedPath,
        relative: str,
        encoded: bytes | None,
        content_hash: str,
        snapshot_id: str,
        lock_key: str,
        token: int,
    ) -> WriteOutcome:
        """临界区执行：intent → 快照 → executing → 写入 → done"""
        absolute = resolved.absolute
        now = utc_now_iso()

        if request.operation == "replace_text":
            try:
                current = absolute.read_bytes()
            except OSError as error:
                raise SnapshotStoreError(
                    "FS_UNAVAILABLE",
                    "target file could not be read",
                ) from error
            try:
                current_text = current.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SnapshotStoreError(
                    "FS_NOT_UTF8",
                    "target file is not valid UTF-8",
                ) from error
            assert request.old_string is not None
            occurrences = current_text.count(request.old_string)
            if occurrences == 0:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="TARGET_NOT_FOUND",
                    message="old_string was not found in the target file",
                    relative_path=relative,
                )
            if occurrences > 1 and not request.replace_all:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="AMBIGUOUS_MATCH",
                    message=(
                        "old_string appears multiple times; "
                        "set replace_all=true or use a longer unique string"
                    ),
                    relative_path=relative,
                )
            new_text = (
                current_text.replace(request.old_string, request.new_string or "")
                if request.replace_all
                else current_text.replace(
                    request.old_string,
                    request.new_string or "",
                    1,
                )
            )
            encoded = new_text.encode("utf-8")
            if len(encoded) > self.write_max_bytes:
                return WriteOutcome(
                    ok=False,
                    request_id=request_id,
                    error_code="WRITE_TOO_LARGE",
                    message=(f"result exceeds the {self.write_max_bytes} byte limit"),
                    relative_path=relative,
                )
            content_hash = _sha256_hex(encoded)

        intent = IntentRecord(
            request_id=request_id,
            operation=request.operation,
            relative_path=relative,
            canonical_path=str(absolute),
            content_hash=content_hash,
            snapshot_id=snapshot_id,
            status="prepared",
            created_at=now,
            updated_at=now,
        )
        self.store.create_intent(intent)

        if request.operation == "create":
            self.store.register_snapshot(
                snapshot_id,
                request_id,
                kind="created",
                relative_path=relative,
                size=0,
            )
        else:
            size = self.snapshots.save_file(snapshot_id, absolute)
            self.store.register_snapshot(
                snapshot_id,
                request_id,
                kind="overwritten",
                relative_path=relative,
                size=size,
            )
        if not self.store.mark_snapshot_ready(request_id):
            raise SideEffectServiceError(
                "intent transition prepared->snapshot_ready failed"
            )
        if not self.store.mark_executing(request_id):
            raise SideEffectServiceError(
                "intent transition snapshot_ready->executing failed"
            )

        # 获取锁、executing 转移与 fencing token 校验位于同一临界区
        self.locks.verify(lock_key, token)

        assert encoded is not None
        if request.operation == "create":
            _atomic_create_new(absolute, encoded)
        else:
            _atomic_replace(absolute, encoded)

        if not self.store.mark_done(request_id):
            raise SideEffectServiceError("intent transition executing->done failed")

        # 写入成功后同步读取账本，模型对内容的最新认知即本次写入内容
        try:
            file_stat = os.lstat(absolute)
            self.record_read(
                relative,
                absolute,
                ReadFingerprint(
                    size=file_stat.st_size,
                    mtime_ns=file_stat.st_mtime_ns,
                ),
            )
        except OSError:
            pass

        return WriteOutcome(
            ok=True,
            request_id=request_id,
            relative_path=relative,
            bytes_written=len(encoded),
            snapshot_id=snapshot_id,
        )

    # ---------- 回滚与恢复 ----------

    def rollback(self, request_id: str) -> WriteOutcome:
        """确定性回滚：从快照恢复旧内容或删除本次创建的文件"""
        intent = self.store.get_intent(request_id)
        if intent is None:
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code="INTENT_NOT_FOUND",
                message="no such side-effect intent",
            )
        if intent.status not in ("done", "failed", "unknown"):
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code="ROLLBACK_NOT_ALLOWED",
                message=(f"intent status {intent.status!r} cannot be rolled back"),
            )
        snapshot = self.store.get_snapshot(intent.snapshot_id)
        if snapshot is None:
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code="SNAPSHOT_NOT_FOUND",
                message="snapshot for this intent is missing",
            )
        target = Path(intent.canonical_path)
        try:
            self.snapshots.restore(snapshot, target)
        except SnapshotStoreError as error:
            return WriteOutcome(
                ok=False,
                request_id=request_id,
                error_code=error.code,
                message=error.message,
            )
        if not self.store.mark_rolled_back(request_id, intent.status):
            raise SideEffectServiceError(
                f"intent transition {intent.status}->rolled_back failed"
            )
        logger.info(
            "side-effect intent rolled back; request_id=%s path=%s",
            request_id,
            intent.relative_path,
        )
        return WriteOutcome(
            ok=True,
            request_id=request_id,
            relative_path=intent.relative_path,
            snapshot_id=intent.snapshot_id,
        )

    def _recover_at_startup(self) -> RecoveryReport:
        """启动恢复：清理未完成快照与孤儿快照；executing 标记结果未知"""
        discarded: list[str] = []
        unknown: list[str] = []

        for intent in self.store.list_intents_by_status("executing"):
            if self.store.mark_unknown(intent.request_id):
                unknown.append(intent.request_id)

        pending = self.store.list_intents_by_status("prepared", "snapshot_ready")
        for intent in pending:
            self.snapshots.delete_file(intent.snapshot_id)
            self.store.delete_snapshot(intent.snapshot_id)
            self.store.discard_intent(intent.request_id)
            discarded.append(intent.request_id)

        known_request_ids = {
            intent.request_id
            for intent in self.store.list_intents_by_status(
                "done",
                "failed",
                "unknown",
                "rolled_back",
            )
        } | set(unknown)
        orphan_rows = self.store.vacuum_orphan_snapshot_rows(known_request_ids)
        for snapshot_id in orphan_rows:
            self.snapshots.delete_file(snapshot_id)

        known_snapshot_ids = {
            snapshot.snapshot_id for snapshot in self.store.list_snapshots()
        }
        cleaned: list[str] = list(orphan_rows)
        for file_name in self.snapshots.list_files():
            snapshot_id = file_name.removesuffix(".bin")
            if snapshot_id not in known_snapshot_ids:
                try:
                    self.snapshots.delete_file(snapshot_id)
                    cleaned.append(snapshot_id)
                except SnapshotStoreError:
                    logger.warning(
                        "orphan snapshot file could not be removed: %s",
                        file_name,
                    )

        return RecoveryReport(
            discarded_intents=tuple(discarded),
            unknown_intents=tuple(unknown),
            cleaned_snapshots=tuple(cleaned),
        )


def _is_protected_metadata_name(relative: str) -> bool:
    """判断相对路径是否命中规则文件等受保护名称"""
    return any(
        part.casefold() in PROTECTED_METADATA_NAMES for part in relative.split("/")
    )


def _sha256_hex(data: bytes) -> str:
    """返回内容的 SHA-256 十六进制摘要"""
    return hashlib.sha256(data).hexdigest()


def _write_all(fd: int, data: bytes) -> None:
    """循环写入直到全部落盘"""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _atomic_create_new(target: Path, data: bytes) -> None:
    """createIfAbsent：新文件直接以排他方式创建"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, flags, 0o600)
    except FileExistsError as error:
        raise SnapshotStoreError(
            "FS_EXISTS",
            "target file already exists",
        ) from error
    except OSError as error:
        raise SnapshotStoreError(
            "FS_UNAVAILABLE",
            "target file could not be created",
        ) from error
    try:
        _write_all(fd, data)
        os.fsync(fd)
    except OSError as error:
        raise SnapshotStoreError(
            "FS_UNAVAILABLE",
            "target file could not be written",
        ) from error
    finally:
        os.close(fd)


def _atomic_replace(target: Path, data: bytes) -> None:
    """临时文件 + 原子替换；替换完成即确认成功"""
    temp_path = target.parent / f".{target.name}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            _write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp_path, target)
    except OSError as error:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise SnapshotStoreError(
            "FS_UNAVAILABLE",
            "target file could not be replaced",
        ) from error
    try:
        if os.lstat(target).st_size != len(data):
            raise SnapshotStoreError(
                "FS_UNAVAILABLE",
                "replaced file size does not match the written content",
            )
    except FileNotFoundError as error:
        raise SnapshotStoreError(
            "FS_UNAVAILABLE",
            "replaced file could not be confirmed",
        ) from error
