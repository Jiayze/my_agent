"""按对话隔离的短期记忆与上下文压缩接口"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from threading import RLock
from typing import Protocol
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from my_agent.agent import Agent
from my_agent.models import AgentResult, AgentRunContext

DEFAULT_MAX_CONTEXT_TURNS = 5
TRUNCATION_NOTICE = "（对话过长，较早的内容已截断，只保留最近几轮。）"


class ContextCompactor(Protocol):
    """返回仍可交给模型的消息

    实现必须保留首条 system prompt，并保留每个 AI tool-call 与其
    ToolMessage 结果的完整配对；不能留下孤儿或重复的 tool_call_id
    """

    def compact(
        self,
        messages: tuple[BaseMessage, ...],
    ) -> tuple[BaseMessage, ...]:
        """压缩消息历史并返回仍可交给模型的消息序列"""
        ...


@dataclass(frozen=True)
class ConversationTurn:
    """一个完整的逻辑轮次从 HumanMessage 开始到下一 HumanMessage 前结束"""

    messages: tuple[BaseMessage, ...]


def _validate_message_protocol(messages: Sequence[BaseMessage]) -> None:
    """验证本地消息历史中的 tool-call/result 配对"""
    pending_ids: set[str] = set()
    result_ids: set[str] = set()
    seen_ids: set[str] = set()

    for message in messages:
        if isinstance(message, AIMessage):
            if pending_ids:
                raise ValueError("message history contains an unclosed tool call")
            for call in message.tool_calls:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("every tool call must have a non-empty id")
                if call_id in seen_ids:
                    raise ValueError(f"duplicate tool_call_id: {call_id!r}")
                seen_ids.add(call_id)
                pending_ids.add(call_id)
            continue

        if isinstance(message, ToolMessage):
            call_id = message.tool_call_id
            if isinstance(call_id, str) and call_id in result_ids:
                raise ValueError(f"duplicate ToolMessage for tool_call_id: {call_id!r}")
            if not isinstance(call_id, str) or call_id not in pending_ids:
                raise ValueError(f"orphan ToolMessage for tool_call_id: {call_id!r}")
            pending_ids.remove(call_id)
            result_ids.add(call_id)
            continue

        if pending_ids:
            raise ValueError("message history contains an unclosed tool call")

    if pending_ids:
        raise ValueError("message history contains an unclosed tool call")


def _message_signature(message: BaseMessage) -> tuple[object, ...]:
    """提取覆盖全部模型字段的前缀签名，任一字段被改写都必须被识别"""
    return (
        type(message).__name__,
        *(
            (name, getattr(message, name, None))
            for name in sorted(type(message).model_fields)
        ),
    )


def _validate_result_prefix(
    run_messages: Sequence[BaseMessage],
    result_messages: Sequence[BaseMessage],
) -> None:
    """确保 Agent 结果逐位保留本次提交给模型的消息前缀

    以 ``run_messages`` 为主体单次线性遍历：先判定当前位置是否被改写或
    重排，再判定结果是否已经缩短，最后才允许追加新消息。
    """
    for index, expected in enumerate(run_messages):
        if index >= len(result_messages):
            raise ValueError("Agent result dropped messages from the model input")
        actual = result_messages[index]
        if _message_signature(actual) != _message_signature(expected):
            raise ValueError("Agent result changed the model input message prefix")


def _ensure_message_ids(
    messages: Sequence[BaseMessage],
) -> tuple[BaseMessage, ...]:
    """为所有消息补齐唯一 ID 并拒绝重复 ID

    进入状态机的每条消息都必须有唯一 ID，否则 ID 比对会出现免检通道，
    LangGraph 的 ``add_messages`` 也会按重复 ID 合并消息。
    """
    normalized: list[BaseMessage] = []
    seen_ids: set[str] = set()
    for message in messages:
        message_id = message.id
        if not isinstance(message_id, str) or not message_id:
            message = message.model_copy(update={"id": uuid4().hex})
            message_id = message.id
        if message_id in seen_ids:
            raise ValueError(f"duplicate message id: {message_id!r}")
        seen_ids.add(message_id)
        normalized.append(message)
    return tuple(normalized)


def _clone_container(value: object) -> object:
    """递归复制 dict/list/tuple/set 容器，不可变叶子保持共享"""
    if isinstance(value, dict):
        return {key: _clone_container(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_container(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_container(item) for item in value)
    if isinstance(value, frozenset):
        return frozenset(_clone_container(item) for item in value)
    if isinstance(value, set):
        return {_clone_container(item) for item in value}
    return value


def _clone_message_for_invoke(message: BaseMessage) -> BaseMessage:
    """结构克隆消息中的可变容器，切断与调用方历史的内存别名"""
    updates: dict[str, object] = {
        "content": _clone_container(message.content),
        "additional_kwargs": _clone_container(message.additional_kwargs),
        "response_metadata": _clone_container(message.response_metadata),
    }
    fields = type(message).model_fields
    for field in (
        "tool_calls",
        "invalid_tool_calls",
        "usage_metadata",
        "artifact",
    ):
        if field in fields:
            updates[field] = _clone_container(getattr(message, field))
    return message.model_copy(update=updates)


def _clone_messages_for_invoke(
    messages: Sequence[BaseMessage],
) -> tuple[BaseMessage, ...]:
    """克隆整条输入消息序列，避免 Agent 就地改写污染已提交历史"""
    return tuple(_clone_message_for_invoke(message) for message in messages)


def _split_turns(messages: Sequence[BaseMessage]) -> list[ConversationTurn]:
    """按 HumanMessage 将消息历史拆分为完整逻辑轮次"""
    turns: list[list[BaseMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            turns.append([message])
        else:
            if not turns:
                turns.append([])
            turns[-1].append(message)
    return [ConversationTurn(tuple(turn)) for turn in turns if turn]


def _truncate_by_turns(
    messages: Sequence[BaseMessage],
    max_turns: int = DEFAULT_MAX_CONTEXT_TURNS,
) -> tuple[BaseMessage, ...]:
    """只保留 system prompt 截断提示和最近 N 个完整轮次"""
    if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns < 1:
        raise ValueError("max_turns must be a positive integer")

    if not messages or not isinstance(messages[0], SystemMessage):
        raise ValueError("conversation history must start with a system prompt")

    static_system = messages[0]
    tail = [
        message for message in messages[1:] if not isinstance(message, SystemMessage)
    ]
    turns = _split_turns(tail)

    if len(turns) <= max_turns:
        result = [static_system]
        for turn in turns:
            result.extend(turn.messages)
        return tuple(result)

    result = [static_system, SystemMessage(content=TRUNCATION_NOTICE)]
    for turn in turns[-max_turns:]:
        result.extend(turn.messages)
    return tuple(result)


class Conversation:
    """持有短期记忆同一 conversation_id 的新实例不共享历史"""

    def __init__(
        self,
        conversation_id: str,
        agent: Agent,
        system_prompt: str,
        context_compactor: ContextCompactor | None = None,
    ) -> None:
        """创建对话并初始化系统提示词和短期消息历史"""
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be a non-empty string")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("system_prompt must be a non-empty string")

        self.conversation_id = conversation_id
        self._agent = agent
        self._compactor = context_compactor
        self._lock = RLock()
        self._system_message = SystemMessage(content=system_prompt, id=uuid4().hex)
        self._messages: tuple[BaseMessage, ...] = (self._system_message,)

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        """返回当前对话的不可变消息历史快照

        返回结构克隆而非内部对象，调用方就地改写不会污染内部历史。
        """
        with self._lock:
            return _clone_messages_for_invoke(self._messages)

    def send(self, user_message: str) -> AgentResult:
        """提交一条用户消息并执行一轮 Agent"""
        if not isinstance(user_message, str) or not user_message.strip():
            raise ValueError("user_message must be a non-empty string")

        with self._lock:
            current_user = HumanMessage(content=user_message, id=uuid4().hex)
            pending = self._messages + (current_user,)

            # 在压缩或模型执行前提交用户意图
            # 即使后续步骤失败下一次调用仍然可以看到这条用户消息
            self._messages = pending
            run_messages = self._compact(pending)
            if not any(message.id == current_user.id for message in run_messages):
                raise ValueError("context compactor dropped the current user message")
            run_context = AgentRunContext(
                run_id=uuid4().hex,
                conversation_id=self.conversation_id,
            )
            result = self._agent.invoke(
                _clone_messages_for_invoke(run_messages),
                run_context=run_context,
            )
            committed = _ensure_message_ids(result.messages)

            if (
                not committed
                or committed[0].id != self._system_message.id
                or committed[0].content != self._system_message.content
            ):
                raise ValueError("Agent result dropped the original system prompt")
            if sum(message.id == self._system_message.id for message in committed) != 1:
                raise ValueError("Agent result duplicated the original system prompt")
            if not any(message.id == current_user.id for message in committed):
                raise ValueError("Agent result dropped the current user message")
            _validate_result_prefix(run_messages, committed)
            _validate_message_protocol(committed)
            self._messages = committed
            # 返回克隆快照，避免调用方通过 result.messages 改写内部历史
            return replace(
                result,
                messages=_clone_messages_for_invoke(committed),
            )

    def _compact(
        self,
        messages: tuple[BaseMessage, ...],
    ) -> tuple[BaseMessage, ...]:
        """压缩消息并验证系统提示词与工具消息协议"""
        if self._compactor is None:
            compacted = _truncate_by_turns(messages)
        else:
            compacted = tuple(self._compactor.compact(messages))

        if not compacted:
            raise ValueError("context compactor must return at least one message")
        first = compacted[0]
        if (
            not isinstance(first, SystemMessage)
            or first.id != self._system_message.id
            or first.content != self._system_message.content
        ):
            raise ValueError(
                "context compactor must preserve the original system prompt"
            )
        if sum(message.id == self._system_message.id for message in compacted) != 1:
            raise ValueError(
                "context compactor must preserve one original system prompt"
            )
        compacted = _ensure_message_ids(compacted)
        _validate_message_protocol(compacted)
        return compacted
