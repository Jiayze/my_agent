from pathlib import Path

import pytest

from my_agent.config import PROJECT_ROOT, AgentConfig, load_config

API_KEY_ENV = "DEEPSEEK_API_KEY"
MODEL_ID_ENV = "MODEL_ID"
BASE_URL_ENV = "BASE_URL"
WORKSPACE_ROOT_ENV = "WORKSPACE_ROOT"
LOG_LEVEL_ENV = "LOG_LEVEL"
ALLOW_SHELL_ENV = "ALLOW_SHELL"
REQUEST_TIMEOUT_ENV = "REQUEST_TIMEOUT_SECONDS"
MAX_TOOL_ROUNDS_ENV = "MAX_TOOL_ROUNDS"
TEMPERATURE_ENV = "TEMPERATURE"

CONFIG_ENVIRONMENT_VARIABLES = (
    API_KEY_ENV,
    MODEL_ID_ENV,
    BASE_URL_ENV,
    WORKSPACE_ROOT_ENV,
    LOG_LEVEL_ENV,
    ALLOW_SHELL_ENV,
    REQUEST_TIMEOUT_ENV,
    MAX_TOOL_ROUNDS_ENV,
    TEMPERATURE_ENV,
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
    monkeypatch.setenv(ALLOW_SHELL_ENV, "yes")


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
        allow_shell=True,
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
    assert config.allow_shell is False
    assert config.request_timeout_seconds == 60
    assert config.max_tool_rounds == 4
    assert config.temperature == 0.0


def test_load_config_rejects_blank_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_KEY_ENV, "   ")

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY is required"):
        load_config(env_file=None)


def test_load_config_rejects_blank_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(MODEL_ID_ENV, "   ")

    with pytest.raises(ValueError, match="MODEL_ID is required"):
        load_config(env_file=None)


def test_load_config_rejects_blank_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(BASE_URL_ENV, "")

    with pytest.raises(ValueError, match="BASE_URL is required"):
        load_config(env_file=None)


@pytest.mark.parametrize(
    ("variable", "value", "range_message"),
    [
        (REQUEST_TIMEOUT_ENV, "abc", "greater than 0"),
        (REQUEST_TIMEOUT_ENV, "0", "greater than 0"),
        (REQUEST_TIMEOUT_ENV, "-1", "greater than 0"),
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

    with pytest.raises(ValueError) as exc_info:
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

    with pytest.raises(ValueError, match=r"log_level should be one of.*got: 'verbose'"):
        load_config(env_file=None)


@pytest.mark.parametrize("raw_value", ["true", "1", "yes"])
def test_load_config_accepts_true_shell_values(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(ALLOW_SHELL_ENV, raw_value)

    assert load_config(env_file=None).allow_shell is True


@pytest.mark.parametrize("raw_value", ["false", "0", "no"])
def test_load_config_accepts_false_shell_values(
    monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(ALLOW_SHELL_ENV, raw_value)

    assert load_config(env_file=None).allow_shell is False


def test_load_config_rejects_invalid_shell_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(ALLOW_SHELL_ENV, "sometimes")

    with pytest.raises(
        ValueError, match=r"ALLOW_SHELL must be one of.*got: 'sometimes'"
    ):
        load_config(env_file=None)


def test_load_config_rejects_missing_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_api_key(monkeypatch)
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(tmp_path / "does-not-exist"))

    with pytest.raises(ValueError, match="WORKSPACE_ROOT does not exist"):
        load_config(env_file=None)


def test_load_config_rejects_file_as_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_api_key(monkeypatch)
    workspace_file = tmp_path / "workspace.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv(WORKSPACE_ROOT_ENV, str(workspace_file))

    with pytest.raises(ValueError, match="WORKSPACE_ROOT is not a directory"):
        load_config(env_file=None)


def test_safe_summary_excludes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    api_key = "test-secret-must-not-appear"
    monkeypatch.setenv(API_KEY_ENV, api_key)

    summary = load_config(env_file=None).safe_summary()

    assert set(summary) == {
        "model_id",
        "base_url",
        "workspace_root",
        "log_level",
        "allow_shell",
        "request_timeout_seconds",
        "max_tool_rounds",
        "temperature",
        "api_key_configured",
    }
    assert api_key not in str(summary)
    assert isinstance(summary["workspace_root"], str)
    assert summary["api_key_configured"] is True
    assert summary["request_timeout_seconds"] == 60
    assert summary["max_tool_rounds"] == 4
    assert summary["temperature"] == 0.0
