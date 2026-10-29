"""DeepSeek 思考模式离线 wire 契约测试

全部请求由 ``httpx.MockTransport`` 拦截，不访问网络，也不读取真实密钥。
覆盖：请求参数、非流式解码、流式增量聚合、工具闭环回传、无 tool-call 回合回传。
"""

import json
from collections.abc import Callable

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.prompt_values import ChatPromptValue
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from my_agent.agent import Agent
from my_agent.conversation import Conversation
from my_agent.deepseek_chat import DeepSeekChatModel
from my_agent.tools import add_numbers

Handler = Callable[[httpx.Request], httpx.Response]


def _completion(message: dict, finish_reason: str = "stop") -> dict:
    """构造最小 Chat Completion 响应"""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _tool_call_message(reasoning: str) -> dict:
    """构造带 reasoning_content 的 tool-call assistant 消息"""
    return {
        "role": "assistant",
        "content": "",
        "reasoning_content": reasoning,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "add_numbers",
                    "arguments": '{"left": 2, "right": 3}',
                },
            }
        ],
    }


def _build_model(handler: Handler) -> tuple[DeepSeekChatModel, httpx.Client]:
    """创建走 MockTransport 的真实适配器实例"""
    return _build_model_with(handler)


def _build_model_with(
    handler: Handler,
    **overrides: object,
) -> tuple[DeepSeekChatModel, httpx.Client]:
    """创建可覆盖构造参数的适配器实例（默认模拟思考模式已开启）"""
    client = httpx.Client(transport=httpx.MockTransport(handler))
    params: dict[str, object] = {
        "model": "deepseek-flash",
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "http_client": client,
        "max_retries": 0,
        "extra_body": {"thinking": {"type": "enabled"}},
        "reasoning_effort": "high",
    }
    params.update(overrides)
    return DeepSeekChatModel(**params), client  # type: ignore[arg-type]


def _assistant_messages(payload: dict) -> list[dict]:
    """取出请求体中的 assistant 消息"""
    return [
        message for message in payload["messages"] if message.get("role") == "assistant"
    ]


def test_agent_tool_loop_returns_reasoning_and_matching_tool_result() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json=_completion(
                    _tool_call_message("需要先调用加法工具"), "tool_calls"
                ),
            )
        return httpx.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": "结果是 5。",
                    "reasoning_content": "工具返回了 5，可以作答",
                }
            ),
        )

    model, client = _build_model(handler)
    try:
        result = Agent(model, [add_numbers], max_tool_rounds=2).invoke(
            [HumanMessage("请计算 2 加 3")]
        )
    finally:
        client.close()

    assert result.ok
    assert result.reply == "结果是 5。"
    assert result.tool_rounds == 1

    first, second = payloads
    assert first["thinking"] == {"type": "enabled"}
    assert first["reasoning_effort"] == "high"
    assert "temperature" not in first
    assert first["tools"][0]["function"]["name"] == "add_numbers"

    assistant = _assistant_messages(second)[0]
    assert assistant["reasoning_content"] == "需要先调用加法工具"
    tool_message = next(
        message for message in second["messages"] if message["role"] == "tool"
    )
    assert tool_message["tool_call_id"] == "call-1"
    assert float(tool_message["content"]) == 5.0

    final = result.messages[-1]
    assert isinstance(final, AIMessage)
    assert final.additional_kwargs["reasoning_content"] == "工具返回了 5，可以作答"

    tool_call_message = next(
        message
        for message in result.messages
        if isinstance(message, AIMessage) and message.tool_calls
    )
    assert tool_call_message.additional_kwargs["reasoning_content"] == (
        "需要先调用加法工具"
    )


