import os
from pathlib import Path

import pytest

from my_agent.config import (
    PROJECT_ROOT,
    UNPRINTABLE_URL,
    AgentConfig,
    ConfigurationError,
    _read_defaulted_required,
    _read_optional,
    _read_required,
    load_config,
    safe_url_summary,
    url_secret_fragments,
)

API_KEY_ENV = "CHAT_MODEL_API_KEY"
MODEL_ID_ENV = "CHAT_MODEL_ID"
BASE_URL_ENV = "CHAT_MODEL_BASE_URL"
WORKSPACE_ROOT_ENV = "WORKSPACE_ROOT"
LOG_LEVEL_ENV = "LOG_LEVEL"
REQUEST_TIMEOUT_ENV = "CHAT_MODEL_REQUEST_TIMEOUT_SECONDS"
MAX_TOOL_ROUNDS_ENV = "MAX_TOOL_ROUNDS"
TEMPERATURE_ENV = "CHAT_MODEL_TEMPERATURE"
THINKING_ENV = "CHAT_MODEL_THINKING"
REASONING_EFFORT_ENV = "CHAT_MODEL_REASONING_EFFORT"

CONFIG_ENVIRONMENT_VARIABLES = (
    API_KEY_ENV,
    MODEL_ID_ENV,
    BASE_URL_ENV,
    WORKSPACE_ROOT_ENV,
    LOG_LEVEL_ENV,
    REQUEST_TIMEOUT_ENV,
    MAX_TOOL_ROUNDS_ENV,
    TEMPERATURE_ENV,
    THINKING_ENV,
    REASONING_EFFORT_ENV,
)


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable_name in CONFIG_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)


def set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "test-secret-must-not-appear")


def set_valid_config_environment(
    monkeypatch: pytest.MonkeyPatch, workspace_root: Path
) -> None:
    monkeypatch.setenv(API_KEY_ENV, "test-api-key")
    monkeypatch.setenv(MODEL_ID_ENV, "test-model")
    monkeypatch.setenv(BASE_URL_ENV, "https://example.invalid/v1")
    monkeypatch.setenv(REQUEST_TIMEOUT_ENV, "30")
    monkeypatch.setenv(MAX_TOOL_ROUNDS_ENV, "7")
    monkeypatch.setenv(TEMPERATURE_ENV, "0.5")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(workspace_root))
    monkeypatch.setenv(LOG_LEVEL_ENV, "debug")


def test_load_config_returns_expected_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_root = PROJECT_ROOT / "tests"
    set_valid_config_environment(monkeypatch, workspace_root)

    config = load_config(env_file=None)

    assert config == AgentConfig(
        api_key="test-api-key",
        model_id="test-model",
        base_url="https://example.invalid/v1",
        request_timeout_seconds=30,
        max_tool_rounds=7,
        temperature=0.5,
        workspace_root=workspace_root.resolve(),
        log_level="DEBUG",
        thinking_enabled=False,
        reasoning_effort="high",
    )
    assert isinstance(config.request_timeout_seconds, int)
    assert isinstance(config.max_tool_rounds, int)
    assert isinstance(config.temperature, float)


def test_load_config_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)

    config = load_config(env_file=None)

    assert config.model_id == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.workspace_root == PROJECT_ROOT
    assert config.log_level == "INFO"
    assert config.request_timeout_seconds == 60
    assert config.max_tool_rounds == 4
    assert config.temperature == 0.0
    assert config.thinking_enabled is False
    assert config.reasoning_effort == "high"


def test_load_config_does_not_implicitly_support_legacy_model_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "legacy-key")
    monkeypatch.setenv("MODEL_ID", "legacy-model")

    with pytest.raises(
        ConfigurationError,
        match="CHAT_MODEL_API_KEY is required",
    ):
        load_config(env_file=None)


def test_load_config_rejects_blank_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "   ")

    with pytest.raises(ConfigurationError, match="CHAT_MODEL_API_KEY is required"):
        load_config(env_file=None)


