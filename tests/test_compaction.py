"""V0.2 上下文压缩测试：全部使用假摘要模型离线验证。"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from my_agent.compaction import (
    PRUNED_MIDDLE_MARKER,
    SUMMARY_OPEN_TAG,
    TRUNCATION_NOTICE,
    SummarizingCompactor,
    estimate_message_tokens,
)
from my_agent.conversation import Conversation
from my_agent.models import AgentResult

SYSTEM = "系统提示词"


class FakeSummarizer:
    """记录调用并返回受控摘要的假摘要模型"""

    def __init__(self, responses: list[str] | str | None = None) -> None:
        self.responses = responses
        self.calls: list[list[object]] = []

    def invoke(self, messages: list[object], *args: object, **kwargs: object) -> object:
        del args, kwargs
        self.calls.append(messages)
        if isinstance(self.responses, BaseException):
            raise self.responses
        if self.responses is None:
            return AIMessage(content="摘要")
        if isinstance(self.responses, list):
            if not self.responses:
                return AIMessage(content="摘要")
            return AIMessage(content=self.responses.pop(0))
        return AIMessage(content=self.responses)


class RecordingAgent:
    def invoke(
        self,
        messages: tuple[object, ...],
        *,
        run_context: object,
    ) -> AgentResult:
        del run_context
        result_messages = tuple(messages) + (AIMessage(content="收到"),)
        return AgentResult(
            reply="收到",
            messages=result_messages,  # type: ignore[arg-type]
            tool_rounds=0,
        )


def _turn(text: str, index: int) -> list[object]:
    return [HumanMessage(content=f"{text}{index}"), AIMessage(content=f"回复{index}")]


def _history(turn_texts: list[str]) -> list[object]:
    messages: list[object] = [SystemMessage(content=SYSTEM)]
    for index, text in enumerate(turn_texts):
        messages.extend(_turn(text, index))
    return messages


def _summary_content(result: tuple[object, ...]) -> str | None:
    for message in result:
        if isinstance(message, SystemMessage):
            text = str(message.content)
            if text.startswith(SUMMARY_OPEN_TAG):
                return text
    return None


def test_estimate_prefers_usage_metadata_over_char_estimate() -> None:
    messages = [
        SystemMessage(content="ab"),
        AIMessage(
            content="回复",
            usage_metadata={
                "input_tokens": 500,
                "output_tokens": 20,
                "total_tokens": 520,
            },
        ),
    ]
    # usage 路径 520 + 尾部字符估算；字符估算整体只有个位数
    assert estimate_message_tokens(messages) == 520


def test_no_compaction_under_target_returns_messages_unchanged() -> None:
    summarizer = FakeSummarizer()
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=100_000,
        hard_limit_tokens=200_000,
        keep_recent_turns=3,
    )
    messages = tuple(_history(["一", "二"]))
    assert compactor.compact(messages) == messages
    assert summarizer.calls == []


def test_pruning_avoids_summarizer_and_marks_middle() -> None:
    summarizer = FakeSummarizer()
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=3000,
        hard_limit_tokens=6000,
        keep_recent_turns=1,
        tool_result_char_limit=4000,
    )
    huge = "x" * 8000
    messages = (
        SystemMessage(content=SYSTEM),
        HumanMessage(content="a" * 1000),
        ToolMessage(content=huge, tool_call_id="call-1", name="read"),
        AIMessage(content="done"),
        HumanMessage(content="下一个问题"),
    )

    result = compactor.compact(messages)

    tool_results = [message for message in result if isinstance(message, ToolMessage)]
    assert len(tool_results) == 1
    content = str(tool_results[0].content)
    assert PRUNED_MIDDLE_MARKER in content
    assert content.startswith("x" * 100)
    assert len(content) < len(huge)
    assert summarizer.calls == []


def test_compaction_replaces_evicted_turns_with_summary_block() -> None:
    summarizer = FakeSummarizer(responses="旧的三个轮次讨论了A、B、C")
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=600,
        hard_limit_tokens=5000,
        keep_recent_turns=1,
    )
    body = "字" * 1200
    messages = tuple(
        _history([body, body, body, body])  # type: ignore[list-item]
    )

    result = compactor.compact(messages)

    summary_block = _summary_content(result)
    assert summary_block is not None
    assert "旧的三个轮次讨论了A、B、C" in summary_block
    # 只保留最近一个轮次的原文
    humans = [m for m in result if isinstance(m, HumanMessage)]
    assert len(humans) == 1
    assert humans[0].content == "字" * 1200 + "3"
    # 摘要输入包含被淘汰的完整轮次
    assert summarizer.calls and len(summarizer.calls) == 1


def test_incremental_summary_folds_previous_summary() -> None:
    summarizer = FakeSummarizer(responses=["第一份摘要", "第二份摘要"])
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=600,
        hard_limit_tokens=5000,
        keep_recent_turns=1,
    )
    body = "字" * 1200
    messages = tuple(_history([body, body, body]))  # type: ignore[list-item]

    first = compactor.compact(messages)
    assert _summary_content(first) is not None

    # 在压缩结果上追加新一轮并再次触发压缩
    second_input = first + (HumanMessage(content="字" * 1200 + "-new"),)
    second = compactor.compact(second_input)

    prompt_text = str(summarizer.calls[-1][-1].content)  # type: ignore[union-attr]
    assert "第一份摘要" in prompt_text
    assert "第二份摘要" in str(_summary_content(second))


def test_summary_must_shrink_then_hard_truncate() -> None:
    huge_summary = "长" * 5000
    summarizer = FakeSummarizer(responses=[huge_summary, huge_summary])
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=600,
        hard_limit_tokens=100_000,
        keep_recent_turns=1,
    )
    body = "字" * 1200
    messages = tuple(_history([body, body, body]))  # type: ignore[list-item]

    result = compactor.compact(messages)

    assert _summary_content(result) is None
    notices = [
        m
        for m in result
        if isinstance(m, SystemMessage) and m.content == TRUNCATION_NOTICE
    ]
    assert len(notices) == 1


def test_summarizer_exception_falls_back_to_hard_truncation() -> None:
    summarizer = FakeSummarizer(responses=RuntimeError("boom"))
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=600,
        hard_limit_tokens=100_000,
        keep_recent_turns=1,
    )
    body = "字" * 1200
    messages = tuple(_history([body, body, body]))  # type: ignore[list-item]

    result = compactor.compact(messages)

    assert _summary_content(result) is None
    assert any(
        isinstance(m, SystemMessage) and m.content == TRUNCATION_NOTICE for m in result
    )


def test_over_hard_limit_skips_summarizer() -> None:
    summarizer = FakeSummarizer()
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=100,
        hard_limit_tokens=300,
        keep_recent_turns=1,
    )
    body = "字" * 1200
    messages = tuple(_history([body, body, body]))  # type: ignore[list-item]

    result = compactor.compact(messages)

    assert summarizer.calls == []
    assert _summary_content(result) is None
    humans = [m for m in result if isinstance(m, HumanMessage)]
    assert len(humans) == 1


def test_consecutive_failures_disable_summarizer_for_process() -> None:
    summarizer = FakeSummarizer(responses=RuntimeError("boom"))
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=600,
        hard_limit_tokens=100_000,
        keep_recent_turns=1,
    )
    body = "字" * 1200
    messages = tuple(_history([body, body, body]))  # type: ignore[list-item]

    for _ in range(3):
        compactor.compact(messages)
    assert len(summarizer.calls) == 3

    result = compactor.compact(messages)
    assert len(summarizer.calls) == 3
    assert _summary_content(result) is None


def test_conversation_with_compactor_preserves_protocol() -> None:
    summarizer = FakeSummarizer(responses="历史轮次摘要")
    compactor = SummarizingCompactor(
        summarizer,  # type: ignore[arg-type]
        target_tokens=600,
        hard_limit_tokens=5000,
        keep_recent_turns=1,
    )
    conversation = Conversation(
        "conversation-1",
        RecordingAgent(),
        SYSTEM,
        context_compactor=compactor,  # type: ignore[arg-type]
    )

    body = "字" * 1200
    for index in range(3):
        result = conversation.send(f"{body}{index}")

    assert result.ok
    assert conversation.messages[0].content == SYSTEM
    # 摘要块与完整轮次共存，协议校验由 Conversation 内部完成
    assert _summary_content(conversation.messages) is not None
    assert isinstance(conversation.messages[-1], AIMessage)


def test_invalid_thresholds_rejected() -> None:
    with pytest.raises(ValueError, match="hard_limit_tokens"):
        SummarizingCompactor(
            FakeSummarizer(),  # type: ignore[arg-type]
            target_tokens=1000,
            hard_limit_tokens=999,
            keep_recent_turns=1,
        )
    with pytest.raises(ValueError, match="keep_recent_turns"):
        SummarizingCompactor(
            FakeSummarizer(),  # type: ignore[arg-type]
            target_tokens=1000,
            hard_limit_tokens=1000,
            keep_recent_turns=0,
        )