def test_second_turn_replays_reasoning_of_assistant_without_tool_calls() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json=_completion(_tool_call_message("第一回合需要工具"), "tool_calls"),
            )
        if len(payloads) == 2:
            return httpx.Response(
                200,
                json=_completion(
                    {
                        "role": "assistant",
                        "content": "5",
                        "reasoning_content": "第一回合最终回答的推理",
                    }
                ),
            )
        return httpx.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": "再次回答",
                    "reasoning_content": "第二回合推理",
                }
            ),
        )

    model, client = _build_model(handler)
    try:
        conversation = Conversation(
            conversation_id="wire",
            agent=Agent(model, [add_numbers], max_tool_rounds=2),
            system_prompt="你是测试助手。",
        )
        assert conversation.send("计算 2 加 3").ok
        assert conversation.send("再说一次").ok
    finally:
        client.close()

    third = payloads[2]
    assert [message["role"] for message in third["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assistants = _assistant_messages(third)
    assert [message["reasoning_content"] for message in assistants] == [
        "第一回合需要工具",
        "第一回合最终回答的推理",
    ]
    # 无 tool-call 的 assistant 也必须回传，且顺序保持在原位置。
    assert assistants[1]["content"] == "5"
    assert "tool_calls" not in assistants[1]
    tool_message = next(
        message for message in third["messages"] if message["role"] == "tool"
    )
    assert tool_message["tool_call_id"] == "call-1"


def test_streaming_chunks_aggregate_reasoning_and_replay_it() -> None:
    payloads: list[dict] = []

    def stream_response() -> httpx.Response:
        deltas = [
            {"role": "assistant", "reasoning_content": "think "},
            {"reasoning_content": "more"},
            {"content": "hello"},
            {},
        ]
        chunks = [
            {
                "id": "chatcmpl-stream",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": "stop" if index == 3 else None,
                    }
                ],
            }
            for index, delta in enumerate(deltas)
        ]
        body = (
            "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
            + "data: [DONE]\n\n"
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=body.encode(),
        )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload.get("stream"):
            return stream_response()
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "done"}),
        )

    model, client = _build_model(handler)
    try:
        chunks = list(model.stream([HumanMessage("hi")]))
        combined = chunks[0]
        for chunk in chunks[1:]:
            combined = combined + chunk

        model.invoke([HumanMessage("hi"), combined, HumanMessage("again")])
    finally:
        client.close()

    assert combined.content == "hello"
    assert combined.additional_kwargs["reasoning_content"] == "think more"

    replay = payloads[1]
    assistant = _assistant_messages(replay)[0]
    assert assistant["reasoning_content"] == "think more"


def test_plain_chat_openai_drops_reasoning_field() -> None:
    """证明未开启思考模式时仍是原始 OpenAI 兼容路径"""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    model = ChatOpenAI(
        model="test-model",
        api_key="test-key",
        base_url="https://example.invalid/v1",
        http_client=client,
        max_retries=0,
    )
    try:
        model.invoke(
            [
                HumanMessage("hi"),
                AIMessage(
                    content="previous",
                    additional_kwargs={"reasoning_content": "should not be sent"},
                ),
                HumanMessage("again"),
            ]
        )
    finally:
        client.close()

    request_body = payloads[0]
    assert "thinking" not in request_body
    assert "reasoning_effort" not in request_body
    assert "reasoning_content" not in _assistant_messages(request_body)[0]


def test_assistant_without_reasoning_keeps_request_unchanged() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )

    model, client = _build_model(handler)
    try:
        model.invoke([AIMessage(content="plain assistant")])
    finally:
        client.close()

    assert "reasoning_content" not in _assistant_messages(payloads[0])[0]


def test_tool_message_requires_matching_tool_call_id() -> None:
    """ToolMessage 与 tool_call 的 id 必须逐条对应"""
    model, client = _build_model(
        lambda request: httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )
    )
    try:
        messages = [
            AIMessage(
                content="",
                additional_kwargs={"reasoning_content": "r"},
                tool_calls=[
                    {
                        "name": "add_numbers",
                        "args": {"left": 1, "right": 2},
                        "id": "call-9",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content="3", tool_call_id="call-9"),
        ]
        payload = model._get_request_payload(messages)
    finally:
        client.close()

    tool_payload = next(
        message for message in payload["messages"] if message["role"] == "tool"
    )
    assert tool_payload["tool_call_id"] == "call-9"
    assert payload["messages"][0]["tool_calls"][0]["id"] == "call-9"


@pytest.mark.parametrize("reasoning", ["", "exact text"])
def test_reasoning_content_is_replayed_byte_exact(reasoning: str) -> None:
    model, client = _build_model(
        lambda request: httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )
    )
    try:
        payload = model._get_request_payload(
            [AIMessage(content="x", additional_kwargs={"reasoning_content": reasoning})]
        )
    finally:
        client.close()

    assert payload["messages"][0]["reasoning_content"] == reasoning


def test_reasoning_text_is_never_written_to_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_reasoning = "INTERNAL-REASONING-MUST-NOT-APPEAR"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": secret_reasoning,
                }
            ),
        )

    model, client = _build_model(handler)
    try:
        with caplog.at_level("DEBUG"):
            Agent(model, [add_numbers], max_tool_rounds=1).invoke([HumanMessage("hi")])
    finally:
        client.close()

    assert secret_reasoning not in caplog.text


