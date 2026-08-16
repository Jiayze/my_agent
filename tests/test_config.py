from pathlib import Path

import pytest

from my_agent.config import PROJECT_ROOT, AgentConfig, load_config

CONFIG_ENVIRONMENT_VARIABLES = (
    "DEEPSEEK_API_KEY",
    "MODEL_ID",
    "BASE_URL",
    "WORKSPACE_ROOT",
    "LOG_LEVEL",
    "ALLOW_SHELL",
)


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable_name in CONFIG_ENVIRONMENT_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)


def set_required_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-must-not-appear")


def test_load_config_returns_expected_values(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_root = PROJECT_ROOT / "tests"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("ALLOW_SHELL", "yes")

    config = load_config(env_file=None)

    assert config == AgentConfig(
        api_key="test-api-key",
        model_id="test-model",
        base_url="https://example.invalid/v1",
        workspace_root=workspace_root.resolve(),
        log_level="DEBUG",
        allow_shell=True,
    )


def test_load_config_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required_environment(monkeypatch)

    config = load_config(env_file=None)

    assert config.model_id == "deepseek-v4-flash"
    assert config.base_url == "https://api.deepseek.com"
    assert config.workspace_root == PROJECT_ROOT
    assert config.log_level == "INFO"
    assert config.allow_shell is False


def test_load_config_rejects_blank_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "   ")

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY is required"):
        load_config(env_file=None)


def test_load_config_rejects_blank_model_id(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("MODEL_ID", "   ")

    with pytest.raises(ValueError, match="MODEL_ID is required"):
        load_config(env_file=None)


def test_load_config_normalizes_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "warning")

    assert load_config(env_file=None).log_level == "WARNING"


def test_load_config_rejects_invalid_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("LOG_LEVEL", "verbose")

    with pytest.raises(ValueError, match="log_level should be one of"):
        load_config(env_file=None)


@pytest.mark.parametrize("raw_value", ["true", "1", "yes"])
def test_load_config_accepts_true_shell_values(
        monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("ALLOW_SHELL", raw_value)

    assert load_config(env_file=None).allow_shell is True


@pytest.mark.parametrize("raw_value", ["false", "0", "no"])
def test_load_config_accepts_false_shell_values(
        monkeypatch: pytest.MonkeyPatch, raw_value: str
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("ALLOW_SHELL", raw_value)

    assert load_config(env_file=None).allow_shell is False


def test_load_config_rejects_invalid_shell_value(
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("ALLOW_SHELL", "sometimes")

    with pytest.raises(ValueError, match="ALLOW_SHELL must be one of"):
        load_config(env_file=None)


def test_load_config_rejects_missing_workspace_root(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_required_environment(monkeypatch)
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "does-not-exist"))

    with pytest.raises(ValueError, match="WORKSPACE_ROOT does not exist"):
        load_config(env_file=None)


def test_load_config_rejects_file_as_workspace_root(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_required_environment(monkeypatch)
    workspace_file = tmp_path / "workspace.txt"
    workspace_file.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_file))

    with pytest.raises(ValueError, match="WORKSPACE_ROOT is not a directory"):
        load_config(env_file=None)


def test_safe_summary_excludes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    api_key = "test-secret-must-not-appear"
    monkeypatch.setenv("DEEPSEEK_API_KEY", api_key)

    summary = load_config(env_file=None).safe_summary()

    assert set(summary) == {
        "model_id",
        "base_url",
        "workspace_root",
        "log_level",
        "allow_shell",
        "api_key_configured",
    }
    assert api_key not in str(summary)
    assert isinstance(summary["workspace_root"], str)
    assert summary["api_key_configured"] is True
