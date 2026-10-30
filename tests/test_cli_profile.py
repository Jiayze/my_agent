"""V0.5 CLI coding profile 装配与端到端测试。

验证：--profile 解析、shell 只在显式能力装配时注册、coding 系统提示词、
Agent 端到端命令执行（假模型 + 假审批 + 真实 cmd.exe）。
"""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, BaseMessage

from my_agent.approval import ApprovalService
from my_agent.main import _coding_tools_factory, _parse_cli_profile, build_conversation
from my_agent.models import AgentResult
from my_agent.prompts import CODING_SYSTEM_PROMPT, SYSTEM_PROMPT


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


class AutoApproveDecider:
    decided_by = "fake:always-yes"

    def decide(self, request: object) -> str:
        del request
        return "allowed_once"


class DenyAllDecider:
    decided_by = "fake:always-no"

    def decide(self, request: object) -> str:
        del request
        return "rejected"


def _config(tmp_path: Path):
    from my_agent.config import AgentConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return AgentConfig(
        api_key="test-key",
        model_id="test-model",
        base_url="https://api.example.invalid",
        request_timeout_seconds=1,
        max_tool_rounds=4,
        temperature=0,
        workspace_root=workspace,
        log_level="INFO",
    ), workspace


# ---------- --profile 解析 ----------


def test_profile_defaults_to_readonly() -> None:
    assert _parse_cli_profile([]) == "default"
    assert _parse_cli_profile(["--profile", "default"]) == "default"


def test_profile_coding_is_explicit() -> None:
    assert _parse_cli_profile(["--profile", "coding"]) == "coding"


def test_unknown_profile_value_is_rejected() -> None:

    with pytest.raises(SystemExit):
        _parse_cli_profile(["--profile", "yolo"])


# ---------- coding 装配 ----------


def test_coding_tools_without_shell_have_no_run_command(tmp_path: Path) -> None:
    from my_agent.tools import get_coding_tools

    registry = get_coding_tools(tmp_path, state_dir=tmp_path / "state")

    assert "run_command" not in registry
    assert "write_text_file" in registry


def test_shell_requires_approval_service(tmp_path: Path) -> None:
    from my_agent.tools import get_coding_tools

    with pytest.raises(ValueError, match="ApprovalService"):
        get_coding_tools(tmp_path, state_dir=tmp_path / "state", enable_shell=True)


def test_shell_registered_with_explicit_capability(tmp_path: Path) -> None:
    from my_agent.tools import get_coding_tools

    registry = get_coding_tools(
        tmp_path,
        state_dir=tmp_path / "state",
        approval_service=ApprovalService([AutoApproveDecider()], mode="ask"),
        enable_shell=True,
    )

    assert "run_command" in registry
    tool = registry.get_tool("run_command")
    assert tool.metadata["effect"] == "host_command"
    assert tool.metadata["retryable"] is False


def test_default_profile_never_contains_write_or_shell(tmp_path: Path) -> None:
    from my_agent.tools import get_tools

    registry = get_tools(tmp_path / "workspace")

    for name in registry.get_tool_names():
        assert registry.get_tool(name).metadata.get("effect") == "read_only"