def _sse_response(deltas: list[tuple[dict, str | None]]) -> httpx.Response:
    """把 (delta, finish_reason) 序列编码成 Chat Completions SSE 响应"""
    chunks = [
        {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "deepseek-flash",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        for delta, finish in deltas
    ]
    body = (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body.encode(),
    )


# reasoning 增量与 tool_call 增量交替出现，且函数名、参数、id 都被拆到不同分片
INTERLEAVED_TOOL_CALL_DELTAS: list[tuple[dict, str | None]] = [
    ({"role": "assistant", "reasoning_content": "先看"}, None),
    ({"reasoning_content": "参数"}, None),
    (
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-7",
                    "type": "function",
                    "function": {"name": "add_numbers", "arguments": '{"left": 2,'},
                }
            ]
        },
        None,
    ),
    ({"reasoning_content": "再调用"}, None),
    (
        {"tool_calls": [{"index": 0, "function": {"arguments": ' "right": 3}'}}]},
        None,
    ),
    ({}, "tool_calls"),
]


def _aggregate_stream(model: DeepSeekChatModel) -> AIMessage:
    """聚合流式分片为单条消息"""
    combined: AIMessage | None = None
    for chunk in model.stream([HumanMessage("请计算 2 加 3")]):
        combined = chunk if combined is None else combined + chunk
    assert combined is not None
    return combined


def test_streamed_tool_call_fragments_merge_with_interleaved_reasoning() -> None:
    """tool_call 分片与 reasoning 分片交错时，聚合结果仍完整"""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return _sse_response(INTERLEAVED_TOOL_CALL_DELTAS)

    model, client = _build_model(handler)
    try:
        combined = _aggregate_stream(model)
    finally:
        client.close()

    assert payloads[0]["stream"] is True
    assert combined.tool_calls == [
        {
            "name": "add_numbers",
            "args": {"left": 2, "right": 3},
            "id": "call-7",
            "type": "tool_call",
        }
    ]
    assert combined.additional_kwargs["reasoning_content"] == "先看参数再调用"


def test_streamed_tool_call_round_trips_through_real_tool_node() -> None:
    """流式聚合 -> 真实 ToolNode -> 下一请求完整回传 reasoning 与 tool_call"""
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload.get("stream"):
            return _sse_response(INTERLEAVED_TOOL_CALL_DELTAS)
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "结果是 5。"}),
        )

    model, client = _build_model(handler)
    try:
        combined = _aggregate_stream(model)
        tool_state = ToolNode([add_numbers]).invoke(
            {"messages": [combined]},
            config={"configurable": {}},
            runtime=Runtime(),
        )
        tool_messages = tool_state["messages"]
        assert len(tool_messages) == 1

        model.invoke([HumanMessage("请计算 2 加 3"), combined, *tool_messages])
    finally:
        client.close()

    assert len(payloads) == 2
    assert payloads[1]["stream"] is False

    replay = payloads[1]["messages"]
    assert [message["role"] for message in replay] == ["user", "assistant", "tool"]

    assistant = replay[1]
    assert assistant["reasoning_content"] == "先看参数再调用"
    assert len(assistant["tool_calls"]) == 1
    call = assistant["tool_calls"][0]
    assert call["id"] == "call-7"
    assert call["type"] == "function"
    assert call["function"]["name"] == "add_numbers"
    assert json.loads(call["function"]["arguments"]) == {"left": 2, "right": 3}

    tool_message = replay[2]
    assert tool_message["tool_call_id"] == "call-7"
    assert float(tool_message["content"]) == 5.0


def test_agent_invoke_uses_non_streaming_transport_only() -> None:
    """Agent.invoke 走 non-stream 图执行，Agent + 流式模型组合不可用

    因此这里只验证 Agent 请求体从不携带 ``stream``，不伪造流式 Agent。
    """
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )

    model, client = _build_model(handler)
    try:
        result = Agent(model, [add_numbers], max_tool_rounds=1).invoke(
            [HumanMessage("hi")]
        )
    finally:
        client.close()

    assert result.ok
    assert len(payloads) == 1
    assert payloads[0]["stream"] is False
    assert "stream_options" not in payloads[0]


