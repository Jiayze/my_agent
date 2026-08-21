"""Shared domain types and errors for the agent."""

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.messages import BaseMessage


class ModelConfigurationError(ValueError):
    """Raised when model configuration is invalid or client creation fails."""


# V0.1 只定义和传递，不消费事件/取消语义。
EventSink = Callable[[object], None]
CancelSignal = Callable[[], bool]


@dataclass(frozen=True)
class AgentRunContext:
    """单次 Agent.invoke 的调用级上下文，调用结束即丢弃。"""

    run_id: str
    conversation_id: str | None = None
    event_sink: EventSink | None = None
    cancel_signal: CancelSignal | None = None


@dataclass(frozen=True)
class AgentResult:
    """一次 Agent.invoke 的成功结果。"""

    reply: str
    messages: tuple[BaseMessage, ...]
    tool_rounds: int
