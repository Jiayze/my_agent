import importlib
import sys
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
def test_create_chat_model_rejects_blank_string_values(
    fake_chat_openai: type[RecordingChatOpenAI],
    field: str,
    value: str,
) -> None:
    with pytest.raises(ModelConfigurationError) as exc_info:
        create_chat_model(make_config(**{field: value}))

    assert field in str(exc_info.value)
    assert "test-api-key" not in str(exc_info.value)
    assert fake_chat_openai.calls == []


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
    with pytest.raises(ModelConfigurationError):
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
    with pytest.raises(ModelConfigurationError, match="temperature"):
        create_chat_model(make_config(), temperature=value)  # type: ignore[arg-type]

    assert fake_chat_openai.calls == []


@pytest.mark.parametrize("value", [0, -1, 3.5, True, False])
def test_create_chat_model_rejects_invalid_timeout(
    fake_chat_openai: type[RecordingChatOpenAI],
    value: object,
) -> None:
    with pytest.raises(ModelConfigurationError, match="request_timeout_seconds"):
        create_chat_model(make_config(request_timeout_seconds=value))  # type: ignore[arg-type]

    assert fake_chat_openai.calls == []


def test_create_chat_model_normalizes_string_values(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    create_chat_model(
        make_config(
            api_key="  config-key  ",
            model_id="  config-model  ",
            base_url="  https://example.invalid/v1  ",
        )
    )

    assert fake_chat_openai.calls[0] == {
        "api_key": "config-key",
        "model": "config-model",
        "base_url": "https://example.invalid/v1",
        "temperature": 0.3,
        "timeout": 30,
    }


def test_create_chat_model_blocks_invalid_config_before_client_creation(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    with pytest.raises(ModelConfigurationError, match="api_key"):
        create_chat_model(make_config(api_key=object()))  # type: ignore[arg-type]

    assert fake_chat_openai.calls == []


def test_create_chat_model_blocks_empty_model_id(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    config = make_config(model_id="")

    with pytest.raises(ModelConfigurationError) as exc_info:
        create_chat_model(config)

    message = str(exc_info.value)
    assert "model_id" in message
    assert "test-api-key" not in message
    assert len(fake_chat_openai.calls) == 0


def test_create_chat_model_blocks_empty_base_url(
    fake_chat_openai: type[RecordingChatOpenAI],
) -> None:
    config = make_config(base_url="")

    with pytest.raises(ModelConfigurationError) as exc_info:
        create_chat_model(config)

    message = str(exc_info.value)
    assert "base_url" in message
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
    assert exc_info.value.__cause__ is None


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