def test_load_config_rejects_blank_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(MODEL_ID_ENV, "   ")

    with pytest.raises(ConfigurationError, match="CHAT_MODEL_ID is required"):
        load_config(env_file=None)


def test_load_config_rejects_blank_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(BASE_URL_ENV, "")

    with pytest.raises(ConfigurationError, match="CHAT_MODEL_BASE_URL is required"):
        load_config(env_file=None)


@pytest.mark.parametrize(
    ("variable", "value", "range_message"),
    [
        (REQUEST_TIMEOUT_ENV, "abc", "greater than or equal to 1"),
        (REQUEST_TIMEOUT_ENV, "0", "greater than or equal to 1"),
        (REQUEST_TIMEOUT_ENV, "-1", "greater than or equal to 1"),
        (MAX_TOOL_ROUNDS_ENV, "abc", "between 1 and 10"),
        (MAX_TOOL_ROUNDS_ENV, "0", "between 1 and 10"),
        (MAX_TOOL_ROUNDS_ENV, "11", "between 1 and 10"),
        (MAX_TOOL_ROUNDS_ENV, "-1", "between 1 and 10"),
        (TEMPERATURE_ENV, "abc", "between 0 and 2"),
        (TEMPERATURE_ENV, "-0.1", "between 0 and 2"),
        (TEMPERATURE_ENV, "2.1", "between 0 and 2"),
    ],
)
def test_load_config_rejects_invalid_numeric_settings(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    range_message: str,
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigurationError) as exc_info:
        load_config(env_file=None)

    assert variable in str(exc_info.value)
    assert value in str(exc_info.value)
    assert range_message in str(exc_info.value)


def test_load_config_normalizes_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(LOG_LEVEL_ENV, "warning")

    assert load_config(env_file=None).log_level == "WARNING"


def test_load_config_rejects_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(LOG_LEVEL_ENV, "verbose")

    with pytest.raises(ConfigurationError, match=r"LOG_LEVEL must be one of"):
        load_config(env_file=None)


def test_load_config_parses_explicit_thinking_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(THINKING_ENV, "ENABLED")
    monkeypatch.setenv(REASONING_EFFORT_ENV, "MAX")

    config = load_config(env_file=None)

    assert config.thinking_enabled is True
    assert config.reasoning_effort == "max"


def test_load_config_keeps_thinking_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_api_key(monkeypatch)

    config = load_config(env_file=None)

    assert config.thinking_enabled is False
    assert config.reasoning_effort == "high"


@pytest.mark.parametrize("value", ["", "yes", "on", "1", "true", "enable"])
def test_load_config_rejects_invalid_thinking_switch(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(THINKING_ENV, value)

    with pytest.raises(
        ConfigurationError,
        match=r"CHAT_MODEL_THINKING must be one of: enabled, disabled",
    ):
        load_config(env_file=None)


@pytest.mark.parametrize("value", ["", "medium", "none", "xhigh"])
def test_load_config_rejects_invalid_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(REASONING_EFFORT_ENV, value)

    with pytest.raises(
        ConfigurationError,
        match=r"CHAT_MODEL_REASONING_EFFORT must be one of: low, high, max",
    ):
        load_config(env_file=None)


def test_agent_config_validates_thinking_fields_directly() -> None:
    base: dict[str, object] = {
        "api_key": "key",
        "model_id": "model",
        "base_url": "https://example.invalid/v1",
        "request_timeout_seconds": 30,
        "max_tool_rounds": 4,
        "temperature": 0.0,
        "workspace_root": PROJECT_ROOT,
        "log_level": "INFO",
    }

    with pytest.raises(ConfigurationError, match="thinking_enabled must be a boolean"):
        AgentConfig(**base, thinking_enabled=1)  # type: ignore[arg-type]

    with pytest.raises(
        ConfigurationError,
        match="reasoning_effort must be one of: low, high, max",
    ):
        AgentConfig(**base, reasoning_effort="medium")


def test_load_config_rejects_missing_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(tmp_path / "does-not-exist"))

    with pytest.raises(ConfigurationError, match="WORKSPACE_ROOT does not exist"):
        load_config(env_file=None)


