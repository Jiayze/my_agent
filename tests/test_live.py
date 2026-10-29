"""需要显式开启网络访问的 OpenAI 兼容模型手动 smoke test。"""

import os

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from my_agent.agent import Agent
from my_agent.config import ConfigurationError, load_config, safe_url_summary
from my_agent.conversation import Conversation
from my_agent.llm import create_chat_model
from my_agent.tools import add_numbers

# 等价于给每个测试函数分别添加：
# @pytest.mark.live
# @pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason=...)
pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1",
        reason="设置 RUN_LIVE_TESTS=1 后才允许访问外部模型服务",
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


def test_openai_compatible_model_returns_a_simple_response() -> None:
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


def test_openai_compatible_model_can_return_a_tool_call() -> None:
    """确认模型能够识别工具并返回工具调用，而不是直接回答。"""
    model = _create_live_model().bind_tools([add_numbers_for_smoke_test])
    response = model.invoke(
        "请使用 add_numbers_for_smoke_test 计算 2 加 3。"
        "必须先调用工具，不要直接给出结果。"
    )

    tool_calls = _tool_calls(response)
    assert tool_calls, (
        "当前模型没有返回工具调用；如果模型不支持 tool calling，"
        "请先更换 CHAT_MODEL_ID 配置，不要修改 Agent 框架。"
    )

    matching_calls = [
        call for call in tool_calls if call.get("name") == "add_numbers_for_smoke_test"
    ]
    assert matching_calls, (
        "模型返回了工具调用，但没有调用预期的 add_numbers_for_smoke_test: "
        f"{tool_calls!r}"
    )

    args = matching_calls[0].get("args") or {}
    assert int(args.get("a")) == 2, f"工具调用参数不符合预期: {args!r}"
    assert int(args.get("b")) == 3, f"工具调用参数不符合预期: {args!r}"


class _CallBudget:
    """为 live 测试设置硬性模型调用次数上限"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.last_error: BaseException | None = None

    def consume(self) -> None:
        if self.used >= self.limit:
            raise RuntimeError("live model call budget exhausted")
        self.used += 1


class _BudgetedModel:
    """在真实模型外层计数，不改变请求与响应内容"""

    def __init__(self, model: object, budget: _CallBudget) -> None:
        self._model = model
        self._budget = budget

    def bind_tools(self, tools: object) -> "_BudgetedModel":
        bound = self._model.bind_tools(tools)  # type: ignore[attr-defined]
        return _BudgetedModel(bound, self._budget)

    def invoke(self, messages: object, config: object | None = None) -> object:
        self._budget.consume()
        try:
            return self._model.invoke(messages, config=config)  # type: ignore[attr-defined]
        except Exception as error:
            self._budget.last_error = error
            raise


def _report_live_failure(
    stage: str, error_code: str | None, budget: _CallBudget
) -> None:
    """只输出异常类型与状态码，不输出响应正文或任何密钥"""
    error = budget.last_error
    status = getattr(error, "status_code", None) if error is not None else None
    print(
        "LIVE_UNVERIFIED "
        f"stage={stage} error_code={error_code} "
        f"exception={type(error).__name__ if error else 'none'} "
        f"status={status} model_calls={budget.used}"
    )


def test_deepseek_thinking_tool_loop_live() -> None:
    """真实 model → tools → model → 第二轮，验证 reasoning_content 回传

    仅在显式设置 CHAT_MODEL_THINKING=enabled 时运行；最多 6 次模型调用，
    失败立即停止，不重试、不新建密钥、不打印推理正文。
    """
    try:
        config = load_config()
    except ConfigurationError as error:
        if error.field == "CHAT_MODEL_API_KEY":
            pytest.skip("LIVE_UNVERIFIED: 未配置 CHAT_MODEL_API_KEY")
        raise
    if not config.thinking_enabled:
        pytest.skip("LIVE_UNVERIFIED: 需要显式设置 CHAT_MODEL_THINKING=enabled")

    budget = _CallBudget(6)
    model = _BudgetedModel(create_chat_model(config), budget)
    conversation = Conversation(
        conversation_id="live-thinking",
        agent=Agent(model, [add_numbers], max_tool_rounds=2),  # type: ignore[arg-type]
        system_prompt="你是测试助手。用户要求计算时必须调用 add_numbers。",
    )

    first = conversation.send("请使用 add_numbers 计算 2 加 3，必须先调用工具。")
    if not first.ok:
        _report_live_failure("first_turn", first.error_code, budget)
        pytest.fail(f"live thinking first turn failed: {first.error_code}")

    assistant_calls = [
        message
        for message in first.messages
        if isinstance(message, AIMessage) and message.tool_calls
    ]
    assert assistant_calls, "真实模型没有返回工具调用"

    call = assistant_calls[0].tool_calls[0]
    tool_messages = [
        message
        for message in first.messages
        if isinstance(message, ToolMessage) and message.tool_call_id == call["id"]
    ]
    assert tool_messages, "工具结果没有匹配到 tool_call_id"
    assert float(tool_messages[0].content) == 5.0

    reasoning_lengths = [
        len(message.additional_kwargs["reasoning_content"])
        for message in first.messages
        if isinstance(message, AIMessage)
        and isinstance(message.additional_kwargs.get("reasoning_content"), str)
        and message.additional_kwargs["reasoning_content"]
    ]
    assert reasoning_lengths, "思考模式响应未保留 reasoning_content"

    second = conversation.send("再重复一次结果。")
    if not second.ok:
        _report_live_failure("second_turn", second.error_code, budget)
        pytest.fail(f"live thinking second turn failed: {second.error_code}")

    assert budget.used <= 6
    print("LIVE_EVIDENCE")
    print(f"model_id={config.model_id}")
    print(f"base_url={safe_url_summary(config.base_url)}")
    print(f"thinking={config.thinking_enabled} effort={config.reasoning_effort}")
    print(f"model_calls={budget.used}")
    print(f"tool={call['name']} args={call['args']} result={tool_messages[0].content}")
    print(
        "reasoning_present=True "
        f"reasoning_lengths={reasoning_lengths} "
        "reasoning_text_recorded=False"
    )
