"""V0.4 工作目录内写入测试：SideEffectService、写入工具与串行执行器。"""

import json
import os
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from my_agent.agent import Agent
from my_agent.side_effects.service import (
    ReadFingerprint,
    SideEffectService,
    WriteRequest,
)
from my_agent.tools import get_coding_tools, get_tools


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "note.txt").write_text("第一行\n第二行\n", encoding="utf-8")
    return root


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture()
def service(workspace: Path, state_dir: Path) -> SideEffectService:
    return SideEffectService(workspace, state_dir)


def _payload(result: str) -> dict[str, object]:
    return json.loads(result)


def _mark_read(service: SideEffectService, workspace: Path, relative: str) -> None:
    """模拟 read_text_file 的读取账本记录"""
    target = workspace / relative
    file_stat = os.lstat(target)
    service.record_read(
        relative,
        target.resolve(),
        ReadFingerprint(size=file_stat.st_size, mtime_ns=file_stat.st_mtime_ns),
    )


# ---------- create / overwrite / replace ----------


def test_create_new_file_with_intent_and_ledger(
    service: SideEffectService,
    workspace: Path,
) -> None:
    outcome = service.execute_write(
        WriteRequest(operation="create", relative_path="sub/new.txt", content="内容")
    )

    assert outcome.ok
    assert outcome.error_code is None
    assert (workspace / "sub" / "new.txt").read_text(encoding="utf-8") == "内容"
    intent = service.store.get_intent(str(outcome.request_id))
    assert intent is not None
    assert intent.status == "done"
    assert intent.operation == "create"
    assert intent.snapshot_id == outcome.snapshot_id


def test_create_existing_file_is_rejected(
    service: SideEffectService,
    workspace: Path,
) -> None:
    outcome = service.execute_write(
        WriteRequest(operation="create", relative_path="note.txt", content="x")
    )

    assert not outcome.ok
    assert outcome.error_code == "FS_EXISTS"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "第一行\n第二行\n"


def test_overwrite_requires_prior_read(
    service: SideEffectService,
    workspace: Path,
) -> None:
    outcome = service.execute_write(
        WriteRequest(operation="overwrite", relative_path="note.txt", content="替换")
    )

    assert not outcome.ok
    assert outcome.error_code == "FS_NOT_READ"


def test_overwrite_after_read_succeeds_and_snapshots(
    service: SideEffectService,
    workspace: Path,
) -> None:
    _mark_read(service, workspace, "note.txt")

    outcome = service.execute_write(
        WriteRequest(
            operation="overwrite", relative_path="note.txt", content="全新内容"
        )
    )

    assert outcome.ok
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "全新内容"
    snapshot = service.store.get_snapshot(str(outcome.snapshot_id))
    assert snapshot is not None
    assert snapshot.kind == "overwritten"
    # 快照数据保存在应用状态目录，不在工作区内
    snapshot_path = service.snapshots.data_path(str(outcome.snapshot_id))
    assert not snapshot_path.is_relative_to(workspace)


def test_overwrite_with_changed_file_returns_stale_version(
    service: SideEffectService,
    workspace: Path,
) -> None:
    _mark_read(service, workspace, "note.txt")
    (workspace / "note.txt").write_text("被外部修改过", encoding="utf-8")

    outcome = service.execute_write(
        WriteRequest(operation="overwrite", relative_path="note.txt", content="写入")
    )

    assert not outcome.ok
    assert outcome.error_code == "FS_STALE_VERSION"


def test_replace_text_uniqueness_rules(
    service: SideEffectService,
    workspace: Path,
) -> None:
    _mark_read(service, workspace, "note.txt")

    missing = service.execute_write(
        WriteRequest(
            operation="replace_text",
            relative_path="note.txt",
            old_string="不存在",
            new_string="x",
        )
    )
    assert missing.error_code == "TARGET_NOT_FOUND"

    (workspace / "dup.txt").write_text("重复\n重复\n", encoding="utf-8")
    _mark_read(service, workspace, "dup.txt")
    ambiguous = service.execute_write(
        WriteRequest(
            operation="replace_text",
            relative_path="dup.txt",
            old_string="重复",
            new_string="唯一",
        )
    )
    assert ambiguous.error_code == "AMBIGUOUS_MATCH"

    replaced = service.execute_write(
        WriteRequest(
            operation="replace_text",
            relative_path="note.txt",
            old_string="第二行",
            new_string="改过的第二行",
        )
    )
    assert replaced.ok
    assert (workspace / "note.txt").read_text(
        encoding="utf-8"
    ) == "第一行\n改过的第二行\n"

    replaced_all = service.execute_write(
        WriteRequest(
            operation="replace_text",
            relative_path="dup.txt",
            old_string="重复",
            new_string="唯一",
            replace_all=True,
        )
    )
    assert replaced_all.ok
    assert (workspace / "dup.txt").read_text(encoding="utf-8") == "唯一\n唯一\n"


