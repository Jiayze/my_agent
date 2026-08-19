from pathlib import Path

import pytest

from my_agent.config import AgentConfig
from my_agent.llm import create_chat_model
from my_agent.models import ModelConfigurationError


def make_config(**overrides: object) -> AgentConfig:
    defaults: dict[str, object] = {
        "api_key": "test-api-key",
        "model_id": "test-model",
        "base_url": "https://example.invalid/v1",
        "request_timeout_seconds": 30,
        "max_tool_rounds": 4,
        "temperature": 0.3,
        "workspace_root": Path("workspace"),
        "log_level": "INFO",
        "allow_shell": False,
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
def fake_chat_openai(monkeypatch: pytest.MonkeyPatch) -> RecordingChatOpenAI:
    RecordingChatOpenAI.calls = []
    monkeypatch.setattr("my_agent.llm.ChatOpenAI", RecordingChatOpenAI)
    return RecordingChatOpenAI


def test_create_chat_model_passes_config_fields(
    fake_chat_openai: RecordingChatOpenAI,
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
    fake_chat_openai: RecordingChatOpenAI,
) -> None:
    config = make_config(temperature=1.0, request_timeout_seconds=60)

    result = create_chat_model(config)

    assert result is not None
    assert fake_chat_openai.calls[0]["temperature"] == 1.0
    assert fake_chat_openai.calls[0]["timeout"] == 60


def test_create_chat_model_supports_parameter_overrides(
    fake_chat_openai: RecordingChatOpenAI,
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


def test_create_chat_model_blocks_empty_model_id(
    fake_chat_openai: RecordingChatOpenAI,
) -> None:
    config = make_config(model_id="")

    with pytest.raises(ModelConfigurationError) as exc_info:
        create_chat_model(config)

    message = str(exc_info.value)
    assert "MODEL_ID" in message
    assert "test-api-key" not in message
    assert len(fake_chat_openai.calls) == 0


def test_create_chat_model_blocks_empty_base_url(
    fake_chat_openai: RecordingChatOpenAI,
) -> None:
    config = make_config(base_url="")

    with pytest.raises(ModelConfigurationError) as exc_info:
        create_chat_model(config)

    message = str(exc_info.value)
    assert "BASE_URL" in message
    assert "test-api-key" not in message
    assert len(fake_chat_openai.calls) == 0


def test_create_chat_model_wraps_initialization_error_without_leaking_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            raise TypeError("deep secret: api_key=test-api-key bad model")

    monkeypatch.setattr("my_agent.llm.ChatOpenAI", ExplodingChatOpenAI)

    with pytest.raises(ModelConfigurationError) as exc_info:
        create_chat_model(make_config())

    message = str(exc_info.value)
    assert "test-api-key" not in message


def test_import_llm_does_not_construct_a_client() -> None:
    """The module must import cleanly without building a model client."""
    from my_agent import llm

    # The module-level name must hold the class itself, never an instance.
    assert isinstance(llm.ChatOpenAI, type)
