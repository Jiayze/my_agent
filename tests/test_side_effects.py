"""V0.4 副作用基础设施测试：状态目录、intent 存储、快照、路径锁、grant。"""

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from my_agent.side_effects.intent_store import (
    GrantRecord,
    IntentRecord,
    IntentStore,
    SnapshotRecord,
    utc_now_iso,
    validate_grant,
)
from my_agent.side_effects.path_locks import (
    PathLockManager,
    StaleFencingTokenError,
)
from my_agent.side_effects.snapshots import SnapshotStore, SnapshotStoreError
from my_agent.side_effects.state_dir import (
    StateDirectoryError,
    ensure_state_directory,
)

# ---------- 状态目录 ----------


def test_ensure_state_directory_creates_directory(tmp_path: Path) -> None:
    state = ensure_state_directory(tmp_path / "state")

    assert state.is_dir()


def test_ensure_state_directory_fails_when_path_is_a_file(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StateDirectoryError):
        ensure_state_directory(occupied)


# ---------- IntentStore 状态机 ----------


def _intent(request_id: str, status: str) -> IntentRecord:
    now = utc_now_iso()
    return IntentRecord(
        request_id=request_id,
        operation="overwrite",
        relative_path="a.txt",
        canonical_path=str(Path("a.txt").resolve()),
        content_hash="0" * 64,
        snapshot_id=f"snap-{request_id}",
        status=status,
        created_at=now,
        updated_at=now,
    )


def test_intent_transitions_follow_the_fixed_order(tmp_path: Path) -> None:
    store = IntentStore(tmp_path / "intents.db")
    store.create_intent(_intent("r1", "prepared"))

    assert store.mark_executing("r1") is False
    assert store.mark_done("r1") is False
    assert store.mark_snapshot_ready("r1") is True
    assert store.mark_executing("r1") is True
    assert store.mark_snapshot_ready("r1") is False
    assert store.mark_done("r1") is True
    assert store.get_intent("r1").status == "done"  # type: ignore[union-attr]


def test_discard_only_removes_pre_executing_intents(tmp_path: Path) -> None:
    store = IntentStore(tmp_path / "intents.db")
    store.create_intent(_intent("r1", "prepared"))
    store.create_intent(_intent("r2", "snapshot_ready"))
    store.create_intent(_intent("r3", "prepared"))
    store.mark_snapshot_ready("r3")
    store.mark_executing("r3")

    store.discard_intent("r1")
    store.discard_intent("r2")
    store.discard_intent("r3")

    assert store.get_intent("r1") is None
    assert store.get_intent("r2") is None
    assert store.get_intent("r3") is not None
    assert store.get_intent("r3").status == "executing"  # type: ignore[union-attr]


def test_rollback_transition_requires_terminal_status(tmp_path: Path) -> None:
    store = IntentStore(tmp_path / "intents.db")
    store.create_intent(_intent("r1", "prepared"))
    store.create_intent(_intent("r2", "prepared"))
    store.mark_snapshot_ready("r2")
    store.mark_executing("r2")
    store.mark_done("r2")

    assert store.mark_rolled_back("r1", "prepared") is False
    assert store.mark_rolled_back("r2", "done") is True
    assert store.get_intent("r2").status == "rolled_back"  # type: ignore[union-attr]


# ---------- grant 数据模型（V0.4 不开放执行） ----------


def _grant(**overrides: object) -> GrantRecord:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = {
        "request_id": "g1",
        "canonical_target": r"C:\outside\target.txt",
        "operation": "write",
        "content_hash": "a" * 64,
        "status": "approved",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "used_at": None,
    }
    defaults.update(overrides)
    return GrantRecord(**defaults)  # type: ignore[arg-type]


def test_valid_grant_passes_validation() -> None:
    grant = _grant()

    assert (
        validate_grant(
            grant,
            canonical_target=str(grant.canonical_target),
            operation="write",
            content_hash="a" * 64,
            now=datetime.now(timezone.utc),
        )
        is None
    )


def test_grant_rejects_mismatched_identity() -> None:
    now = datetime.now(timezone.utc)
    grant = _grant()

    assert (
        validate_grant(
            grant,
            canonical_target=r"C:\outside\other.txt",
            operation="write",
            content_hash="a" * 64,
            now=now,
        )
        == "GRANT_TARGET_MISMATCH"
    )
    assert (
        validate_grant(
            grant,
            canonical_target=str(grant.canonical_target),
            operation="replace",
            content_hash="a" * 64,
            now=now,
        )
        == "GRANT_OPERATION_MISMATCH"
    )
    assert (
        validate_grant(
            grant,
            canonical_target=str(grant.canonical_target),
            operation="write",
            content_hash="b" * 64,
            now=now,
        )
        == "GRANT_CONTENT_MISMATCH"
    )