# ---------- 边界与策略 ----------


def test_out_of_root_write_rejected_with_lazy_suggestion(
    service: SideEffectService,
    tmp_path: Path,
) -> None:
    outcome = service.execute_write(
        WriteRequest(operation="create", relative_path="../outside.txt", content="x")
    )

    assert not outcome.ok
    assert outcome.error_code == "PATH_TRAVERSAL_DENIED"
    assert outcome.suggestion is not None
    assert "/command" in outcome.suggestion
    assert not (tmp_path / "outside.txt").exists()


def test_sensitive_and_protected_paths_rejected(
    service: SideEffectService,
    workspace: Path,
) -> None:
    sensitive = service.execute_write(
        WriteRequest(operation="create", relative_path=".env", content="K=V")
    )
    assert sensitive.error_code == "PATH_SENSITIVE_DENIED"

    rules = service.execute_write(
        WriteRequest(operation="overwrite", relative_path="AGENTS.md", content="x")
    )
    assert rules.error_code == "PATH_PROTECTED_DENIED"


def test_oversized_content_rejected(service: SideEffectService) -> None:
    outcome = service.execute_write(
        WriteRequest(
            operation="create",
            relative_path="big.txt",
            content="x" * (service.write_max_bytes + 1),
        )
    )

    assert not outcome.ok
    assert outcome.error_code == "WRITE_TOO_LARGE"


# ---------- 回滚 ----------


def test_rollback_restores_previous_content(
    service: SideEffectService,
    workspace: Path,
) -> None:
    _mark_read(service, workspace, "note.txt")
    outcome = service.execute_write(
        WriteRequest(operation="overwrite", relative_path="note.txt", content="覆盖后")
    )
    assert outcome.ok

    rollback = service.rollback(str(outcome.request_id))

    assert rollback.ok
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "第一行\n第二行\n"
    intent = service.store.get_intent(str(outcome.request_id))
    assert intent is not None
    assert intent.status == "rolled_back"


def test_rollback_of_created_file_removes_it(
    service: SideEffectService,
    workspace: Path,
) -> None:
    outcome = service.execute_write(
        WriteRequest(operation="create", relative_path="fresh.txt", content="新建")
    )
    assert outcome.ok

    rollback = service.rollback(str(outcome.request_id))

    assert rollback.ok
    assert not (workspace / "fresh.txt").exists()


def test_rollback_rejects_unknown_request(service: SideEffectService) -> None:
    rollback = service.rollback("nonexistent")

    assert not rollback.ok
    assert rollback.error_code == "INTENT_NOT_FOUND"


# ---------- 并发与启动恢复 ----------


