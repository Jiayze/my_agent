"""V0.3 进度账本防卡死测试：全部离线运行。"""

from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool

from my_agent.agent import Agent, build_progress_record
from my_agent.models import AgentResult, ToolExecutionError
from my_agent.tools import add_numbers
from my_agent.tools.registry import ToolRegistry


class FakeModel:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[BaseMessage, ...]] = []

    def bind_tools(self, tools: Sequence[object]) -> "FakeModel":
        del tools
        return self

    def invoke(
        self,
        messages: list[BaseMessage],
        *,
        config: object | None = None,
    ) -> object:
        del config
        self.calls.append(tuple(messages))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _tool_call(name: str, args: dict[str, object], call_id: str) -> dict[str, object]:
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _repeat_response(call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[_tool_call("add_numbers", {"left": 2, "right": 3}, call_id)],
    )


def test_repeated_identical_calls_trigger_agent_stall() -> None:
    model = FakeModel([_repeat_response(f"call-{i}") for i in range(1, 4)])
    agent = Agent(model, [add_numbers], max_tool_rounds=5, max_stall_rounds=3)

    result = agent.invoke([])

    assert isinstance(result, AgentResult)
    assert result.error_code == "agent_stall"
    assert not result.ok
    # 账本随一次 invoke 创建并丢弃，Agent 实例保持无状态
    assert not hasattr(agent, "progress_log")
    assert not hasattr(agent, "_progress_log")


def test_changing_args_resets_stall_detection() -> None:
    responses: list[AIMessage] = []
    for index in range(5):
        right = 3 if index % 2 == 0 else 4
        responses.append(
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("add_numbers", {"left": 2, "right": right}, f"c{index}")
                ],
            )
        )
    model = FakeModel(responses)
    agent = Agent(model, [add_numbers], max_tool_rounds=4, max_stall_rounds=3)

    result = agent.invoke([])

    # 参数交替变化永远不会被判定为卡死，最终撞工具轮数上限
    assert result.error_code == "tool_round_limit"


def test_changing_results_resets_stall_detection() -> None:
    counter = {"value": 0}

    @tool
    def next_value(unused: str) -> str:
        """每次调用返回递增计数的演示工具"""
        del unused
        counter["value"] += 1
        return f"结果 {counter['value']}"

    next_value.metadata = {"effect": "read_only"}

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("next_value", {"unused": "x"}, f"c{i}")],
            )
            for i in range(5)
        ]
    )
    agent = Agent(model, [next_value], max_tool_rounds=4, max_stall_rounds=3)

    result = agent.invoke([])

    # 每轮结果摘要都不同，不构成卡死
    assert result.error_code == "tool_round_limit"


def test_parallel_call_order_does_not_affect_signature() -> None:
    def response(order: str, call_id: str) -> AIMessage:
        calls = (
            [
                _tool_call("add_numbers", {"left": 2, "right": 3}, f"{call_id}-a"),
                _tool_call("add_numbers", {"left": 5, "right": 7}, f"{call_id}-b"),
            ]
            if order == "ab"
            else [
                _tool_call("add_numbers", {"left": 5, "right": 7}, f"{call_id}-b"),
                _tool_call("add_numbers", {"left": 2, "right": 3}, f"{call_id}-a"),
            ]
        )
        return AIMessage(content="", tool_calls=calls)

    model = FakeModel(
        [response("ab", "r1"), response("ba", "r2"), response("ab", "r3")]
    )
    agent = Agent(model, [add_numbers], max_tool_rounds=5, max_stall_rounds=3)

    result = agent.invoke([])

    assert result.error_code == "agent_stall"


def test_dict_key_order_does_not_affect_signature() -> None:
    results_a = {"status": "success", "content": "5"}
    results_b = {"status": "success", "content": "5"}
    record_a = build_progress_record(
        1,
        [_tool_call("add_numbers", {"left": 2, "right": 3}, "c1")],
        {"c1": _tool_message("c1", "5")},
    )
    record_b = build_progress_record(
        7,
        [_tool_call("add_numbers", {"right": 3, "left": 2}, "c9")],
        {"c9": _tool_message("c9", "5")},
    )

    assert record_a.stall_signature == record_b.stall_signature
    assert record_a.round_index != record_b.round_index
    assert results_a == results_b


def test_error_results_count_toward_stall() -> None:
    @tool
    def always_fails(unused: str) -> str:
        """始终抛出业务失败的演示工具"""
        del unused
        raise ToolExecutionError("boom")

    always_fails.metadata = {"effect": "read_only"}

    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("always_fails", {"unused": "x"}, f"c{i}")],
            )
            for i in range(3)
        ]
    )
    agent = Agent(model, [always_fails], max_tool_rounds=5, max_stall_rounds=3)

    result = agent.invoke([])

    # 反复撞同一个被拒工具正是典型死循环，必须计数
    assert result.error_code == "agent_stall"


def test_max_stall_rounds_validation() -> None:
    with pytest.raises(ValueError, match="max_stall_rounds"):
        Agent(FakeModel([]), [add_numbers], max_tool_rounds=2, max_stall_rounds=1)
    with pytest.raises(ValueError, match="max_stall_rounds"):
        Agent(FakeModel([]), [add_numbers], max_tool_rounds=2, max_stall_rounds=True)  # type: ignore[arg-type]


def _tool_message(call_id: str, content: str) -> object:
    from langchain_core.messages import ToolMessage

    return ToolMessage(content=content, tool_call_id=call_id, name="add_numbers")


def test_registry_rejects_tools_without_effect() -> None:
    from langchain_core.tools import tool as tool_decorator

    @tool_decorator
    def no_effect(unused: str) -> str:
        """未声明 effect 的工具"""
        del unused
        return "ok"

    with pytest.raises(ValueError, match="effect"):
        Agent(FakeModel([]), ToolRegistry([no_effect]), max_tool_rounds=2)
