"""无状态 Agent 执行核心：LangGraph 状态图 + 工具调用闭环。"""

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode

from my_agent.models import AgentResult, AgentRunContext

_SAFE_MODEL_ERROR_MESSAGE = "模型调用失败，请稍后重试。"
_SAFE_EMPTY_MODEL_MESSAGE = "模型返回了空回复，请重试。"
_SAFE_TOOL_ROUND_LIMIT_MESSAGE = "已达到工具调用轮数上限，该工具未执行。"
_SAFE_TOOL_EXECUTION_FAILED_MESSAGE = "工具执行失败，该工具未完成。"
_SAFE_EXECUTION_ERROR_MESSAGE = "Agent 执行失败，请稍后重试。"


class AgentRunError(Exception):
    """Agent 执行失败的基础异常，携带完整且协议有效的消息状态。"""

    def __init__(
        self,
        messages: tuple[BaseMessage, ...],
        safe_message: str,
    ) -> None:
        super().__init__(safe_message)
        self.messages = messages
        self.safe_message = safe_message


class ModelError(AgentRunError):
    """模型调用本身失败。"""


class EmptyModelResponseError(AgentRunError):
    """模型返回了没有工具调用且规范化后文本为空的响应。"""


class ToolRoundLimitError(AgentRunError):
    """工具调用轮数达到上限。"""


class AgentExecutionError(AgentRunError):
    """Agent 内部执行错误。"""


class AgentState(MessagesState):
    tool_rounds: int
    terminal_error: str | None


def _normalize_content(content: object) -> str:
    """从字符串 content 或内容块列表提取最终文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _build_closers(
    messages: Sequence[BaseMessage],
    *,
    content: str,
) -> tuple[ToolMessage, ...]:
    """为最后一条 AIMessage 中尚未闭合的 tool_call 构造安全 ToolMessage。"""
    if not messages:
        return ()
    last = messages[-1]
    if not isinstance(last, AIMessage):
        return ()
    existing_ids = {
        msg.tool_call_id for msg in messages if isinstance(msg, ToolMessage)
    }
    closers: list[ToolMessage] = []
    for call in last.tool_calls or []:
        call_id = call.get("id")
        if call_id and call_id not in existing_ids:
            closers.append(
                ToolMessage(
                    content=content,
                    tool_call_id=call_id,
                    name=call.get("name"),
                )
            )
    return tuple(closers)


class Agent:
    """无状态 Agent 核心。

    只接收完整消息序列并返回结果；不保存对话历史或调用级上下文。
    """

    def __init__(
        self,
        model: BaseChatModel,
        tools: Sequence[BaseTool],
        max_tool_rounds: int,
    ) -> None:
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be >= 1")

        self._tools = tuple(tools)
        self._max_tool_rounds = max_tool_rounds
        self._bound_model = model.bind_tools(self._tools)
        self._tools_node = ToolNode(self._tools, handle_tool_errors=True)
        self._recursion_limit = max(25, 2 * max_tool_rounds + 5)
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        builder = StateGraph(AgentState)
        builder.add_node("model", self._model_node)
        builder.add_node("tools", self._tools_node_fn)
        builder.add_node("increment_tool_rounds", self._increment_tool_rounds)
        builder.add_node("close_unexecuted_tools", self._close_unexecuted_tools)

        builder.add_edge(START, "model")
        builder.add_conditional_edges(
            "model",
            self._route_after_model,
            {
                "tools": "tools",
                "close_unexecuted_tools": "close_unexecuted_tools",
                END: END,
            },
        )
        builder.add_edge("tools", "increment_tool_rounds")
        builder.add_edge("increment_tool_rounds", "model")
        builder.add_edge("close_unexecuted_tools", END)

        return builder.compile()

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        run_context: AgentRunContext | None = None,
    ) -> AgentResult:
        """执行完整消息序列，返回最终回复和完整消息状态。"""
        initial = {
            "messages": list(messages),
            "tool_rounds": 0,
            "terminal_error": None,
        }
        config: RunnableConfig = {"recursion_limit": self._recursion_limit}
        if run_context is not None:
            config["configurable"] = {"run_context": run_context}

        try:
            final = self._graph.invoke(initial, config)
        except AgentRunError:
            raise
        except Exception:
            raise AgentExecutionError(
                tuple(messages), _SAFE_EXECUTION_ERROR_MESSAGE
            ) from None

        final_messages = tuple(final["messages"])

        if final.get("terminal_error") == "tool_round_limit":
            raise ToolRoundLimitError(
                final_messages,
                _SAFE_TOOL_ROUND_LIMIT_MESSAGE,
            )

        if not final_messages or not isinstance(final_messages[-1], AIMessage):
            raise AgentExecutionError(
                final_messages, _SAFE_EXECUTION_ERROR_MESSAGE
            )

        last = final_messages[-1]
        reply = _normalize_content(getattr(last, "content", None))
        if not reply.strip():
            raise EmptyModelResponseError(
                final_messages[:-1],
                _SAFE_EMPTY_MODEL_MESSAGE,
            )

        return AgentResult(
            reply=reply,
            messages=final_messages,
            tool_rounds=final["tool_rounds"],
        )

    def _model_node(
        self,
        state: AgentState,
        config: RunnableConfig,
    ) -> dict[str, list[BaseMessage]]:
        current = tuple(state["messages"])
        try:
            response = self._bound_model.invoke(list(current), config=config)
        except AgentRunError:
            raise
        except Exception:
            raise ModelError(current, _SAFE_MODEL_ERROR_MESSAGE) from None

        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            text = _normalize_content(getattr(response, "content", None))
            if not text.strip():
                raise EmptyModelResponseError(
                    current, _SAFE_EMPTY_MODEL_MESSAGE
                )
        return {"messages": [response]}

    def _tools_node_fn(
        self,
        state: AgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        try:
            return self._tools_node.invoke(state, config=config)
        except AgentRunError:
            raise
        except Exception:
            closers = _build_closers(
                state["messages"],
                content=_SAFE_TOOL_EXECUTION_FAILED_MESSAGE,
            )
            closed = tuple(state["messages"]) + closers
            raise AgentExecutionError(
                closed, _SAFE_EXECUTION_ERROR_MESSAGE
            ) from None

    def _increment_tool_rounds(self, state: AgentState) -> dict[str, int]:
        return {"tool_rounds": state["tool_rounds"] + 1}

    def _route_after_model(self, state: AgentState) -> str:
        messages = state["messages"]
        if not messages:
            return END
        last = messages[-1]
        if not isinstance(last, AIMessage):
            return END
        if not last.tool_calls:
            return END
        if state.get("tool_rounds", 0) >= self._max_tool_rounds:
            return "close_unexecuted_tools"
        return "tools"

    def _close_unexecuted_tools(self, state: AgentState) -> dict[str, Any]:
        closers = _build_closers(
            state["messages"],
            content=_SAFE_TOOL_ROUND_LIMIT_MESSAGE,
        )
        return {
            "messages": list(closers),
            "terminal_error": "tool_round_limit",
        }
