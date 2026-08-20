"""需要显式开启网络访问的 DeepSeek 手动 smoke test。"""

import os

import pytest
from langchain_core.tools import tool

from my_agent.config import load_config
from my_agent.llm import create_chat_model

# 等价于给每个测试函数分别添加：
# @pytest.mark.live
# @pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason=...)
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1",
        reason="设置 RUN_LIVE_TESTS=1 后才允许访问 DeepSeek",
    ),
]


def _response_text(response: object) -> str:
    """将 LangChain 响应内容转为可断言的文本。"""
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    return str(content)


def _tool_calls(response: object) -> list[dict[str, object]]:
    """安全地提取响应中的 tool_calls，避免属性缺失导致误判。"""
    calls = getattr(response, "tool_calls", None) or []
    return list(calls)


def _create_live_model():
    """只在 live 测试已经通过 skip 条件后读取配置并创建模型。"""
    return create_chat_model(load_config())


def test_deepseek_returns_a_simple_response() -> None:
    """确认当前配置能够获得一条最基本的模型回复，且不误调工具。"""
    response = _create_live_model().invoke(
        "请只回复：smoke test ok。不要调用工具，不要添加其他内容。"
    )

    text = _response_text(response).strip()
    assert text, "模型返回了空回复"
    assert "smoke test ok" in text.lower(), f"回复内容不符合预期: {text!r}"
    assert not _tool_calls(response), "普通回答测试中模型不应返回工具调用"


@tool
def add_numbers_for_smoke_test(a: int, b: int) -> int:
    """将两个整数相加，仅用于验证模型是否能生成工具调用。"""
    return a + b


def test_deepseek_can_return_a_tool_call() -> None:
    """确认模型能够识别工具并返回工具调用，而不是直接回答。"""
    model = _create_live_model().bind_tools([add_numbers_for_smoke_test])
    response = model.invoke(
        "请使用 add_numbers_for_smoke_test 计算 2 加 3。"
        "必须先调用工具，不要直接给出结果。"
    )

    tool_calls = _tool_calls(response)
    assert tool_calls, (
        "当前模型没有返回工具调用；如果模型不支持 tool calling，"
        "请先更换 MODEL_ID 配置，不要修改 Agent 框架。"
    )

    matching_calls = [
        call for call in tool_calls
        if call.get("name") == "add_numbers_for_smoke_test"
    ]
    assert matching_calls, (
        "模型返回了工具调用，但没有调用预期的 add_numbers_for_smoke_test: "
        f"{tool_calls!r}"
    )

    args = matching_calls[0].get("args") or {}
    assert int(args.get("a")) == 2, f"工具调用参数不符合预期: {args!r}"
    assert int(args.get("b")) == 3, f"工具调用参数不符合预期: {args!r}"