def test_load_config_rejects_file_as_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_api_key(monkeypatch)
    workspace_file = tmp_path / "workspace.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(workspace_file))

    with pytest.raises(
        ConfigurationError,
        match="WORKSPACE_ROOT is not a directory",
    ):
        load_config(env_file=None)


def test_load_config_rejects_non_string_workspace_root() -> None:
    """非字符串工作目录必须报出公开的环境变量名而不是内部字段名"""
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(
            env_file=None,
            environ={API_KEY_ENV: "test-api-key", WORKSPACE_ROOT_ENV: 123},  # type: ignore[dict-item]
        )

    assert exc_info.value.field == WORKSPACE_ROOT_ENV
    assert exc_info.value.reason == "must be a string"


def test_safe_summary_excludes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    api_key = "test-secret-must-not-appear"
    monkeypatch.setenv(API_KEY_ENV, api_key)

    summary = load_config(env_file=None).safe_summary()

    assert set(summary) == {
        "model_id",
        "base_url",
        "workspace_root",
        "log_level",
        "request_timeout_seconds",
        "max_tool_rounds",
        "temperature",
        "thinking_enabled",
        "reasoning_effort",
        "api_key_configured",
    }
    assert api_key not in str(summary)
    assert isinstance(summary["workspace_root"], str)
    assert summary["api_key_configured"] is True
    assert summary["request_timeout_seconds"] == 60
    assert summary["max_tool_rounds"] == 4
    assert summary["temperature"] == 0.0


def test_safe_url_summary_drops_userinfo_and_query() -> None:
    summary = safe_url_summary(
        "https://alice:sk-proxy-secret@example.invalid:8443/v1?token=query-secret-value"
    )

    assert summary == "https://example.invalid:8443"
    assert safe_url_summary("not-a-url") == UNPRINTABLE_URL


def test_url_secret_fragments_cover_userinfo_and_query_values() -> None:
    fragments = url_secret_fragments(
        "https://alice:sk-proxy-secret@example.invalid/v1?token=query-secret-value"
    )

    assert "alice" in fragments
    assert "alice:sk-proxy-secret" in fragments
    assert "sk-proxy-secret" in fragments
    assert "query-secret-value" in fragments
    assert "example.invalid" not in fragments


def test_safe_summary_sanitizes_base_url_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, "test-api-key")
    monkeypatch.setenv(
        BASE_URL_ENV,
        "https://alice:sk-proxy-secret@example.invalid/v1?token=query-secret-value",
    )

    summary = load_config(env_file=None).safe_summary()

    assert summary["base_url"] == "https://example.invalid"
    assert "alice" not in str(summary)
    assert "sk-proxy-secret" not in str(summary)
    assert "query-secret-value" not in str(summary)


def test_agent_config_direct_construction_uses_same_validation(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        api_key="  key  ",
        model_id="  model  ",
        base_url="  https://example.invalid/v1  ",
        request_timeout_seconds=30,
        max_tool_rounds=4,
        temperature=0,
        workspace_root=tmp_path,
        log_level="warning",
    )

    assert config.api_key == "key"
    assert config.model_id == "model"
    assert config.base_url == "https://example.invalid/v1"
    assert config.temperature == 0.0
    assert config.log_level == "WARNING"