def test_grant_is_single_use_and_expires() -> None:
    now = datetime.now(timezone.utc)
    used = _grant(used_at=now.isoformat())
    assert (
        validate_grant(
            used,
            canonical_target=str(used.canonical_target),
            operation="write",
            content_hash="a" * 64,
            now=now,
        )
        == "GRANT_ALREADY_USED"
    )

    expired = _grant(expires_at=(now - timedelta(seconds=1)).isoformat())
    assert (
        validate_grant(
            expired,
            canonical_target=str(expired.canonical_target),
            operation="write",
            content_hash="a" * 64,
            now=now,
        )
        == "GRANT_EXPIRED"
    )

    pending = _grant(status="pending")
    assert (
        validate_grant(
            pending,
            canonical_target=str(pending.canonical_target),
            operation="write",
            content_hash="a" * 64,
            now=now,
        )
        == "GRANT_NOT_APPROVED"
    )


def test_grant_target_comparison_is_case_insensitive() -> None:
    grant = _grant(canonical_target=r"C:\Outside\Target.TXT")

    assert (
        validate_grant(
            grant,
            canonical_target=r"c:\outside\target.txt",
            operation="write",
            content_hash="a" * 64,
            now=datetime.now(timezone.utc),
        )
        is None
    )


def test_grant_store_roundtrip(tmp_path: Path) -> None:
    store = IntentStore(tmp_path / "intents.db")
    grant = _grant()
    store.upsert_grant(grant)

    assert store.get_grant("g1") == grant


# ---------- 快照存储 ----------


def test_snapshot_roundtrip_overwritten(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    source = tmp_path / "a.txt"
    source.write_text("旧内容", encoding="utf-8")

    size = store.save_file("s1", source)
    source.write_text("新内容", encoding="utf-8")
    store.restore(
        SnapshotRecord(
            snapshot_id="s1",
            request_id="r1",
            kind="overwritten",
            relative_path="a.txt",
            size=size,
        ),
        source,
    )

    assert source.read_text(encoding="utf-8") == "旧内容"


def test_snapshot_rollback_of_created_file_removes_it(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    created = tmp_path / "new.txt"
    created.write_text("本次新建", encoding="utf-8")

    store.restore(
        SnapshotRecord(
            snapshot_id="s2",
            request_id="r1",
            kind="created",
            relative_path="new.txt",
            size=0,
        ),
        created,
    )

    assert not created.exists()


def test_snapshot_restore_with_missing_data_fails(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "snapshots")
    target = tmp_path / "a.txt"
    target.write_text("当前", encoding="utf-8")

    with pytest.raises(SnapshotStoreError, match="ROLLBACK_FAILED"):
        store.restore(
            SnapshotRecord(
                snapshot_id="missing",
                request_id="r1",
                kind="overwritten",
                relative_path="a.txt",
                size=10,
            ),
            target,
        )


# ---------- 写路径锁与 fencing token ----------


def test_path_lock_verify_within_critical_section() -> None:
    manager = PathLockManager()
    with manager.acquire(r"C:\ws\a.txt") as token:
        manager.verify(r"C:\WS\A.txt", token)  # 锁键大小写规范化
        assert token >= 1
    with pytest.raises(StaleFencingTokenError):
        manager.verify(r"C:\ws\a.txt", token)


def test_revoked_token_cannot_pass_verification() -> None:
    manager = PathLockManager()
    with manager.acquire(r"C:\ws\a.txt") as stale_token:
        manager.revoke(r"C:\ws\a.txt")
        with pytest.raises(StaleFencingTokenError):
            manager.verify(r"C:\ws\a.txt", stale_token)


def test_different_paths_hold_independent_locks() -> None:
    manager = PathLockManager()
    with manager.acquire(r"C:\ws\a.txt") as token_a:
        with manager.acquire(r"C:\ws\b.txt") as token_b:
            manager.verify(r"C:\ws\a.txt", token_a)
            manager.verify(r"C:\ws\b.txt", token_b)
            assert token_a != token_b


def test_revoke_keeps_lock_held_by_old_holder() -> None:
    """吊销只使 token 失效，锁本体仍被旧持有者占用直到释放"""
    manager = PathLockManager()
    holder_entered = threading.Event()
    release = threading.Event()
    waiter_done = threading.Event()

    def holder() -> None:
        with manager.acquire(r"C:\ws\a.txt"):
            holder_entered.set()
            release.wait(timeout=5)

    def waiter() -> None:
        with manager.acquire(r"C:\ws\a.txt"):
            waiter_done.set()

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert holder_entered.wait(timeout=5)
    manager.revoke(r"C:\ws\a.txt")

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    assert not waiter_done.wait(timeout=0.2)

    release.set()
    holder_thread.join(timeout=5)
    waiter_thread.join(timeout=5)
    assert waiter_done.is_set()
