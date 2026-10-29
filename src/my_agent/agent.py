"""无状态 Agent 执行核心：LangGraph 状态图与工具调用闭环"""

from __future__ import annotations

import logging
import time
import traceback
from collections.abc import Sequence
from typing import Any

try:
    from openai import OpenAIError
except ImportError:  # pragma: no cover - optional import fallback
    OpenAIError = ()  # type: ignore[assignment,misc]

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool, ToolException
from langgraph.errors import GraphBubbleUp, GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolInvocationError
from langgraph.runtime import Runtime

from my_agent.models import AgentResult, AgentRunContext, ModelProtocolError
from my_agent.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

_SAFE_MODEL_ERROR_MESSAGE = "模型调用失败，请稍后重试。"
_SAFE_EMPTY_MODEL_MESSAGE = "模型返回了空回复，请重试。"
_SAFE_TOOL_ROUND_LIMIT_MESSAGE = "已达到工具调用轮数上限，该工具未执行。"
_SAFE_TOOL_EXECUTION_FAILED_MESSAGE = "工具执行失败，该工具未完成。"
_SAFE_TOOL_INPUT_MESSAGE = "工具参数无效，请根据工具参数 schema 修正后重试。"
_SAFE_RECURSION_MESSAGE = "Agent 执行达到框架安全上限，请稍后重试。"


def _is_expected_model_error(error: Exception) -> bool:
    """判断异常是否属于预期的模型服务或传输失败"""
    expected = (TimeoutError, ConnectionError)
    if isinstance(OpenAIError, type):
        expected += (OpenAIError,)
    return isinstance(error, expected)


