"""Shared domain types and errors for the agent."""

from dataclasses import dataclass

from langchain_core.messages import BaseMessage


@dataclass(frozen=True)
class AgentRunContext:
    """单次 Agent.invoke 的调用级上下文，调用结束即丢弃。"""

    run_id: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class AgentResult:
    """一次 ``Agent.invoke`` 的结果。

    可预期的模型、工具和运行时失败通过 ``error_code`` 返回，而不是把
    安全用户提示和底层 traceback 混在一起。``messages`` 只表示截至当前
    边界已经确认、可以再次交给消息转换器的消息状态。
    """

    reply: str
    messages: tuple[BaseMessage, ...]
    tool_rounds: int
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether this result represents a normal final answer."""
        return self.error_code is None
