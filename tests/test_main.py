import pytest

from my_agent import main as main_module
from my_agent.config import PROJECT_ROOT


def test_main_prints_safe_summary_for_valid_configuration(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key")
    monkeypatch.setenv("MODEL_ID", "test-model")
    monkeypatch.setenv("BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WORKSPACE_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    monkeypatch.setenv("ALLOW_SHELL", "false")

    exit_code = main_module.main()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "agent ready" in captured.out
    assert "model_id: test-model" in captured.out
    assert "api_key_configured: True" in captured.out
    assert "test-api-key" not in captured.out


def test_main_reports_configuration_errors_to_stderr(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An empty value keeps load_dotenv() from replacing it with a real .env key.
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")

    exit_code = main_module.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "configuration error: DEEPSEEK_API_KEY is required\n"
