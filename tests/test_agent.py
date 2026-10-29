"""M4 Agent 核心离线测试。"""

import asyncio
import logging
from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.errors import NodeCancelledError

from my_agent.agent import Agent
from my_agent.models import (
    AgentResult,
    AgentRunContext,
    ModelProtocolError,
    ToolExecutionError,
)
from my_agent.tools import add_numbers


class FakeModel:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[BaseMessage, ...]] = []
        self.configs: list[object | None] = []
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
        self.calls.append(tuple(messages))
        self.configs.append(config)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_agent_returns_direct_model_answer() -> None:
    model = FakeModel([AIMessage(content="你好")])
    agent = Agent(model, [add_numbers], max_tool_rounds=2)

    result = agent.invoke([])

    assert isinstance(result, AgentResult)
    assert result.ok
    assert result.reply == "你好"
    assert result.error_code is None
    assert result.tool_rounds == 0
    assert len(model.calls) == 1


def test_agent_executes_tool_and_preserves_protocol_order() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_numbers",
                        "args": {"left": 2, "right": 3},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="结果是 5。"),
        ]
    )
    agent = Agent(model, [add_numbers], max_tool_rounds=2)

    result = agent.invoke([])

    assert result.ok
    assert result.reply == "结果是 5。"
    assert result.tool_rounds == 1
    assert [type(message).__name__ for message in result.messages] == [
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    tool_message = result.messages[1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-1"
    assert tool_message.status == "success"


def test_model_timeout_returns_structured_failure() -> None:
    model = FakeModel([TimeoutError("upstream timeout")])
    agent = Agent(model, [], max_tool_rounds=1)

    result = agent.invoke([])

    assert result.error_code == "model_error"
    assert result.reply
    assert result.messages == ()


def test_empty_final_response_returns_structured_failure() -> None:
    model = FakeModel([AIMessage(content="")])
    agent = Agent(model, [], max_tool_rounds=1)

    result = agent.invoke([])

    assert result.error_code == "empty_model_response"
    assert result.messages == ()


def test_agent_preserves_string_and_dict_text_blocks() -> None:
    model = FakeModel(
        [AIMessage(content=["第一段", {"type": "text", "text": "第二段"}])]
    )
    agent = Agent(model, [], max_tool_rounds=1)

    result = agent.invoke([])

    assert result.ok
    assert result.reply == "第一段第二段"


def test_non_ai_model_response_raises_protocol_error() -> None:
    model = FakeModel([HumanMessage(content="错误角色")])
    agent = Agent(model, [], max_tool_rounds=1)

    with pytest.raises(ModelProtocolError, match="HumanMessage"):
        agent.invoke([])


def test_business_tool_error_becomes_error_tool_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @tool
    def failing_tool() -> str:
        """测试可预期的工具业务失败。"""
        raise ToolExecutionError("token=tool-secret-value")

    failing_tool.metadata = {"effect": "read_only"}

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "failing_tool",
                        "args": {},
                        "id": "call-failing",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="工具失败，但我仍然可以继续。"),
        ]
    )
    agent = Agent(model, [failing_tool], max_tool_rounds=1)

    with caplog.at_level("ERROR", logger="my_agent.agent"):
        result = agent.invoke([])

    assert result.ok
    tool_message = result.messages[1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.status == "error"
    assert tool_message.tool_call_id == "call-failing"
    assert "failing_tool" in caplog.text
    assert "call-failing" in caplog.text
    assert "ToolExecutionError" in caplog.text
    assert "Traceback" in caplog.text
    assert "tool-secret-value" not in caplog.text


def test_tool_round_limit_closes_each_unexecuted_call() -> None:
    def make_response(call_id: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "add_numbers",
                    "args": {"left": 1, "right": 2},
                    "id": call_id,
                    "type": "tool_call",
                }
            ],
        )

    model = FakeModel([make_response("call-1"), make_response("call-2")])
    agent = Agent(model, [add_numbers], max_tool_rounds=1)

    result = agent.invoke([])

    assert result.error_code == "tool_round_limit"
    assert result.messages[-1].type == "tool"
    closer = result.messages[-1]
    assert isinstance(closer, ToolMessage)
    assert closer.tool_call_id == "call-2"
    assert closer.status == "error"