@pytest.mark.parametrize(
    "overrides",
    [
        {"use_responses_api": True},
        # 该模型名在父类里默认倾向 Responses API
        {"model": "gpt-5-pro"},
        {"output_version": "responses/v1"},
    ],
)
def test_responses_api_is_forced_to_chat_completions(
    overrides: dict[str, object],
) -> None:
    """显式或默认触发 Responses API 时仍必须打到 /chat/completions"""
    requests: list[httpx.Request] = []
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )

    model, client = _build_model_with(handler, **overrides)
    try:
        assert model._use_responses_api({"messages": []}) is False
        result = model.invoke([HumanMessage("hi")])
    finally:
        client.close()

    assert result.content == "ok"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/chat/completions"
    assert "messages" in payloads[0]
    assert "input" not in payloads[0]


def test_streaming_route_stays_on_chat_completions_with_responses_enabled() -> None:
    """流式路径同样不路由到 Responses API"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _sse_response([({"content": "ok"}, "stop")])

    model, client = _build_model_with(handler, use_responses_api=True)
    try:
        combined = _aggregate_stream(model)
    finally:
        client.close()

    assert combined.content == "ok"
    assert [request.url.path for request in requests] == ["/v1/chat/completions"]


def _reasoning_assistant() -> AIMessage:
    return AIMessage(
        content="answer",
        additional_kwargs={"reasoning_content": "keep-me"},
    )


def test_supported_input_shapes_never_lose_assistant_reasoning() -> None:
    """父类不做消息合并/拆分，因此 reasoning 不会被位置对齐逻辑丢弃

    ``_get_request_payload`` 只在请求体消息数与转换后消息数一致时才回填，
    这里覆盖字符串、元组列表、PromptValue 以及含 SystemMessage 的输入形态。
    """
    model, client = _build_model(
        lambda request: httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )
    )
    try:
        inputs = [
            [HumanMessage("hi"), _reasoning_assistant(), HumanMessage("again")],
            [SystemMessage("sys"), HumanMessage("hi"), _reasoning_assistant()],
            ChatPromptValue(messages=[SystemMessage("sys"), _reasoning_assistant()]),
            "plain string",
            [("human", "hi"), ("assistant", "yo")],
        ]
        for input_ in inputs:
            payload = model._get_request_payload(input_)
            converted = model._convert_input(input_).to_messages()
            payload_messages = payload["messages"]

            assert len(payload_messages) == len(converted)
            replayed = [
                message.get("reasoning_content")
                for message in payload_messages
                if message["role"] == "assistant"
            ]
            # 每条 assistant 的位置与源消息严格一一对应，不出现静默丢弃
            assert replayed == [
                message.additional_kwargs.get("reasoning_content")
                if isinstance(message, AIMessage)
                else None
                for message in converted
                if isinstance(message, AIMessage)
            ]
    finally:
        client.close()


def test_system_message_is_not_merged_away() -> None:
    """SystemMessage 保持独立条目，不会被父类折叠进首条 user 消息"""
    model, client = _build_model(
        lambda request: httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )
    )
    try:
        payload = model._get_request_payload(
            ChatPromptValue(messages=[SystemMessage("sys"), HumanMessage("hi")])
        )
    finally:
        client.close()

    assert [message["role"] for message in payload["messages"]] == [
        "system",
        "user",
    ]
    assert payload["messages"][0]["content"] == "sys"


def test_disabled_thinking_keeps_request_parameters_identical() -> None:
    """未开启思考模式时，适配器与原生 ChatOpenAI 的请求体完全一致"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion({"role": "assistant", "content": "ok"}),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    common: dict[str, object] = {
        "model": "deepseek-flash",
        "api_key": "test-key",
        "base_url": "https://example.invalid/v1",
        "http_client": client,
        "max_retries": 0,
        "temperature": 0.2,
    }
    messages = [HumanMessage("hi")]
    try:
        adapter_payload = DeepSeekChatModel(**common)._get_request_payload(  # type: ignore[arg-type]
            messages
        )
        plain_payload = ChatOpenAI(**common)._get_request_payload(  # type: ignore[arg-type]
            messages
        )
    finally:
        client.close()

    assert adapter_payload == plain_payload
    assert adapter_payload["temperature"] == 0.2
    assert "thinking" not in adapter_payload
    assert "reasoning_effort" not in adapter_payload
