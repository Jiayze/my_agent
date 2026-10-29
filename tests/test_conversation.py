"""M4 Conversation 短期记忆与消息协议测试。"""

from collections.abc import Sequence

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from my_agent.agent import Agent
from my_agent.conversation import (
    Conversation,
    _clone_message_for_invoke,
    _validate_result_prefix,
)
from my_agent.models import AgentResult
from my_agent.tools import add_numbers


class ConversationFakeModel:
    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.bound_tools: tuple[object, ...] = ()

    def bind_tools(self, tools: Sequence[object]) -> "ConversationFakeModel":
        self.bound_tools = tuple(tools)
        return self

    def invoke(
        self,
        messages: list[object],
        *,
        config: object | None = None,
    ) -> object:
        del messages, config
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class RecordingAgent:
    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.calls: list[tuple[tuple[object, ...], object]] = []

    def invoke(
        self,
        messages: tuple[object, ...],
        *,
        run_context: object,
    ) -> AgentResult:
        self.calls.append((messages, run_context))
        if self.fail is not None:
            raise self.fail
        result_messages = tuple(messages) + (AIMessage(content="收到"),)
        return AgentResult(
            reply="收到",
            messages=result_messages,  # type: ignore[arg-type]
            tool_rounds=0,
        )


def test_send_returns_result_and_keeps_system_prompt_once() -> None:
    agent = RecordingAgent()
    conversation = Conversation("conversation-1", agent, "系统提示")

    result = conversation.send("你好")

    assert result.reply == "收到"
    assert len(conversation.messages) == 3
    assert isinstance(conversation.messages[0], SystemMessage)
    assert conversation.messages[0].content == "系统提示"
    assert isinstance(conversation.messages[1], HumanMessage)
    assert conversation.messages[1].content == "你好"


def test_pending_user_message_survives_agent_exception() -> None:
    agent = RecordingAgent(fail=RuntimeError("unexpected"))
    conversation = Conversation("conversation-1", agent, "系统提示")

    with pytest.raises(RuntimeError, match="unexpected"):
        conversation.send("不要丢失我")

    assert isinstance(conversation.messages[-1], HumanMessage)
    assert conversation.messages[-1].content == "不要丢失我"


def test_agent_result_error_code_is_committed_without_fake_success_ai() -> None:
    class FailingResultAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            return AgentResult(
                reply="模型调用失败，请稍后重试。",
                messages=tuple(messages),  # type: ignore[arg-type]
                tool_rounds=0,
                error_code="model_error",
            )

    conversation = Conversation(
        "conversation-1",
        FailingResultAgent(),
        "系统提示",
    )

    result = conversation.send("保留这条意图")

    assert result.error_code == "model_error"
    assert isinstance(conversation.messages[-1], HumanMessage)
    assert not any(
        isinstance(message, AIMessage)
        and message.content == "模型调用失败，请稍后重试。"
        for message in conversation.messages
    )


def test_custom_compactor_must_preserve_system_prompt() -> None:
    class BadCompactor:
        def compact(self, messages: tuple[object, ...]) -> tuple[object, ...]:
            return tuple(messages[1:])

    conversation = Conversation(
        "conversation-1",
        RecordingAgent(),
        "系统提示",
        context_compactor=BadCompactor(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="system prompt"):
        conversation.send("你好")

    assert isinstance(conversation.messages[-1], HumanMessage)


def test_protocol_rejects_orphan_tool_message_from_compactor() -> None:
    class BadCompactor:
        def compact(self, messages: tuple[object, ...]) -> tuple[object, ...]:
            return tuple(messages) + (
                ToolMessage(content="孤儿", tool_call_id="missing"),
            )

    conversation = Conversation(
        "conversation-1",
        RecordingAgent(),
        "系统提示",
        context_compactor=BadCompactor(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="orphan"):
        conversation.send("你好")


def test_conversation_commits_complete_tool_turn() -> None:
    model = ConversationFakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_numbers",
                        "args": {"left": 2, "right": 3},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="结果是 5。"),
        ]
    )
    conversation = Conversation(
        "conversation-1",
        Agent(model, [add_numbers], max_tool_rounds=1),
        "系统提示",
    )

    result = conversation.send("请计算 2 加 3")

    assert result.ok
    assert result.reply == "结果是 5。"
    assert [type(message).__name__ for message in conversation.messages] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]