def _normalize_content(content: object) -> str:
    """从字符串或内容块列表中提取模型最终文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _format_safe_traceback(error: BaseException) -> str:
    """保留调用栈和异常类型但不记录可能含密钥的异常文本"""
    extracted = traceback.extract_tb(error.__traceback__)
    if not extracted:
        return type(error).__name__
    frames = "\n".join(
        f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
        for frame in extracted
    )
    return f"Traceback (most recent call last):\n{frames}\n{type(error).__name__}"


def _handle_tool_error(error: ToolException) -> str:
    """把明确的工具业务失败映射为安全文本

    日志由唯一的工具调用包装点负责，避免同一异常被重复记录。
    """
    if isinstance(error, ToolInvocationError):
        return _SAFE_TOOL_INPUT_MESSAGE
    return _SAFE_TOOL_EXECUTION_FAILED_MESSAGE


def _require_read_only_tools(tools: Sequence[BaseTool]) -> None:
    """V0.1 只执行显式声明 effect 且为只读的工具"""
    for tool in tools:
        metadata = tool.metadata or {}
        if "effect" not in metadata:
            raise ValueError(
                f"tool {tool.name!r} must declare an explicit 'effect' metadata"
            )
        effect = metadata["effect"]
        if effect != "read_only":
            raise ValueError(
                f"tool {tool.name!r} requires a side-effect execution policy; "
                f"got effect: {effect!r}"
            )


def _build_closers(
    messages: Sequence[BaseMessage],
    *,
    content: str,
) -> tuple[ToolMessage, ...]:
    """为最后一个带 tool-call 的 AI 消息补齐唯一错误结果"""
    if not messages:
        return ()

    last_ai_index = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            last_ai_index = index
            break
    if last_ai_index < 0:
        return ()

    last_ai = messages[last_ai_index]
    assert isinstance(last_ai, AIMessage)
    existing_ids = {
        message.tool_call_id
        for message in messages[last_ai_index + 1 :]
        if isinstance(message, ToolMessage) and message.tool_call_id
    }

    closers: list[ToolMessage] = []
    for call in last_ai.tool_calls:
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in existing_ids:
            continue
        name = call.get("name")
        closers.append(
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                name=name if isinstance(name, str) else None,
                status="error",
            )
        )
        existing_ids.add(call_id)
    return tuple(closers)


class AgentState(MessagesState):
    """单次运行的图状态绝不保存到 Agent 实例上"""

    tool_rounds: int
    terminal_error: str | None


class Agent:
    """无状态 Agent 核心"""

    def __init__(
        self,
        model: BaseChatModel,
        tools: Sequence[BaseTool] | ToolRegistry,
        max_tool_rounds: int,
    ) -> None:
        """初始化无状态 Agent 并绑定模型工具和执行轮数上限"""
        if isinstance(max_tool_rounds, bool) or not isinstance(max_tool_rounds, int):
            raise ValueError("max_tool_rounds must be an integer >= 1")
        if max_tool_rounds < 1:
            raise ValueError("max_tool_rounds must be >= 1")

        registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools)
        self._tools = tuple(registry.get_tools())
        _require_read_only_tools(self._tools)
        self._max_tool_rounds = max_tool_rounds
        self._bound_model = model.bind_tools(self._tools)
        self._tools_node = ToolNode(
            self._tools,
            handle_tool_errors=False,
            wrap_tool_call=self._wrap_tool_call,
        )
        # 每轮依次经过 model tools increment 再回到 model
        # 最后由模型完成回答或由闭合节点结束
        self._recursion_limit = max(25, 3 * max_tool_rounds + 5)
        self._graph = self._build_graph()

    def _build_graph(self) -> Any:
        """构建模型工具执行轮数统计和未执行调用闭合组成的状态图"""
        builder = StateGraph(AgentState, context_schema=AgentRunContext)
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
        builder.add_conditional_edges(
            "tools",
            self._route_after_tools,
            {"increment": "increment_tool_rounds", END: END},
        )
        builder.add_edge("increment_tool_rounds", "model")
        builder.add_edge("close_unexecuted_tools", END)
        return builder.compile()

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        run_context: AgentRunContext | None = None,
    ) -> AgentResult:
        """执行一轮消息序列并返回结果或结构化预期失败"""
        initial: dict[str, Any] = {
            "messages": list(messages),
            "tool_rounds": 0,
            "terminal_error": None,
        }
        config: RunnableConfig = {"recursion_limit": self._recursion_limit}

        latest: dict[str, Any] = initial
        try:
            for snapshot in self._graph.stream(
                initial,
                config,
                context=run_context,
                stream_mode="values",
            ):
                latest = dict(snapshot)
        except GraphRecursionError as error:
            logger.error(
                "agent graph reached recursion limit\n%s",
                _format_safe_traceback(error),
            )
            return self._failure_result(
                latest.get("messages", ()),
                tool_rounds=int(latest.get("tool_rounds", 0)),
                error_code="recursion_limit",
                error_message=_SAFE_RECURSION_MESSAGE,
            )
        except GraphBubbleUp:
            # GraphInterrupt 和 GraphDrained 属于控制流而不是工具错误
            raise
        except Exception as error:
            logger.error(
                "unexpected agent graph execution error; "
                "run_id=%s conversation_id=%s\n%s",
                run_context.run_id if run_context is not None else "unknown",
                run_context.conversation_id if run_context is not None else "unknown",
                _format_safe_traceback(error),
            )
            raise

        final_messages = tuple(latest.get("messages", ()))
        terminal_error = latest.get("terminal_error")
        if isinstance(terminal_error, str):
            error_message = {
                "model_error": _SAFE_MODEL_ERROR_MESSAGE,
                "empty_model_response": _SAFE_EMPTY_MODEL_MESSAGE,
                "tool_round_limit": _SAFE_TOOL_ROUND_LIMIT_MESSAGE,
            }.get(terminal_error, _SAFE_TOOL_EXECUTION_FAILED_MESSAGE)
            return self._failure_result(
                final_messages,
                tool_rounds=int(latest.get("tool_rounds", 0)),
                error_code=terminal_error,
                error_message=error_message,
            )

        if not final_messages or not isinstance(final_messages[-1], AIMessage):
            logger.error("agent graph ended without a final AIMessage")
            raise RuntimeError("agent graph ended without a final AIMessage")

        final = final_messages[-1]
        if final.tool_calls:
            logger.error("agent graph ended with unclosed tool calls")
            raise RuntimeError("agent graph ended with unclosed tool calls")

        reply = _normalize_content(final.content)
        if not reply.strip():
            return self._failure_result(
                final_messages[:-1],
                tool_rounds=int(latest.get("tool_rounds", 0)),
                error_code="empty_model_response",
                error_message=_SAFE_EMPTY_MODEL_MESSAGE,
            )

        return AgentResult(
            reply=reply,
            messages=final_messages,
            tool_rounds=int(latest.get("tool_rounds", 0)),
        )

    @staticmethod
    def _failure_result(
        messages: Sequence[BaseMessage],
        *,
        tool_rounds: int,
        error_code: str,
        error_message: str,
    ) -> AgentResult:
        """构造安全失败结果而不伪造成功的 assistant 消息"""
        normalized = tuple(messages)
        normalized += _build_closers(normalized, content=error_message)
        return AgentResult(
            reply=error_message,
            messages=normalized,
            tool_rounds=tool_rounds,
            error_code=error_code,
        )

    def _model_node(
        self,
        state: AgentState,
        config: RunnableConfig,
        runtime: Runtime[AgentRunContext],
    ) -> dict[str, Any]:
        """调用已绑定工具的模型并将响应写入图状态"""
        current = tuple(state["messages"])
        run_context = runtime.context
        logger.info(
            "model invocation started; run_id=%s conversation_id=%s",
            run_context.run_id if run_context is not None else "unknown",
            run_context.conversation_id if run_context is not None else "unknown",
        )
        started = time.perf_counter()
        try:
            response = self._bound_model.invoke(list(current), config=config)
        except GraphBubbleUp:
            raise
        except Exception as error:
            duration_ms = (time.perf_counter() - started) * 1000
            if not _is_expected_model_error(error):
                logger.error(
                    "unexpected model invocation error; "
                    "run_id=%s conversation_id=%s duration_ms=%.1f\n%s",
                    run_context.run_id if run_context is not None else "unknown",
                    run_context.conversation_id
                    if run_context is not None
                    else "unknown",
                    duration_ms,
                    _format_safe_traceback(error),
                )
                raise
            logger.error(
                "model invocation failed; run_id=%s conversation_id=%s "
                "duration_ms=%.1f\n%s",
                run_context.run_id if run_context is not None else "unknown",
                run_context.conversation_id if run_context is not None else "unknown",
                duration_ms,
                _format_safe_traceback(error),
            )
            return {"terminal_error": "model_error"}

        if not isinstance(response, AIMessage):
            logger.error(
                "model returned unexpected message type: %s",
                type(response).__name__,
            )
            raise ModelProtocolError(
                f"model returned unexpected message type: {type(response).__name__}"
            )

        logger.info(
            "model invocation finished; run_id=%s conversation_id=%s "
            "duration_ms=%.1f tool_calls=%d",
            run_context.run_id if run_context is not None else "unknown",
            run_context.conversation_id if run_context is not None else "unknown",
            (time.perf_counter() - started) * 1000,
            len(response.tool_calls),
        )

        if not response.tool_calls and not _normalize_content(response.content).strip():
            return {"terminal_error": "empty_model_response"}
        return {"messages": [response]}

    def _tools_node_fn(
        self,
        state: AgentState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        """执行当前状态中的工具调用

        每个工具的耗时和结果状态由 ``_wrap_tool_call`` 统一记录。
        """
        return self._tools_node.invoke(state, config=config)

    @staticmethod
    def _wrap_tool_call(request: Any, execute: Any) -> Any:
        """只转换明确的工具业务异常并保留原始调用关联"""
        call = request.tool_call
        tool_name = str(call.get("name", "unknown"))
        tool_call_id = str(call.get("id", "unknown"))
        started = time.perf_counter()
        try:
            result = execute(request)
        except ToolException as error:
            logger.error(
                "tool execution failed; tool=%s tool_call_id=%s duration_ms=%.1f "
                "exception_type=%s\n%s",
                tool_name,
                tool_call_id,
                (time.perf_counter() - started) * 1000,
                type(error).__name__,
                _format_safe_traceback(error),
            )
            return ToolMessage(
                content=_handle_tool_error(error),
                name=tool_name,
                tool_call_id=tool_call_id,
                status="error",
            )
        except Exception as error:
            logger.error(
                "unexpected tool execution error; tool=%s tool_call_id=%s "
                "duration_ms=%.1f exception_type=%s\n%s",
                tool_name,
                tool_call_id,
                (time.perf_counter() - started) * 1000,
                type(error).__name__,
                _format_safe_traceback(error),
            )
            raise

        logger.info(
            "tool execution finished; tool=%s tool_call_id=%s duration_ms=%.1f",
            tool_name,
            tool_call_id,
            (time.perf_counter() - started) * 1000,
        )
        return result

    def _increment_tool_rounds(self, state: AgentState) -> dict[str, int]:
        """将工具调用轮数增加一轮"""
        return {"tool_rounds": int(state["tool_rounds"]) + 1}

    def _route_after_model(self, state: AgentState) -> str:
        """根据模型状态决定结束执行工具或闭合未执行调用"""
        if state.get("terminal_error"):
            return END

        messages = state["messages"]
        if not messages or not isinstance(messages[-1], AIMessage):
            return END
        if not messages[-1].tool_calls:
            return END
        if int(state["tool_rounds"]) >= self._max_tool_rounds:
            return "close_unexecuted_tools"
        return "tools"

    @staticmethod
    def _route_after_tools(state: AgentState) -> str:
        """根据工具节点状态决定结束本轮或继续调用模型"""
        return END if state.get("terminal_error") else "increment"

    @staticmethod
    def _close_unexecuted_tools(state: AgentState) -> dict[str, Any]:
        """为达到轮数上限时尚未执行的工具调用补充错误结果"""
        closers = _build_closers(
            state["messages"],
            content=_SAFE_TOOL_ROUND_LIMIT_MESSAGE,
        )
        return {
            "messages": list(closers),
            "terminal_error": "tool_round_limit",
        }
