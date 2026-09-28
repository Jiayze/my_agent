"""Agent 使用的共享领域类型和错误"""

from dataclasses import dataclass

from langchain_core.messages import BaseMessage
from langchain_core.tools import ToolException


class ModelProtocolError(RuntimeError):
    """模型适配器返回了不符合 Agent 协议的对象"""


class ToolExecutionError(ToolException):
    """第一方工具明确声明的可预期业务失败"""


@dataclass(frozen=True)
class AgentRunContext:
    """单次 Agent.invoke 的调用级上下文调用结束即丢弃"""

    run_id: str
    conversation_id: str | None = None


@dataclass(frozen=True)
class AgentResult:
    """一次 ``Agent.invoke`` 的结果

    可预期的模型、工具和运行时失败通过 ``error_code`` 返回，``messages`` 只表示截至当前
    边界已经确认且可以再次交给消息转换器的消息状态
    """

    reply: str
    messages: tuple[BaseMessage, ...]
    tool_rounds: int
    error_code: str | None = None

    @property
    def ok(self) -> bool:
        """返回结果是否表示正常完成的最终回答"""
        return self.error_code is None
