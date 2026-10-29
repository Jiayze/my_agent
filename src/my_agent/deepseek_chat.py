"""DeepSeek 思考模式的最小兼容层

为什么需要这一层：

* ``langchain-openai`` 的 ``ChatOpenAI`` 只覆盖 OpenAI 官方字段，明确声明不会
  提取或保留 ``reasoning_content`` 这类第三方扩展字段
  （``langchain_openai/chat_models/base.py`` 模块文档）。
* 官方 ``langchain-deepseek`` 的 ``ChatDeepSeek`` 会把响应中的
  ``reasoning_content`` 解码到 ``AIMessage.additional_kwargs``，但不会在后续请求
  中把它序列化回 assistant 消息（2026 公开 issue #37713 / #37174 指向该缺口）。
* DeepSeek 思考模式要求：只要请求携带 ``tools``，历史中**每一条** assistant 消息
  的 ``reasoning_content`` 都必须原样回传，包括没有 tool call 的回合，否则返回
  HTTP 400。

本类只补齐“解码 + 序列化”两个缺口，并强制走 Chat Completions；其余行为完全继承
``ChatOpenAI``。仅在用户显式打开 ``CHAT_MODEL_THINKING`` 时启用，非 DeepSeek
自定义 base URL 不会被自动套用。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

_REASONING_CONTENT_KEY = "reasoning_content"


def _read_reasoning_content(message: object) -> str | None:
    """从 SDK 对象或响应字典中读取 reasoning_content"""
    if isinstance(message, dict):
        value = message.get(_REASONING_CONTENT_KEY)
    else:
        value = getattr(message, _REASONING_CONTENT_KEY, None)
        if value is None:
            extra = getattr(message, "model_extra", None)
            if isinstance(extra, dict):
                value = extra.get(_REASONING_CONTENT_KEY)
    return value if isinstance(value, str) else None


def _extract_reasoning_content(response: object) -> str | None:
    """从非流式 Chat Completion 响应中提取首条 choice 的 reasoning_content"""
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if not choices:
            return None
        return _read_reasoning_content(choices[0].get("message"))

    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    return _read_reasoning_content(choices[0].message)


class DeepSeekChatModel(ChatOpenAI):
    """保留并回传 DeepSeek ``reasoning_content`` 的 Chat Completions 适配器

    该适配器不构造、不推断思维链：它只搬运服务端实际返回的文本，调用方必须把
    该字段视为内部数据，不得输出到用户终端或日志。
    """

    def _use_responses_api(self, payload: dict) -> bool:
        """DeepSeek 只提供 Chat Completions，禁止路由到 Responses API"""
        return False

    def _create_chat_result(
        self,
        response: dict | object,
        generation_info: dict | None = None,
    ) -> ChatResult:
        """在父类解析结果上补充非流式 reasoning_content"""
        result = super()._create_chat_result(response, generation_info)
        reasoning = _extract_reasoning_content(response)
        if reasoning is None or not result.generations:
            return result

        message = result.generations[0].message
        if isinstance(message, AIMessage):
            message.additional_kwargs[_REASONING_CONTENT_KEY] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """在流式增量中补充 reasoning_content

        该值保持为字符串增量；``AIMessageChunk`` 相加时字符串会按顺序拼接，
        因此调用方聚合后的完整消息与真实回传内容逐字一致。
        """
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk,
            default_chunk_class,
            base_generation_info,
        )
        if generation_chunk is None:
            return None

        choices = chunk.get("choices") or []
        if not choices:
            return generation_chunk
        delta = choices[0].get("delta") or {}
        reasoning = delta.get(_REASONING_CONTENT_KEY)
        if isinstance(reasoning, str) and isinstance(
            generation_chunk.message,
            AIMessageChunk,
        ):
            generation_chunk.message.additional_kwargs[_REASONING_CONTENT_KEY] = (
                reasoning
            )
        return generation_chunk

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """把历史 assistant 消息中的 reasoning_content 原样写回请求体

        DeepSeek 要求携带 ``tools`` 的请求回传所有历史 assistant 的该字段，
        包括没有 tool call 的回合。没有该字段的回合保持原样，不伪造空思维链。
        """
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        messages = self._convert_input(input_).to_messages()
        payload_messages = payload.get("messages")
        if not isinstance(payload_messages, list) or len(payload_messages) != len(
            messages
        ):
            return payload

        for message, message_dict in zip(messages, payload_messages):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get(_REASONING_CONTENT_KEY)
            if isinstance(reasoning, str):
                message_dict[_REASONING_CONTENT_KEY] = reasoning
        return payload
