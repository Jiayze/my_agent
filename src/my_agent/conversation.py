"""Conversation：按对话隔离的短期记忆与上下文压缩接口。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from my_agent.agent import Agent, AgentRunError

DEFAULT_MAX_CONTEXT_TURNS = 5
TRUNCATION_NOTICE = "（对话过长，较早的内容已截断，只保留最近几轮。）"


class ContextCompactor(Protocol):
    """上下文压缩协议；V0.2 用 token 级摘要实现替代默认硬截断。"""

    def compact(
        self,
        messages: tuple[BaseMessage, ...],
    ) -> tuple[BaseMessage, ...]:
        ...


@dataclass(frozen=True)
class ConversationTurn:
    """一个完整的逻辑轮次：HumanMessage 开始到下一 HumanMessage 前结束。"""

    messages: tuple[BaseMessage, ...]


def _split_turns(messages: Sequence[BaseMessage]) -> list[ConversationTurn]:
    """按 HumanMessage 边界切分成完整轮次。"""
    turns: list[list[BaseMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            turns.append([message])
        else:
            if not turns:
                turns.append([])
            turns[-1].append(message)
    return [ConversationTurn(tuple(turn)) for turn in turns]


def _truncate_by_turns(
    messages: Sequence[BaseMessage],
    max_turns: int = DEFAULT_MAX_CONTEXT_TURNS,
) -> tuple[BaseMessage, ...]:
    """硬截断兜底：只保留静态系统提示词、截断提示和最近 N 个完整轮次。"""
    head: list[BaseMessage] = []
    tail: list[BaseMessage] = []
    for index, message in enumerate(messages):
        if isinstance(message, SystemMessage):
            head.append(message)
        else:
            tail.extend(messages[index:])
            break
    else:
        tail = []

    # 系统提示词只保留第一份；此前截断产生的提示不再累积。
    static = head[:1]
    turns = _split_turns(tail)

    if len(turns) <= max_turns:
        result = list(static)
        for turn in turns:
            result.extend(turn.messages)
        return tuple(result)

    recent = turns[-max_turns:]
    result = list(static)
    result.append(SystemMessage(content=TRUNCATION_NOTICE))
    for turn in recent:
        result.extend(turn.messages)
    return tuple(result)


class Conversation:
    """持有短期记忆；同一 conversation_id 的新实例不共享历史。"""

    def __init__(
        self,
        conversation_id: str,
        agent: Agent,
        system_prompt: str,
        context_compactor: ContextCompactor | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self._agent = agent
        self._compactor = context_compactor
        self._messages: tuple[BaseMessage, ...] = (
            SystemMessage(content=system_prompt),
        )

    @property
    def messages(self) -> tuple[BaseMessage, ...]:
        return self._messages

    def send(self, user_message: str) -> str:
        """处理一条用户消息：追加输入、压缩、执行 Agent、更新历史。"""
        pending = self._messages + (HumanMessage(content=user_message),)
        run_messages = self._compact(pending)

        try:
            result = self._agent.invoke(run_messages)
        except AgentRunError as error:
            # 用错误发生时的完整状态替换历史，再追加一条安全 AI 消息闭合本轮。
            self._messages = error.messages + (
                AIMessage(content=error.safe_message),
            )
            raise

        self._messages = result.messages
        return result.reply

    def _compact(
        self,
        messages: tuple[BaseMessage, ...],
    ) -> tuple[BaseMessage, ...]:
        if self._compactor is None:
            return _truncate_by_turns(messages, DEFAULT_MAX_CONTEXT_TURNS)
        return tuple(self._compactor.compact(messages))