def test_unknown_tool_call_gets_error_result_and_can_continue() -> None:
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "missing_tool",
                        "args": {},
                        "id": "call-missing",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="工具不存在，但我可以继续。"),
        ]
    )
    agent = Agent(model, [add_numbers], max_tool_rounds=1)

    result = agent.invoke([])

    assert result.ok
    tool_message = result.messages[1]
    assert isinstance(tool_message, ToolMessage)
    assert tool_message.tool_call_id == "call-missing"
    assert tool_message.status == "error"
    assert "not a valid tool" in str(tool_message.content)


def test_parallel_tool_calls_each_get_one_result() -> None:
    @tool
    def failing_tool() -> str:
        """测试并行调用中的业务失败。"""
        raise ToolExecutionError("parallel failure")

    failing_tool.metadata = {"effect": "read_only"}

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_numbers",
                        "args": {"left": 1, "right": 2},
                        "id": "call-success",
                        "type": "tool_call",
                    },
                    {
                        "name": "failing_tool",
                        "args": {},
                        "id": "call-failure",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(content="并行工具结果已处理。"),
        ]
    )
    agent = Agent(model, [add_numbers, failing_tool], max_tool_rounds=1)

    result = agent.invoke([])

    assert result.ok
    tool_messages = [
        message for message in result.messages if isinstance(message, ToolMessage)
    ]
    assert {message.tool_call_id for message in tool_messages} == {
        "call-success",
        "call-failure",
    }
    assert {message.status for message in tool_messages} == {"success", "error"}


def test_programmer_error_in_tool_is_not_masqueraded() -> None:
    @tool
    def broken_tool() -> str:
        """测试未预期的工具编程错误。"""
        raise NameError("broken implementation")

    broken_tool.metadata = {"effect": "read_only"}

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "broken_tool",
                        "args": {},
                        "id": "call-broken",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    agent = Agent(model, [broken_tool], max_tool_rounds=1)

    with pytest.raises(NameError, match="broken implementation"):
        agent.invoke([])


@pytest.mark.parametrize("error_type", [TypeError, RuntimeError, AssertionError])
def test_other_unexpected_tool_errors_are_not_masqueraded(
    error_type: type[Exception],
) -> None:
    @tool
    def broken_tool() -> str:
        """测试其他未预期工具实现错误。"""
        raise error_type("broken implementation")

    broken_tool.metadata = {"effect": "read_only"}

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "broken_tool",
                        "args": {},
                        "id": "call-broken",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    agent = Agent(model, [broken_tool], max_tool_rounds=1)

    with pytest.raises(error_type, match="broken implementation"):
        agent.invoke([])


def test_agent_rejects_tool_without_effect_metadata() -> None:
    @tool
    def undeclared_tool() -> str:
        """测试未声明 effect 元数据的工具。"""
        return "不应执行"

    model = FakeModel([])

    with pytest.raises(ValueError, match="effect"):
        Agent(model, [undeclared_tool], max_tool_rounds=1)

    assert model.bound_tools == ()


def test_agent_rejects_declared_side_effect_tool_without_policy() -> None:
    @tool
    def host_command_tool() -> str:
        """测试声明为主机命令的工具。"""
        return "不应执行"

    host_command_tool.metadata = {"effect": "host_command"}
    model = FakeModel([])

    with pytest.raises(ValueError, match="side-effect execution policy"):
        Agent(model, [host_command_tool], max_tool_rounds=1)

    assert model.bound_tools == ()


def test_run_context_is_consumed_without_being_stored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    model = FakeModel([AIMessage(content="完成")])
    agent = Agent(model, [], max_tool_rounds=1)
    run_context = AgentRunContext(
        run_id="run-context-test",
        conversation_id="conversation-context-test",
    )

    with caplog.at_level(logging.INFO, logger="my_agent.agent"):
        result = agent.invoke([], run_context=run_context)

    assert result.ok
    assert "run-context-test" in caplog.text
    assert "conversation-context-test" in caplog.text
    assert run_context not in agent.__dict__.values()


def test_keyboard_interrupt_propagates_from_agent_core() -> None:
    model = FakeModel([KeyboardInterrupt()])
    agent = Agent(model, [], max_tool_rounds=1)

    with pytest.raises(KeyboardInterrupt):
        agent.invoke([])


def test_node_cancelled_error_propagates_from_agent_core() -> None:
    model = FakeModel([asyncio.CancelledError()])
    agent = Agent(model, [], max_tool_rounds=1)

    with pytest.raises(NodeCancelledError):
        agent.invoke([])
