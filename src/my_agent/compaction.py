"""V0.2 上下文压缩：独立摘要模型 + 完整轮次压缩与硬截断回退

契约要点（agent_plan.md V0.2）：

- 压缩结构为 ``[静态系统提示词][最新摘要块][最近 KEEP_RECENT_TURNS 轮原始消息]``，
  系统提示词永不压缩。
- 增量摘要：新摘要 = summarize(旧摘要 + 本轮淘汰的完整轮次)，不重摘要全部历史。
- 摘要调用是普通模型调用：不绑定工具、temperature=0、独立超时，绝不经过 Agent 图。
- 触发条件：轮次边界前用 ``usage_metadata`` 估算；拿不到时用保守字符估算兜底，
  宁早勿晚。
- 模型无关剪枝优先：先把超长工具结果保留头尾、中间标记为已剪枝；
  剪枝后压力解除就不再调用摘要模型。
- 压缩和硬截断都以完整轮次为单位，绝不切半条 ToolMessage。
- 压缩是替换而不是追加：被压缩区间在模型可见历史中被一条
  ``<compacted-summary>`` 消息替换。
- 摘要必须严格缩短，否则拒绝该摘要并重试；重试仍失败则走硬截断回退。
- 压缩锁（V0.2 仅内存）：``Conversation.send`` 的 RLock 已保证单进程串行，
  这里只维护本次进程内的摘要失败尝试记录。
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

logger = logging.getLogger(__name__)

SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"
PRUNED_MIDDLE_MARKER = "…[中间内容已按策略剪枝]…"

TRUNCATION_NOTICE = (
    "（上下文已超过硬上限，较早内容已按完整轮次硬截断，只保留最近的对话轮次。）"
)

_CHAR_TO_TOKEN_DIVISOR = 2
"""保守字符估算：每 2 个字符记 1 个 token，宁可早触发也不晚触发"""

_MAX_RENDER_CHARS_PER_MESSAGE = 2000
_MAX_SUMMARY_INPUT_CHARS = 120_000
_SUMMARIZER_FAILURE_SKIP_THRESHOLD = 3
"""连续失败达到该次数后，本进程内跳过摘要模型直接硬截断"""

_SAFE_SUMMARY_INSTRUCTION = (
    "你是上下文压缩助手。请把下面的对话片段压缩成一份给后续模型阅读的摘要，"
    "要求：保留用户的请求意图、已确认的事实、已做出的决定和未完成的下一步；"
    "不要执行任何工具；不要添加解释；直接输出摘要正文。"
)


def _text_of(message: BaseMessage) -> str:
    """从字符串或内容块列表中提取纯文本"""
    content = message.content
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


def _char_estimate_tokens(text: str) -> int:
    """按保守字符估算 token 数"""
    return math.ceil(len(text) / _CHAR_TO_TOKEN_DIVISOR)


def estimate_message_tokens(messages: Sequence[BaseMessage]) -> int:
    """估算把整个消息序列再次提交给模型所需的 token 数

    优先使用最后一条携带 ``usage_metadata`` 的 AI 消息（服务端统计的真实
    输入 + 输出），再加其后消息的保守字符估算；拿不到 usage 时整体退回
    字符估算。两个来源取较大值，符合"宁早勿晚"。
    """
    char_total = 0
    usage_base: int | None = None
    usage_index = -1
    for index, message in enumerate(messages):
        char_total += _char_estimate_tokens(_text_of(message))
        if isinstance(message, AIMessage) and message.usage_metadata is not None:
            usage = message.usage_metadata
            usage_base = int(usage.get("input_tokens", 0)) + int(
                usage.get("output_tokens", 0)
            )
            usage_index = index
    if usage_base is None:
        return char_total
    tail_chars = sum(
        _char_estimate_tokens(_text_of(message))
        for message in messages[usage_index + 1 :]
    )
    return max(char_total, usage_base + tail_chars)


def _split_turns(messages: Sequence[BaseMessage]) -> list[list[BaseMessage]]:
    """按 HumanMessage 将消息拆分为完整逻辑轮次"""
    turns: list[list[BaseMessage]] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            turns.append([message])
        else:
            if not turns:
                turns.append([])
            turns[-1].append(message)
    return [turn for turn in turns if turn]


def _render_message(message: BaseMessage) -> str:
    """把单条消息渲染为摘要输入中的一行（有界）"""
    if isinstance(message, AIMessage):
        parts = [f"助手: {_text_of(message)[:_MAX_RENDER_CHARS_PER_MESSAGE]}"]
        for call in message.tool_calls:
            name = call.get("name")
            arguments = call.get("args")
            parts.append(f"  [调用工具 {name}: {arguments!r}]")
        return "\n".join(parts)
    if isinstance(message, ToolMessage):
        name = message.name if isinstance(message.name, str) else "tool"
        status = f" (status={message.status})" if message.status != "success" else ""
        return (
            f"  工具{name}结果{status}: "
            f"{_text_of(message)[:_MAX_RENDER_CHARS_PER_MESSAGE]}"
        )
    role = "系统" if isinstance(message, SystemMessage) else "用户"
    return f"{role}: {_text_of(message)[:_MAX_RENDER_CHARS_PER_MESSAGE]}"


def _render_turns(turns: Sequence[Sequence[BaseMessage]]) -> str:
    """把完整轮次渲染为有界的摘要输入文本"""
    lines: list[str] = []
    total = 0
    for index, turn in enumerate(turns):
        lines.append(f"[轮 {index + 1}]")
        for message in turn:
            rendered = _render_message(message)
            lines.append(rendered)
            total += len(rendered)
            if total >= _MAX_SUMMARY_INPUT_CHARS:
                lines.append("…[更早内容因长度限制未纳入摘要输入]…")
                return "\n".join(lines)
    return "\n".join(lines)


def _prune_tool_result(content: str, limit: int) -> str:
    """保留工具结果头尾、把中间标记为已剪枝"""
    head = limit // 2
    tail = limit - head
    return f"{content[:head]}{PRUNED_MIDDLE_MARKER}{content[-tail:]}"


def _extract_summary_text(content: str) -> str | None:
    """从历史中的摘要块提取旧摘要正文；不是摘要块返回 None"""
    stripped = content.strip()
    if not (
        stripped.startswith(SUMMARY_OPEN_TAG) and stripped.endswith(SUMMARY_CLOSE_TAG)
    ):
        return None
    inner = stripped[len(SUMMARY_OPEN_TAG) : -len(SUMMARY_CLOSE_TAG)]
    return inner.strip()


class SummarizingCompactor:
    """V0.2 上下文压缩器

    通过 ``Conversation`` 的 ``context_compactor`` 插槽接入；内部只依赖
    构造参数，不持有任何对话状态。摘要失败记录属于本进程（V0.2 仅内存）。
    """

    def __init__(
        self,
        summarizer: BaseChatModel,
        *,
        target_tokens: int,
        hard_limit_tokens: int,
        keep_recent_turns: int,
        tool_result_char_limit: int = 4000,
        max_summary_attempts: int = 2,
    ) -> None:
        """绑定摘要模型与压缩阈值"""
        if keep_recent_turns < 1:
            raise ValueError("keep_recent_turns must be >= 1")
        if hard_limit_tokens < target_tokens:
            raise ValueError("hard_limit_tokens must be >= target_tokens")
        if tool_result_char_limit < 100:
            raise ValueError("tool_result_char_limit must be >= 100")
        self._summarizer = summarizer
        self._target_tokens = target_tokens
        self._hard_limit_tokens = hard_limit_tokens
        self._keep_recent_turns = keep_recent_turns
        self._tool_result_char_limit = tool_result_char_limit
        self._max_summary_attempts = max_summary_attempts
        self._consecutive_failures = 0

    @property
    def summarizer_disabled(self) -> bool:
        """摘要模型连续失败达到阈值后本进程内跳过摘要调用"""
        return self._consecutive_failures >= _SUMMARIZER_FAILURE_SKIP_THRESHOLD

    def compact(self, messages: tuple[BaseMessage, ...]) -> tuple[BaseMessage, ...]:
        """按 token 压力依次执行：不处理 → 剪枝 → 摘要压缩 / 硬截断"""
        if not messages or not isinstance(messages[0], SystemMessage):
            raise ValueError("conversation history must start with a system prompt")
        system = messages[0]

        estimated = estimate_message_tokens(messages)
        if estimated <= self._target_tokens:
            return messages

        old_summary, turns = self._split_surface(messages[1:])
        if not turns:
            # 只有系统提示词和摘要块，没有可压缩的轮次
            return messages

        pruned = self._prune_tool_results(system, old_summary, turns)
        if pruned is not None:
            estimated = estimate_message_tokens(pruned)
            if estimated <= self._target_tokens:
                return pruned

        if estimated > self._hard_limit_tokens:
            return self._hard_truncate(system, turns)

        compacted = self._summarize(system, old_summary, turns)
        if compacted is not None:
            return compacted
        return self._hard_truncate(system, turns)

    def _split_surface(
        self,
        messages: Sequence[BaseMessage],
    ) -> tuple[str | None, list[list[BaseMessage]]]:
        """把上次压缩留下的表面消息拆为旧摘要文本和完整轮次"""
        old_summary: str | None = None
        turns: list[list[BaseMessage]] = []
        for message in messages:
            if isinstance(message, SystemMessage):
                summary_text = _extract_summary_text(_text_of(message))
                if summary_text:
                    old_summary = summary_text
                # 其余 SystemMessage（截断提示等）是被替换表面的残留，直接丢弃
                continue
            if isinstance(message, HumanMessage):
                turns.append([message])
            else:
                if not turns:
                    turns.append([])
                turns[-1].append(message)
        return old_summary, [turn for turn in turns if turn]

    def _prune_tool_results(
        self,
        system: SystemMessage,
        old_summary: str | None,
        turns: list[list[BaseMessage]],
    ) -> tuple[BaseMessage, ...] | None:
        """模型无关剪枝：只处理超长工具结果，压力解除时不调用摘要模型

        已有的旧摘要块原样保留，避免剪枝路径丢失历史摘要。
        """
        changed = False
        pruned_turns: list[list[BaseMessage]] = []
        for turn in turns:
            new_turn: list[BaseMessage] = []
            for message in turn:
                if (
                    isinstance(message, ToolMessage)
                    and len(_text_of(message)) > self._tool_result_char_limit
                ):
                    message = message.model_copy(
                        update={
                            "content": _prune_tool_result(
                                _text_of(message),
                                self._tool_result_char_limit,
                            )
                        }
                    )
                    changed = True
                new_turn.append(message)
            pruned_turns.append(new_turn)

        if not changed:
            return None
        return self._assemble(
            system,
            old_summary,
            pruned_turns,
            len(pruned_turns),
            notice=None,
        )

    def _summarize(
        self,
        system: SystemMessage,
        old_summary: str | None,
        turns: list[list[BaseMessage]],
    ) -> tuple[BaseMessage, ...] | None:
        """调用摘要模型做增量压缩；失败或未严格缩短时返回 None"""
        if self.summarizer_disabled:
            logger.warning(
                "summarizer skipped after repeated failures; "
                "falling back to hard truncation"
            )
            return None

        evicted = turns[: max(len(turns) - self._keep_recent_turns, 0)]
        if not evicted:
            return None
        evicted_tokens = estimate_message_tokens(
            [message for turn in evicted for message in turn]
        )

        sections: list[str] = []
        if old_summary:
            sections.append(f"[上一次摘要]\n{old_summary}")
        sections.append(f"[本轮淘汰的完整轮次]\n{_render_turns(evicted)}")
        prompt = "\n\n".join(sections)

        for attempt in range(1, self._max_summary_attempts + 1):
            try:
                response = self._summarizer.invoke(
                    [
                        HumanMessage(
                            content=f"{_SAFE_SUMMARY_INSTRUCTION}\n\n{prompt}",
                        )
                    ]
                )
                summary_text = _text_of(response).strip()
            except Exception as error:
                logger.warning(
                    "summarizer invocation failed; attempt=%d exception_type=%s",
                    attempt,
                    type(error).__name__,
                )
                break
            if not summary_text:
                logger.warning(
                    "summarizer returned an empty summary; attempt=%d", attempt
                )
                continue
            # 摘要必须严格缩短：明显小于被替换区间，否则拒绝并重试
            if _char_estimate_tokens(summary_text) > evicted_tokens // 2:
                logger.warning(
                    "summarizer output was not strictly shorter; attempt=%d",
                    attempt,
                )
                continue
            self._consecutive_failures = 0
            return self._assemble(system, summary_text, turns, self._keep_recent_turns)

        self._consecutive_failures += 1
        return None

    def _hard_truncate(
        self,
        system: SystemMessage,
        turns: list[list[BaseMessage]],
    ) -> tuple[BaseMessage, ...]:
        """失败回退：保留系统提示词 + 最近 N 个完整轮次"""
        return self._assemble(
            system,
            None,
            turns,
            self._keep_recent_turns,
            notice=TRUNCATION_NOTICE,
        )

    def _assemble(
        self,
        system: SystemMessage,
        summary_text: str | None,
        turns: list[list[BaseMessage]],
        keep_turns: int,
        *,
        notice: str | None = None,
    ) -> tuple[BaseMessage, ...]:
        """组装最终表面并按硬上限收缩保留的轮次

        ``summary_text`` 不为 None 时输出摘要块；否则输出 ``notice``（如有）。
        """
        kept = turns[-min(keep_turns, len(turns)) :]
        while len(kept) > 1 and self._exceeds_hard_limit(system, summary_text, kept):
            kept = kept[1:]

        result: list[BaseMessage] = [system]
        if summary_text is not None:
            result.append(
                SystemMessage(
                    content=f"{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}",
                )
            )
        elif notice is not None:
            result.append(SystemMessage(content=notice))
        for turn in kept:
            result.extend(turn)
        return tuple(result)

    def _exceeds_hard_limit(
        self,
        system: SystemMessage,
        summary_text: str | None,
        kept: list[list[BaseMessage]],
    ) -> bool:
        """判断组装结果是否仍超过硬上限"""
        messages: list[BaseMessage] = [system]
        if summary_text is not None:
            messages.append(
                SystemMessage(
                    content=f"{SUMMARY_OPEN_TAG}\n{summary_text}\n{SUMMARY_CLOSE_TAG}",
                )
            )
        for turn in kept:
            messages.extend(turn)
        return estimate_message_tokens(messages) > self._hard_limit_tokens