def test_agent_config_requires_path_workspace_root(tmp_path: Path) -> None:
    values: dict[str, object] = {
        "api_key": "key",
        "model_id": "model",
        "base_url": "https://example.invalid/v1",
        "request_timeout_seconds": 30,
        "max_tool_rounds": 4,
        "temperature": 0.0,
        "workspace_root": str(tmp_path),
        "log_level": "INFO",
    }

    with pytest.raises(ConfigurationError, match="workspace_root must be a path"):
        AgentConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("api_key", ""),
        ("model_id", ""),
        ("base_url", ""),
        ("request_timeout_seconds", 0),
        ("max_tool_rounds", 11),
        ("temperature", float("nan")),
        ("log_level", "verbose"),
    ],
)
def test_agent_config_rejects_invalid_direct_values(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "api_key": "key",
        "model_id": "model",
        "base_url": "https://example.invalid/v1",
        "request_timeout_seconds": 30,
        "max_tool_rounds": 4,
        "temperature": 0.0,
        "workspace_root": tmp_path,
        "log_level": "INFO",
    }
    values[field] = value

    with pytest.raises(ConfigurationError):
        AgentConfig(**values)  # type: ignore[arg-type]


def test_load_config_does_not_mutate_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    env = {
        API_KEY_ENV: "from-file",
        WORKSPACE_ROOT_ENV: str(tmp_path),
        LOG_LEVEL_ENV: "ERROR",
    }

    config = load_config(env_file=None, environ=env)

    assert config.api_key == "from-file"
    assert API_KEY_ENV not in os.environ


def test_load_config_environment_overrides_dotenv(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CHAT_MODEL_API_KEY=dotenv-key\nCHAT_MODEL_ID=dotenv-model\n",
        encoding="utf-8",
    )

    config = load_config(
        env_file=env_file,
        environ={
            API_KEY_ENV: "environment-key",
            WORKSPACE_ROOT_ENV: str(tmp_path),
        },
    )

    assert config.api_key == "environment-key"
    assert config.model_id == "dotenv-model"


def test_configuration_error_exposes_field_and_reason() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        load_config(env_file=None, environ={})

    assert exc_info.value.field == API_KEY_ENV
    assert exc_info.value.reason == "is required"
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize(
    ("environment", "name", "default", "expected"),
    [
        ({}, "KEY", " fallback ", "fallback"),
        ({"KEY": None}, "KEY", " fallback ", "fallback"),
        ({"KEY": "  value  "}, "KEY", "fallback", "value"),
    ],
)
def test_read_optional_uses_and_cleans_default_or_value(
    environment: dict[str, str | None],
    name: str,
    default: str,
    expected: str,
) -> None:
    assert _read_optional(environment, name, default) == expected


@pytest.mark.parametrize("raw_value", [None, "", "   "])
def test_read_required_rejects_missing_and_blank_values(raw_value: str | None) -> None:
    environment = {} if raw_value is None else {"KEY": raw_value}

    with pytest.raises(ConfigurationError, match="KEY is required"):
        _read_required(environment, "KEY")


def test_read_required_returns_cleaned_value() -> None:
    assert _read_required({"KEY": "  value  "}, "KEY") == "value"


@pytest.mark.parametrize("raw_value", [None, "", "   "])
def test_read_defaulted_required_rejects_blank_after_defaulting(
    raw_value: str | None,
) -> None:
    environment = {"KEY": raw_value}

    with pytest.raises(ConfigurationError, match="KEY is required"):
        _read_defaulted_required(environment, "KEY", "   ")


def test_read_defaulted_required_returns_cleaned_value() -> None:
    assert _read_defaulted_required({"KEY": "  value  "}, "KEY", "fallback") == "value"


def test_api_key_type_error_uses_string_validation_message() -> None:
    with pytest.raises(
        ConfigurationError,
        match="api_key must be a non-empty string",
    ):
        AgentConfig(
            api_key=123,  # type: ignore[arg-type]
            model_id="model",
            base_url="https://example.invalid/v1",
            request_timeout_seconds=30,
            max_tool_rounds=4,
            temperature=0.0,
            workspace_root=PROJECT_ROOT,
            log_level="INFO",
        )