def test_coding_factory_wires_shell_and_coding_prompt(tmp_path: Path) -> None:
    config, _workspace = _config(tmp_path)
    captured: dict[str, object] = {}

    def agent_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    def conversation_factory(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    def summarizer_factory(_config: object) -> object:
        return object()

    build_conversation(
        config,
        profile="coding",
        state_dir=tmp_path / "state",
        model_factory=lambda _config: object(),  # type: ignore[arg-type,return-value]
        summarizer_factory=summarizer_factory,  # type: ignore[arg-type]
        agent_factory=agent_factory,  # type: ignore[arg-type]
        conversation_factory=conversation_factory,  # type: ignore[arg-type]
    )

    registry = captured["tools"]
    assert isinstance(registry, object)
    names = registry.get_tool_names()  # type: ignore[attr-defined]
    assert "run_command" in names
    assert "write_text_file" in names
    assert captured["system_prompt"] == CODING_SYSTEM_PROMPT
    assert CODING_SYSTEM_PROMPT != SYSTEM_PROMPT


def test_default_profile_uses_base_system_prompt(tmp_path: Path) -> None:
    config, _workspace = _config(tmp_path)
    captured: dict[str, object] = {}

    build_conversation(
        config,
        model_factory=lambda _config: object(),  # type: ignore[arg-type,return-value]
        summarizer_factory=lambda _config: object(),  # type: ignore[arg-type,return-value]
        agent_factory=lambda **kwargs: captured.update(agent=kwargs) or object(),  # type: ignore[arg-type,return-value]
        conversation_factory=lambda **kwargs: (
            captured.update(  # type: ignore[arg-type,return-value]
                conversation=kwargs
            )
            or object()
        ),
    )

    assert captured["conversation"]["system_prompt"] == SYSTEM_PROMPT  # type: ignore[index]


# ---------- 端到端（假模型 + 假审批 + 真实 cmd.exe） ----------


def _coding_registry(tmp_path: Path, decider: object):
    from my_agent.tools import get_coding_tools

    return get_coding_tools(
        tmp_path,
        state_dir=tmp_path / "state",
        approval_service=ApprovalService([decider], mode="ask"),
        enable_shell=True,
    )


def test_agent_runs_approved_command_end_to_end(tmp_path: Path) -> None:
    registry = _coding_registry(tmp_path, AutoApproveDecider())
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_command",
                        "args": {"command": "echo e2e-ok"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="命令输出包含 e2e-ok。"),
        ]
    )

    from my_agent.agent import Agent

    agent = Agent(model, registry, max_tool_rounds=3)

    result = agent.invoke([])

    assert result.ok
    tool_message = result.messages[1]
    payload = json.loads(str(tool_message.content))
    assert payload["status"] == "succeeded"
    assert "e2e-ok" in payload["stdout"]
    assert result.reply == "命令输出包含 e2e-ok。"


def test_agent_reports_rejected_command_to_model(tmp_path: Path) -> None:
    registry = _coding_registry(tmp_path, DenyAllDecider())
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_command",
                        "args": {"command": "echo denied-check"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="用户拒绝了该命令。"),
        ]
    )

    from my_agent.agent import Agent

    agent = Agent(model, registry, max_tool_rounds=3)

    result = agent.invoke([])

    assert result.ok
    payload = json.loads(str(result.messages[1].content))
    assert payload["status"] == "denied"
    # 消息协议保持：AI tool-call 与 ToolMessage 一一配对
    assert result.messages[1].tool_call_id == "call-1"


def test_forbidden_command_rejected_without_prompt(tmp_path: Path) -> None:
    class RecordingDecider(AutoApproveDecider):
        decided_by = "fake:recording"

        def __init__(self) -> None:
            self.requests: list[object] = []

        def decide(self, request: object) -> str:
            self.requests.append(request)
            return "allowed_once"

    decider = RecordingDecider()
    registry = _coding_registry(tmp_path, decider)
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "run_command",
                        "args": {"command": "git push origin master"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="该命令被策略拒绝。"),
        ]
    )

    from my_agent.agent import Agent

    agent = Agent(model, registry, max_tool_rounds=3)

    result = agent.invoke([])

    payload = json.loads(str(result.messages[1].content))
    assert payload["status"] == "denied"
    assert decider.requests == []


def test_write_then_command_batch_runs_serially(tmp_path: Path) -> None:
    """含写工具与命令的批次必须按模型返回顺序串行执行"""
    registry = _coding_registry(tmp_path, AutoApproveDecider())
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "write_text_file",
                        "args": {"relative_path": "built.txt", "content": "v1"},
                        "id": "call-w",
                        "type": "tool_call",
                    },
                    {
                        "name": "run_command",
                        "args": {"command": "echo after-write"},
                        "id": "call-c",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="批次完成。"),
        ]
    )

    from my_agent.agent import Agent

    agent = Agent(model, registry, max_tool_rounds=3)

    result = agent.invoke([])

    assert result.ok
    write_payload = json.loads(str(result.messages[1].content))
    command_payload = json.loads(str(result.messages[2].content))
    assert write_payload["ok"] is True
    assert command_payload["status"] == "succeeded"
    assert (tmp_path / "built.txt").read_text(encoding="utf-8") == "v1"


def test_coding_factory_direct(tmp_path: Path) -> None:
    """_coding_tools_factory 装配完整 coding 工具集（含 shell）"""
    config, _workspace = _config(tmp_path)

    inputs: list[str] = []
    registry = _coding_tools_factory(
        config,
        tmp_path / "state",
        input_fn=lambda _prompt: inputs.append(_prompt) or "n",
        output_fn=lambda _line: None,
    )

    assert "run_command" in registry
    assert "write_text_file" in registry
    assert "read_text_file" in registry


def test_agent_result_type_round_trip() -> None:
    result = AgentResult(reply="ok", messages=(), tool_rounds=0)

    assert result.ok