def test_real_agent_preserves_previous_turn_prefix() -> None:
    model = ConversationFakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "add_numbers",
                        "args": {"left": 2, "right": 3},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="结果是 5。"),
            AIMessage(content="第二轮回答。"),
        ]
    )
    conversation = Conversation(
        "conversation-1",
        Agent(model, [add_numbers], max_tool_rounds=2),
        "系统提示",
    )
    conversation.send("请计算 2 加 3")

    result = conversation.send("再聊一句")

    assert result.ok
    assert [type(message).__name__ for message in conversation.messages] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "HumanMessage",
        "AIMessage",
    ]


def test_repeated_text_cannot_confirm_dropped_current_message() -> None:
    class DroppingAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            human_messages = [
                message for message in messages if isinstance(message, HumanMessage)
            ]
            if len(human_messages) == 1:
                result_messages = tuple(messages) + (AIMessage(content="第一次"),)
            else:
                current_id = human_messages[-1].id
                result_messages = tuple(
                    message
                    for message in messages
                    if getattr(message, "id", None) != current_id
                ) + (AIMessage(content="错误结果"),)
            return AgentResult(
                reply="完成",
                messages=result_messages,  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation("conversation-1", DroppingAgent(), "系统提示")
    conversation.send("重复文本")
    first_user = next(
        message
        for message in conversation.messages
        if isinstance(message, HumanMessage)
    )

    with pytest.raises(ValueError, match="current user message"):
        conversation.send("重复文本")

    assert isinstance(conversation.messages[-1], HumanMessage)
    assert conversation.messages[-1].id != first_user.id


def test_agent_result_must_preserve_original_system_prompt() -> None:
    class ReplacingSystemAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            result_messages = (
                SystemMessage(content="替换后的系统提示"),
                *messages[1:],
                AIMessage(content="错误结果"),
            )
            return AgentResult(
                reply="错误结果",
                messages=result_messages,  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation(
        "conversation-1",
        ReplacingSystemAgent(),
        "原始系统提示",
    )

    with pytest.raises(ValueError, match="original system prompt"):
        conversation.send("你好")


def test_compactor_must_preserve_pending_user_message() -> None:
    class DroppingCompactor:
        def compact(self, messages: tuple[object, ...]) -> tuple[object, ...]:
            return tuple(
                message for message in messages if not isinstance(message, HumanMessage)
            )

    agent = RecordingAgent()
    conversation = Conversation(
        "conversation-1",
        agent,
        "系统提示",
        context_compactor=DroppingCompactor(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="current user message"):
        conversation.send("不能丢失")

    assert agent.calls == []


def test_agent_result_must_preserve_messages_sent_to_model() -> None:
    class DroppingHistoryAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            result_messages = (messages[0], messages[-1], AIMessage(content="错误结果"))
            return AgentResult(
                reply="错误结果",
                messages=result_messages,  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation(
        "conversation-1",
        DroppingHistoryAgent(),
        "系统提示",
    )
    conversation.send("第一轮")

    with pytest.raises(ValueError, match="changed the model input message prefix"):
        conversation.send("第二轮")


def test_agent_receives_cloned_message_objects() -> None:
    agent = RecordingAgent()
    conversation = Conversation("conversation-1", agent, "系统提示")
    conversation.send("第一轮")
    snapshot = conversation.messages

    conversation.send("第二轮")

    sent = agent.calls[-1][0]
    assert sent[0] is not snapshot[0]
    assert sent[1] is not snapshot[1]
    assert [message.content for message in sent[:2]] == [
        message.content for message in snapshot[:2]
    ]


def test_agent_mutating_received_message_cannot_corrupt_history() -> None:
    class MutatingAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            messages[1].content = "被就地改写"  # type: ignore[attr-defined]
            return AgentResult(
                reply="完成",
                messages=tuple(messages) + (AIMessage(content="完成"),),  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation("conversation-1", MutatingAgent(), "系统提示")

    with pytest.raises(ValueError, match="changed the model input message prefix"):
        conversation.send("原始内容")

    assert isinstance(conversation.messages[-1], HumanMessage)
    assert conversation.messages[-1].content == "原始内容"


def test_nested_content_containers_are_deep_cloned() -> None:
    history_message = HumanMessage(
        content=[{"type": "text", "text": "原始内容"}],
        id="history-message",
    )

    class ListContentCompactor:
        def compact(self, messages: tuple[object, ...]) -> tuple[object, ...]:
            return (messages[0], history_message, messages[-1])

    class MutatingAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            messages[1].content[0]["text"] = "被就地改写"  # type: ignore[attr-defined]
            return AgentResult(
                reply="完成",
                messages=tuple(messages) + (AIMessage(content="完成"),),  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation(
        "conversation-1",
        MutatingAgent(),
        "系统提示",
        context_compactor=ListContentCompactor(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="changed the model input message prefix"):
        conversation.send("新消息")

    assert history_message.content == [{"type": "text", "text": "原始内容"}]


def test_reordered_history_is_rejected() -> None:
    class ReorderingAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            if len(messages) < 4:
                reordered = tuple(messages)
            else:
                reordered = (messages[0], messages[2], messages[1], *messages[3:])
            return AgentResult(
                reply="错误结果",
                messages=reordered + (AIMessage(content="错误结果"),),  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation("conversation-1", ReorderingAgent(), "系统提示")
    conversation.send("第一轮")

    with pytest.raises(ValueError, match="changed the model input message prefix"):
        conversation.send("第二轮")

    assert isinstance(conversation.messages[-1], HumanMessage)
    assert conversation.messages[-1].content == "第二轮"


def test_prefix_validator_prefers_changed_over_dropped() -> None:
    system = SystemMessage(content="系统提示", id="system")
    first = HumanMessage(content="第一轮", id="first")
    second = HumanMessage(content="第二轮", id="second")

    with pytest.raises(ValueError, match="changed the model input message prefix"):
        _validate_result_prefix((system, first, second), (system, second))


def test_prefix_validator_reports_dropped_messages() -> None:
    system = SystemMessage(content="系统提示", id="system")
    first = HumanMessage(content="第一轮", id="first")

    with pytest.raises(ValueError, match="dropped messages from the model input"):
        _validate_result_prefix((system, first), (system,))


def test_committed_messages_receive_unique_ids() -> None:
    agent = RecordingAgent()
    conversation = Conversation("conversation-1", agent, "系统提示")

    conversation.send("第一轮")
    conversation.send("第二轮")

    ids = [message.id for message in conversation.messages]
    assert all(isinstance(message_id, str) and message_id for message_id in ids)
    assert len(set(ids)) == len(ids)
    assert all(
        isinstance(message.id, str) and message.id for message in agent.calls[-1][0]
    )


def test_clone_message_detaches_usage_metadata_artifact_and_set() -> None:
    ai_message = AIMessage(
        content="x",
        usage_metadata={
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        },
    )
    cloned_ai = _clone_message_for_invoke(ai_message)
    assert cloned_ai.usage_metadata is not ai_message.usage_metadata
    cloned_ai.usage_metadata["input_tokens"] = 99  # type: ignore[index]
    assert ai_message.usage_metadata["input_tokens"] == 1  # type: ignore[index]

    tool_message = ToolMessage(
        content="r",
        tool_call_id="c1",
        artifact={"rows": [1]},
    )
    cloned_tool = _clone_message_for_invoke(tool_message)
    assert cloned_tool.artifact is not tool_message.artifact
    cloned_tool.artifact["rows"].append(2)  # type: ignore[index]
    assert tool_message.artifact == {"rows": [1]}

    human_message = HumanMessage(
        content="x",
        additional_kwargs={"tags": {"a"}},
    )
    cloned_human = _clone_message_for_invoke(human_message)
    assert (
        cloned_human.additional_kwargs["tags"]
        is not human_message.additional_kwargs["tags"]
    )
    cloned_human.additional_kwargs["tags"].add("b")
    assert human_message.additional_kwargs["tags"] == {"a"}


def test_send_result_messages_do_not_alias_internal_history() -> None:
    conversation = Conversation("conversation-1", RecordingAgent(), "系统提示")

    result = conversation.send("hi")

    result.messages[1].content = "hacked"  # type: ignore[attr-defined]
    assert conversation.messages[1].content == "hi"


def test_messages_property_returns_isolated_snapshot() -> None:
    conversation = Conversation("conversation-1", RecordingAgent(), "系统提示")
    conversation.send("hi")

    snapshot = conversation.messages
    snapshot[1].content = "hacked"  # type: ignore[attr-defined]

    assert conversation.messages[1].content == "hi"


def test_duplicate_message_ids_are_rejected() -> None:
    class DuplicateIdAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            duplicate = AIMessage(content="重复", id=messages[-1].id)  # type: ignore[attr-defined]
            return AgentResult(
                reply="重复",
                messages=tuple(messages) + (duplicate,),  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation("conversation-1", DuplicateIdAgent(), "系统提示")

    with pytest.raises(ValueError, match="duplicate message id"):
        conversation.send("hi")


def test_prefix_validator_covers_metadata_and_artifact_fields() -> None:
    original_ai = AIMessage(
        content="x",
        id="ai-1",
        usage_metadata={
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        },
    )
    changed_ai = AIMessage(
        content="x",
        id="ai-1",
        usage_metadata={
            "input_tokens": 9,
            "output_tokens": 1,
            "total_tokens": 10,
        },
    )
    with pytest.raises(ValueError, match="changed the model input message prefix"):
        _validate_result_prefix((original_ai,), (changed_ai,))

    original_tool = ToolMessage(
        content="r",
        tool_call_id="c1",
        id="tool-1",
        artifact={"rows": [1]},
    )
    changed_tool = ToolMessage(
        content="r",
        tool_call_id="c1",
        id="tool-1",
        artifact={"rows": [2]},
    )
    with pytest.raises(ValueError, match="changed the model input message prefix"):
        _validate_result_prefix((original_tool,), (changed_tool,))


def test_agent_mutating_usage_metadata_cannot_corrupt_history() -> None:
    history_message = AIMessage(
        content="旧回答",
        id="history-ai",
        usage_metadata={
            "input_tokens": 1,
            "output_tokens": 1,
            "total_tokens": 2,
        },
    )

    class HistoryCompactor:
        def compact(self, messages: tuple[object, ...]) -> tuple[object, ...]:
            return (messages[0], history_message, messages[-1])

    class MetadataMutatingAgent(RecordingAgent):
        def invoke(
            self,
            messages: tuple[object, ...],
            *,
            run_context: object,
        ) -> AgentResult:
            del run_context
            messages[1].usage_metadata["input_tokens"] = 999  # type: ignore[index]
            return AgentResult(
                reply="完成",
                messages=tuple(messages) + (AIMessage(content="完成"),),  # type: ignore[arg-type]
                tool_rounds=0,
            )

    conversation = Conversation(
        "conversation-1",
        MetadataMutatingAgent(),
        "系统提示",
        context_compactor=HistoryCompactor(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="changed the model input message prefix"):
        conversation.send("新消息")

    assert history_message.usage_metadata["input_tokens"] == 1  # type: ignore[index]
