import importlib
import sys

import pytest

from my_agent.config import PROJECT_ROOT, AgentConfig, ConfigurationError
from my_agent.llm import ModelInitializationError, create_chat_model


def make_config(**overrides: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "api_key": "test-api-key",
        "model_id": "test-model",
        "base_url": "https://example.invalid/v1",
        "request_timeout_seconds": 30,
        "max_tool_rounds": 4,
        "temperature": 0.3,
        "workspace_root": PROJECT_ROOT,
        "log_level": "INFO",
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)  # type: ignore[arg-type]


class RecordingChatOpenAI:
    """Fake ChatOpenAI that records constructor kwargs and never touches the network."""

    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        type(self).calls.append(kwargs)
        self.kwargs = kwargs


@pytest.fixture
def fake_chat_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> type[RecordingChatOpenAI]:
    RecordingChatOpenAI.calls = []
    monkeypatch.setattr("my_agent.llm.ChatOpenAI", RecordingChatOpenAI)
    return RecordingChatOpenAI


def test_create_chat_model_passes_config_fields(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    config = make_config()

    create_chat_model(config)

    assert len(fake_chat_openai.calls) == 1
    call = fake_chat_openai.calls[0]
    assert call["api_key"] == "test-api-key"
    assert call["model"] == "test-model"
    assert call["base_url"] == "https://example.invalid/v1"
    assert call["temperature"] == 0.3
    assert call["timeout"] == 30


def test_create_chat_model_defaults_use_config_values(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    config = make_config(temperature=1.0, request_timeout_seconds=60)

    result = create_chat_model(config)

    assert result is not None
    assert fake_chat_openai.calls[0]["temperature"] == 1.0
    assert fake_chat_openai.calls[0]["timeout"] == 60


def test_create_chat_model_supports_parameter_overrides(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    config = make_config()

    create_chat_model(
        config,
        model_id="override-model",
        base_url="https://override.invalid",
        api_key="override-key",
        temperature=1.5,
    )

    call = fake_chat_openai.calls[0]
    assert call["model"] == "override-model"
    assert call["base_url"] == "https://override.invalid"
    assert call["api_key"] == "override-key"
    assert call["temperature"] == 1.5
    # timeout has no override, so it still comes from config.
    assert call["timeout"] == 30


def test_create_chat_model_allows_zero_temperature_override(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    create_chat_model(make_config(temperature=1.0), temperature=0)

    assert fake_chat_openai.calls[0]["temperature"] == 0.0


def test_create_chat_model_uses_thinking_adapter_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class RecordingDeepSeekChatModel:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

    monkeypatch.setattr(
        "my_agent.llm.DeepSeekChatModel",
        RecordingDeepSeekChatModel,
    )

    create_chat_model(
        make_config(thinking_enabled=True, reasoning_effort="max", temperature=0.3)
    )

    assert calls == [
        {
            "api_key": "test-api-key",
            "model": "test-model",
            "base_url": "https://example.invalid/v1",
            "timeout": 30,
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "max",
        }
    ]
    # DeepSeek 思考模式忽略 temperature，适配器必须省略而不是发送无效值。
    assert "temperature" not in calls[0]


def test_create_chat_model_keeps_plain_openai_path_when_thinking_disabled(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    create_chat_model(make_config(thinking_enabled=False, reasoning_effort="max"))

    call = fake_chat_openai.calls[0]
    assert "extra_body" not in call
    assert "reasoning_effort" not in call
    assert call["temperature"] == 0.3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", ""),
        ("api_key", "   "),
        ("model_id", ""),
        ("model_id", "   "),
        ("base_url", ""),
        ("base_url", "   "),
    ],
)
def test_create_chat_model_rejects_blank_overrides(
    fake_chat_openai: type[RecordingChatOpenAI],
    field: str,
    value: str,
) -> None:
    with pytest.raises(ConfigurationError):
        create_chat_model(make_config(), **{field: value})

    assert fake_chat_openai.calls == []


@pytest.mark.parametrize(
    "value",
    [True, False, -0.1, 2.1, "abc", float("nan"), float("inf")],
)
def test_create_chat_model_rejects_invalid_temperature(
    fake_chat_openai: type[RecordingChatOpenAI],
    value: object,
) -> None:
    with pytest.raises(ConfigurationError, match="temperature"):
        create_chat_model(make_config(), temperature=value)  # type: ignore[arg-type]

    assert fake_chat_openai.calls == []


def test_create_chat_model_uses_validated_config_values(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    create_chat_model(
        make_config(
            api_key="config-key",
            model_id="config-model",
            base_url="https://example.invalid/v1",
        )
    )

    assert fake_chat_openai.calls[0] == {
        "api_key": "config-key",
        "model": "config-model",
        "base_url": "https://example.invalid/v1",
        "temperature": 0.3,
        "timeout": 30,
    }


def test_create_chat_model_wraps_initialization_error_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ExplodingChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            raise TypeError("deep secret: api_key=test-api-key bad model")

    monkeypatch.setattr("my_agent.llm.ChatOpenAI", ExplodingChatOpenAI)

    with caplog.at_level("ERROR", logger="my_agent.llm"):
        with pytest.raises(ModelInitializationError) as exc_info:
            create_chat_model(make_config())

    message = str(exc_info.value)
    assert "test-api-key" not in message
    assert "test-api-key" not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "Traceback" in caplog.text
    assert exc_info.value.__cause__ is not None


def test_import_llm_does_not_construct_a_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing the module must not construct a model client."""
    import langchain_openai

    calls = 0

    class CountingChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            nonlocal calls
            calls += 1

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", CountingChatOpenAI)
    original_module = sys.modules.get("my_agent.llm")
    sys.modules.pop("my_agent.llm", None)
    try:
        importlib.import_module("my_agent.llm")
    finally:
        if original_module is None:
            sys.modules.pop("my_agent.llm", None)
        else:
            sys.modules["my_agent.llm"] = original_module

    assert calls == 0
