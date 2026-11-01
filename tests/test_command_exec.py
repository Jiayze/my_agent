"""V0.5 CommandExecutionService 测试：真实 cmd.exe、离线、无网络。

覆盖结果状态协议（succeeded/failed/timed_out/denied/unavailable）、
intent 生命周期、审计记录、超时进程树终止、cmd 原生引号语义与
整棵进程树生命周期。
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from my_agent.approval import ApprovalRequest, ApprovalService, HumanCliDecider
from my_agent.command_exec import CommandExecutionService
from my_agent.tools.shell_tool import build_shell_tool


class FakeDecider:
    """受控决策者：记录请求并返回固定裁决"""

    def __init__(self, verdict: str = "allowed_once") -> None:
        self.verdict = verdict
        self.decided_by = "fake"
        self.requests: list[ApprovalRequest] = []

    def decide(self, request: ApprovalRequest) -> str:
        self.requests.append(request)
        return self.verdict


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture()
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _service(
    workspace: Path, state_dir: Path, decider: FakeDecider
) -> CommandExecutionService:
    return CommandExecutionService(
        workspace,
        state_dir,
        approval=ApprovalService([decider], mode="ask"),
        default_timeout_seconds=120,
    )


def test_succeeded_command_round_trip(workspace: Path, state_dir: Path) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run("echo hello-coding")

    assert outcome.status == "succeeded"
    assert outcome.exit_code == 0
    assert "hello-coding" in outcome.stdout
    assert outcome.duration_ms is not None
    # 审批请求包含命令全文与工作目录
    # （由 FakeDecider 记录，见对应断言测试）


def test_failed_command_reports_nonzero_exit(workspace: Path, state_dir: Path) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run("cmd /c exit 7")

    assert outcome.status == "failed"
    assert outcome.exit_code == 7


def test_approval_rejected_maps_to_denied_not_unavailable(
    workspace: Path,
    state_dir: Path,
) -> None:
    decider = FakeDecider(verdict="rejected")
    service = _service(workspace, state_dir, decider)

    outcome = service.run("echo never-ran")

    assert outcome.status == "denied"
    assert outcome.exit_code is None
    assert "拒绝" in (outcome.decision_reason or "")
    intent = service.store.get_intent(outcome.request_id)
    assert intent is not None
    assert intent.status == "rejected"
    audits = service.store.list_shell_audits()
    assert audits[-1]["decision"] == "rejected"
    assert audits[-1]["decided_by"] == "fake"


def test_approval_unavailable_maps_to_unavailable(
    workspace: Path,
    state_dir: Path,
) -> None:
    service = CommandExecutionService(
        workspace,
        state_dir,
        approval=ApprovalService([], mode="ask"),
        default_timeout_seconds=120,
    )

    outcome = service.run("echo never-ran")

    # 没有人能批准 ≠ 用户拒绝：两者都 fail closed 但状态不同
    assert outcome.status == "unavailable"
    assert service.store.get_intent(outcome.request_id).status == "rejected"  # type: ignore[union-attr]
    assert service.store.list_shell_audits()[-1]["decision"] == "unavailable"


def test_never_mode_rejects_without_prompt(workspace: Path, state_dir: Path) -> None:
    service = CommandExecutionService(
        workspace,
        state_dir,
        approval=ApprovalService([FakeDecider()], mode="never"),
        default_timeout_seconds=120,
    )

    outcome = service.run("echo never-ran")

    assert outcome.status == "denied"


def test_forbidden_command_never_reaches_approval(
    workspace: Path,
    state_dir: Path,
) -> None:
    decider = FakeDecider()
    service = _service(workspace, state_dir, decider)

    outcome = service.run("del note.txt")

    assert outcome.status == "denied"
    assert decider.requests == []  # forbidden 位于审批之上
    audits = service.store.list_shell_audits()
    assert audits[-1]["decision"] == "forbidden"
    assert audits[-1]["decided_by"] == "policy:forbidden"


def test_approval_request_carries_command_and_cwd(
    workspace: Path,
    state_dir: Path,
) -> None:
    decider = FakeDecider()
    service = _service(workspace, state_dir, decider)

    service.run("echo context-check", timeout_seconds=60)

    assert len(decider.requests) == 1
    request = decider.requests[0]
    assert request.command == "echo context-check"
    assert request.cwd == str(workspace)
    assert request.timeout_seconds == 60
    assert request.kind == "host_command"


def test_timeout_kills_process_tree_and_reports(
    workspace: Path,
    state_dir: Path,
) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run("ping -n 30 127.0.0.1", timeout_seconds=5)

    assert outcome.status == "timed_out"
    audits = service.store.list_shell_audits()
    assert audits[-1]["status"] == "timed_out"
    assert audits[-1]["timed_out"] == 1
    intent = service.store.get_intent(outcome.request_id)
    assert intent is not None
    assert intent.status == "done"


def test_command_runs_in_workspace_directory(
    workspace: Path,
    state_dir: Path,
) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run("cd")

    assert outcome.status == "succeeded"
    assert str(workspace).lower() in outcome.stdout.lower()


def test_invalid_arguments_denied_without_intent(
    workspace: Path,
    state_dir: Path,
) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    empty = service.run("   ")
    oversized = service.run("x" * 9000)

    assert empty.status == "denied"
    assert empty.error_code == "INVALID_ARGUMENT"
    assert oversized.error_code == "INVALID_ARGUMENT"
    assert service.store.list_intents_by_status("prepared") == []


def test_human_cli_approval_flow_end_to_end(
    workspace: Path,
    state_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs: list[str] = []
    answers = iter(["y"])
    decider = HumanCliDecider(lambda _prompt: next(answers), outputs.append)
    service = CommandExecutionService(
        workspace,
        state_dir,
        approval=ApprovalService([decider], mode="ask"),
        default_timeout_seconds=120,
    )

    outcome = service.run("echo approved-run")

    assert outcome.status == "succeeded"
    assert "approved-run" in outcome.stdout
    assert any("echo approved-run" in line for line in outputs)


def test_shell_tool_json_contract(workspace: Path, state_dir: Path) -> None:
    service = _service(workspace, state_dir, FakeDecider())
    (tool,) = build_shell_tool(service)

    assert tool.metadata["effect"] == "host_command"
    assert tool.metadata["retryable"] is False
    assert tool.metadata["approval"] == "required_per_command"

    payload = json.loads(str(tool.invoke({"command": "echo tool-round-trip"})))

    assert payload["status"] == "succeeded"
    assert "tool-round-trip" in payload["stdout"]


# ---------- 返工 P1-1：cmd 原生引号语义（2026-11-01） ----------
# 修复前：argv 经 list2cmdline 二次转义，引号变成字面 \" 或被吞掉，
# 出现报错甚至 "succeeded 但语义完全不同" 的假成功。


def test_quoted_executable_path_runs(workspace: Path, state_dir: Path) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run(f'"{sys.executable}" --version')

    assert outcome.status == "succeeded", outcome.stderr
    assert outcome.exit_code == 0
    assert "Python" in outcome.stdout


def test_python_dash_c_prints_output(workspace: Path, state_dir: Path) -> None:
    """修复前：succeeded、退出 0 但无输出（假成功）"""
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run(f'{sys.executable} -c "print(12345)"')

    assert outcome.status == "succeeded", outcome.stderr
    assert "12345" in outcome.stdout


def test_python_dash_c_exit_code_is_preserved(
    workspace: Path,
    state_dir: Path,
) -> None:
    """修复前：内部引号被改写，退出码 7 变成语法错误退出 1"""
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run(f'{sys.executable} -c "import sys; sys.exit(7)"')

    assert outcome.status == "failed"
    assert outcome.exit_code == 7


def test_quoted_path_with_spaces(workspace: Path, state_dir: Path) -> None:
    space_dir = state_dir.parent / "py dir"
    space_dir.mkdir(exist_ok=True)
    script = space_dir / "probe script.cmd"
    script.write_text("@echo space-path-ok", encoding="utf-8")
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run(f'"{script}"')

    assert outcome.status == "succeeded", outcome.stderr
    assert "space-path-ok" in outcome.stdout


def test_multi_arguments_with_spaces(workspace: Path, state_dir: Path) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    outcome = service.run(
        f'{sys.executable} -c "import sys; print(sys.argv[1])" "hello world"'
    )

    assert outcome.status == "succeeded", outcome.stderr
    assert "hello world" in outcome.stdout


def test_redirection_and_compound_commands(workspace: Path, state_dir: Path) -> None:
    service = _service(workspace, state_dir, FakeDecider())

    redirect = service.run("echo redirect-ok> out.txt")
    compound = service.run("echo part-a & echo part-b")

    assert redirect.status == "succeeded"
    assert (workspace / "out.txt").read_text(encoding="utf-8").strip() == "redirect-ok"
    assert compound.status == "succeeded"
    assert "part-a" in compound.stdout and "part-b" in compound.stdout


# ---------- 返工 P1-2：整棵进程树的生命周期（2026-11-01） ----------
# 修复前：根进程退出即返回 succeeded，分离的后代进程仍能继续写文件。


def test_returns_only_after_whole_tree_exits(
    workspace: Path,
    state_dir: Path,
) -> None:
    """分离孙进程（stdio 全部脱离管道）写入标记：返回时必须已完成"""
    service = _service(workspace, state_dir, FakeDecider())
    marker = workspace / "grandchild_marker.txt"
    assert not marker.exists()

    outcome = service.run(
        'start "" /b cmd /c "ping -n 3 127.0.0.1 >nul 2>&1 '
        '& echo done>grandchild_marker.txt"'
    )

    assert outcome.status == "succeeded", outcome.stderr
    # 服务返回即承诺整棵树已到终态：标记必须已写入
    assert marker.exists()


def test_timeout_kills_detached_grandchild(
    workspace: Path,
    state_dir: Path,
) -> None:
    service = _service(workspace, state_dir, FakeDecider())
    marker = workspace / "late_marker.txt"

    outcome = service.run(
        'start "" /b cmd /c "ping -n 60 127.0.0.1 >nul 2>&1 '
        '& echo done>late_marker.txt"',
        timeout_seconds=5,
    )

    # status == timed_out 本身已内含"Job 活动进程数归零"的确认
    # （无法确认时服务会返回 unknown 而不是 timed_out）
    assert outcome.status == "timed_out"
    time.sleep(2)
    # 整树已被终止：延迟写入永远不会发生
    assert not marker.exists()


# ---------- 返工第二轮(2026-11-01)：故障注入与协议缺口 ----------
# 对应独立复核 R1/R2/R3,使用与审查探针相同的 patch 方式。


_LAUNCHER_TEMPLATE = [
    "from pathlib import Path",
    "import subprocess, sys",
    "Path({parent_started!r}).write_text('started')",
    "subprocess.Popen([sys.executable, {child!r}],",
    "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,",
    "    stderr=subprocess.DEVNULL)",
]

_CHILD_TEMPLATE = [
    "import time",
    "from pathlib import Path",
    "time.sleep({delay})",
    "Path({marker!r}).write_text('finished')",
]


def _write_launcher_scripts(
    folder: Path, *, child_delay: float
) -> tuple[Path, Path, Path, Path]:
    """launcher:先写 parent_started 标记再派生 stdio 全脱离的子进程"""
    folder.mkdir(exist_ok=True)
    parent_started = folder / "parent_started.txt"
    marker = folder / "child_finished.txt"
    child = folder / "child.py"
    launcher = folder / "launcher.py"
    child.write_text(
        "\n".join(
            line.format(
                delay=child_delay,
                marker=str(marker),
            )
            for line in _CHILD_TEMPLATE
        ),
        encoding="utf-8",
    )
    launcher.write_text(
        "\n".join(
            line.format(
                parent_started=str(parent_started),
                child=str(child),
            )
            for line in _LAUNCHER_TEMPLATE
        ),
        encoding="utf-8",
    )
    return launcher, parent_started, marker, child


def test_delayed_job_setup_still_controls_tree(
    workspace: Path,
    state_dir: Path,
) -> None:
    """R1：Job 创建被调度延迟时,挂起创建保证根进程未先行运行"""
    import my_agent.bounded_process as bp

    folder = state_dir.parent / "delayed_probe"
    launcher, parent_started, marker, _child = _write_launcher_scripts(
        folder, child_delay=1.2
    )
    service = _service(workspace, state_dir, FakeDecider())

    original_create = bp._create_kill_on_close_job
    observed: dict[str, bool] = {}

    def delayed_create() -> object:
        time.sleep(0.4)
        observed["parent_ran_before_job"] = parent_started.exists()
        return original_create()

    with patch.object(bp, "_create_kill_on_close_job", side_effect=delayed_create):
        outcome = service.run(f'"{sys.executable}" "{launcher}"')

    assert outcome.status == "succeeded", outcome.stderr
    # 挂起创建生效：Job 创建时根进程尚未执行任何代码
    assert observed["parent_ran_before_job"] is False
    assert marker.exists()


def test_job_creation_failure_fails_closed(
    workspace: Path,
    state_dir: Path,
) -> None:
    """R2：Job 创建失败必须在恢复执行前终止根进程并返回明确失败"""
    import my_agent.bounded_process as bp

    folder = state_dir.parent / "create_fail_probe"
    launcher, _parent_started, marker, _child = _write_launcher_scripts(
        folder, child_delay=1.0
    )
    service = _service(workspace, state_dir, FakeDecider())

    with patch.object(bp, "_create_kill_on_close_job", return_value=None):
        outcome = service.run(f'"{sys.executable}" "{launcher}"')

    assert outcome.status == "failed"
    assert outcome.error_code == "TREE_CONTROL_UNAVAILABLE"
    assert service.store.get_intent(outcome.request_id).status == "failed"  # type: ignore[union-attr]
    time.sleep(1.8)
    # 子进程从未运行：不存在"成功返回后继续写入"
    assert not marker.exists()


def test_job_assignment_failure_fails_closed(
    workspace: Path,
    state_dir: Path,
) -> None:
    import my_agent.bounded_process as bp

    folder = state_dir.parent / "assign_fail_probe"
    launcher, _parent_started, marker, _child = _write_launcher_scripts(
        folder, child_delay=1.0
    )
    service = _service(workspace, state_dir, FakeDecider())

    with patch.object(bp, "_assign_process_to_job", return_value=False):
        outcome = service.run(f'"{sys.executable}" "{launcher}"')

    assert outcome.status == "failed"
    assert outcome.error_code == "TREE_CONTROL_UNAVAILABLE"
    time.sleep(1.8)
    assert not marker.exists()


def test_resume_failure_fails_closed(
    workspace: Path,
    state_dir: Path,
) -> None:
    """恢复失败同样 fail closed,不留挂起根进程也不执行命令"""
    import my_agent.bounded_process as bp

    folder = state_dir.parent / "resume_fail_probe"
    launcher, _parent_started, marker, _child = _write_launcher_scripts(
        folder, child_delay=1.0
    )
    service = _service(workspace, state_dir, FakeDecider())

    with patch.object(bp, "_resume_process_threads", return_value=False):
        outcome = service.run(f'"{sys.executable}" "{launcher}"')

    assert outcome.status == "failed"
    assert outcome.error_code == "TREE_CONTROL_UNAVAILABLE"
    time.sleep(1.8)
    assert not marker.exists()


def test_unconfirmed_tree_exit_maps_to_unknown(
    workspace: Path,
    state_dir: Path,
) -> None:
    """R3：终止后无法确认整树终态时必须返回 unknown,而不是普通结果"""
    import my_agent.bounded_process as bp

    service = _service(workspace, state_dir, FakeDecider())

    with patch.object(bp, "_tree_drained", return_value=False):
        outcome = service.run("echo never-confirmed", timeout_seconds=5)

    assert outcome.status == "unknown"
    assert "未确认" in outcome.message
    intent = service.store.get_intent(outcome.request_id)
    assert intent is not None
    assert intent.status == "unknown"
    audits = service.store.list_shell_audits()
    assert audits[-1]["status"] == "unknown"


def test_await_tree_exit_returns_explicit_confirmation() -> None:
    """R3：_await_tree_exit 必须返回可区分的确认状态"""
    import my_agent.bounded_process as bp

    class FakeProcess:
        def poll(self) -> int:
            return 0

    with patch.object(bp, "_tree_drained", return_value=True):
        assert bp._await_tree_exit(FakeProcess(), None) is True

    with patch.object(bp, "_tree_drained", return_value=False):
        assert bp._await_tree_exit(FakeProcess(), object()) is False