def test_concurrent_writes_to_same_path_serialize(
    workspace: Path,
    state_dir: Path,
) -> None:
    service = SideEffectService(workspace, state_dir)
    barrier = threading.Barrier(2)
    results: dict[int, object] = {}

    def worker(index: int) -> None:
        barrier.wait()
        results[index] = service.execute_write(
            WriteRequest(
                operation="create",
                relative_path="shared.txt",
                content=f"from {index}",
            )
        )

    threads = [threading.Thread(target=worker, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    outcomes = list(results.values())
    codes = {outcome.error_code for outcome in outcomes}  # type: ignore[union-attr]
    # 排他创建 + 路径锁：两个并发写请求不会同时进入 executing，
    # 后到的请求在锁内发现文件已存在并被拒绝
    assert codes == {None, "FS_EXISTS"}
    content = (workspace / "shared.txt").read_text(encoding="utf-8")
    assert content.startswith("from ")


def test_startup_recovery_discards_and_marks_unknown(
    workspace: Path,
    state_dir: Path,
) -> None:
    from my_agent.side_effects.intent_store import IntentRecord, utc_now_iso

    service_a = SideEffectService(workspace, state_dir)
    _mark_read(service_a, workspace, "note.txt")
    done = service_a.execute_write(
        WriteRequest(operation="overwrite", relative_path="note.txt", content="完成")
    )
    assert done.ok

    now = utc_now_iso()
    # 人为构造：prepared intent + 快照数据（尚未进入 executing，可安全丢弃）
    service_a.store.create_intent(
        IntentRecord(
            request_id="pending-1",
            operation="overwrite",
            relative_path="note.txt",
            canonical_path=str(workspace / "note.txt"),
            content_hash="0" * 64,
            snapshot_id="snap-pending-1",
            status="prepared",
            created_at=now,
            updated_at=now,
        )
    )
    service_a.snapshots.write_bytes("snap-pending-1", b"old")
    service_a.store.register_snapshot(
        "snap-pending-1",
        "pending-1",
        kind="overwritten",
        relative_path="note.txt",
        size=3,
    )
    # 人为构造：executing intent（结果未知，必须保留并上报）
    service_a.store.create_intent(
        IntentRecord(
            request_id="exec-1",
            operation="overwrite",
            relative_path="note.txt",
            canonical_path=str(workspace / "note.txt"),
            content_hash="0" * 64,
            snapshot_id="snap-exec-1",
            status="executing",
            created_at=now,
            updated_at=now,
        )
    )
    service_a.snapshots.write_bytes("snap-exec-1", b"old")
    service_a.store.register_snapshot(
        "snap-exec-1",
        "exec-1",
        kind="overwritten",
        relative_path="note.txt",
        size=3,
    )
    # 孤儿快照文件（无 intent 引用）
    service_a.snapshots.write_bytes("orphan-1", b"junk")

    service_b = SideEffectService(workspace, state_dir)
    report = service_b.recovery_report

    assert "pending-1" in report.discarded_intents
    assert service_b.store.get_intent("pending-1") is None
    assert "exec-1" in report.unknown_intents
    assert service_b.store.get_intent("exec-1").status == "unknown"  # type: ignore[union-attr]
    assert "orphan-1" in report.cleaned_snapshots
    # executing 的快照必须保留，等待人工检查，绝不自动重试
    assert service_b.snapshots.data_path("snap-exec-1").exists()
    assert not service_b.snapshots.data_path("snap-pending-1").exists()
    assert service_b.recovery_report.has_unknown_results


# ---------- 写入工具与 Agent 串行执行器 ----------


class FakeModel:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.bound_tools: tuple[object, ...] = ()

    def bind_tools(self, tools: Sequence[object]) -> "FakeModel":
        self.bound_tools = tuple(tools)
        return self

    def invoke(
        self,
        messages: list[BaseMessage],
        *,
        config: object | None = None,
    ) -> object:
        del messages, config
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _registry(workspace: Path, tmp_path: Path):
    return get_coding_tools(workspace, state_dir=tmp_path / "state")


def test_coding_profile_registry_contains_write_tools_with_metadata(
    workspace: Path,
    tmp_path: Path,
) -> None:
    registry = _registry(workspace, tmp_path)
    names = registry.get_tool_names()

    assert "write_text_file" in names
    assert "replace_text" in names
    write_tool = registry.get_tool("write_text_file")
    assert write_tool.metadata["effect"] == "workspace_write"
    assert write_tool.metadata["risk"] == "workspace_write"
    assert write_tool.metadata["approval"] == "direct_within_workspace"


def test_default_profile_has_no_write_tools(tmp_path: Path) -> None:
    registry = get_tools(tmp_path / "workspace")

    for name in registry.get_tool_names():
        assert registry.get_tool(name).metadata.get("effect") == "read_only"


def test_agent_serializes_read_then_write_in_one_batch(
    workspace: Path,
    tmp_path: Path,
) -> None:
    """同一批次读后写必须按模型返回顺序串行执行"""
    registry = _registry(workspace, tmp_path)
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_text_file",
                        "args": {"relative_path": "note.txt"},
                        "id": "call-read",
                        "type": "tool_call",
                    },
                    {
                        "name": "write_text_file",
                        "args": {"relative_path": "note.txt", "content": "串行覆盖"},
                        "id": "call-write",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="已写入。"),
        ]
    )
    agent = Agent(model, registry, max_tool_rounds=4)

    result = agent.invoke([])

    assert result.ok
    assert result.reply == "已写入。"
    assert (workspace / "note.txt").read_text(encoding="utf-8") == "串行覆盖"
    write_result = json.loads(str(result.messages[2].content))
    assert write_result["ok"] is True


def test_write_tool_reports_out_of_root_with_suggestion(
    workspace: Path,
    tmp_path: Path,
) -> None:
    registry = _registry(workspace, tmp_path)
    write_tool = registry.get_tool("write_text_file")

    payload = _payload(
        str(write_tool.invoke({"relative_path": "../evil.txt", "content": "x"}))
    )

    assert payload["ok"] is False
    assert payload["error"] == "PATH_TRAVERSAL_DENIED"
    assert payload["suggestion"] is not None
    assert not (workspace.parent / "evil.txt").exists()


def test_replace_text_tool_end_to_end(
    workspace: Path,
    tmp_path: Path,
) -> None:
    registry = _registry(workspace, tmp_path)
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "read_text_file",
                        "args": {"relative_path": "note.txt"},
                        "id": "call-r",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "replace_text",
                        "args": {
                            "relative_path": "note.txt",
                            "old_string": "第一行",
                            "new_string": "唯一第一行",
                        },
                        "id": "call-w",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="替换完成。"),
        ]
    )
    agent = Agent(model, registry, max_tool_rounds=4)

    result = agent.invoke([])

    assert result.ok
    assert (workspace / "note.txt").read_text(
        encoding="utf-8"
    ) == "唯一第一行\n第二行\n"
