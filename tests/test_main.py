"""M6 CLI 组合根与 M7 日志的离线测试。"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator, Sequence

import pytest

from my_agent import main as main_module
from my_agent.config import PROJECT_ROOT, AgentConfig, ConfigurationError
from my_agent.llm import ModelInitializationError
from my_agent.logging_setup import HANDLER_MARKER, configure_logging
from my_agent.models import AgentResult
from my_agent.prompts import SYSTEM_PROMPT
from my_agent.tools import ToolRegistry, add_numbers, get_tools

SECRET = "sk-test-secret-value"
URL_USER = "alice"
URL_PASSWORD = "sk-proxy-secret"
QUERY_TOKEN = "query-secret-value"
CREDENTIALED_BASE_URL = (
    f"https://{URL_USER}:{URL_PASSWORD}@example.invalid/v1?token={QUERY_TOKEN}"
)
URL_CREDENTIALS = (URL_USER, URL_PASSWORD, QUERY_TOKEN)


def make_config(**overrides: object) -> AgentConfig:
    """构造不读取真实 .env 的测试配置"""
    values: dict[str, object] = {
        "api_key": SECRET,
        "model_id": "test-model",
        "base_url": "https://example.invalid/v1",
        "request_timeout_seconds": 60,
        "max_tool_rounds": 4,
        "temperature": 0.0,
        "workspace_root": PROJECT_ROOT,
        "log_level": "INFO",
    }
    values.update(overrides)
    return AgentConfig(**values)  # type: ignore[arg-type]


def ok_result(reply: str) -> AgentResult:
    """构造正常完成的单轮结果"""
    return AgentResult(reply=reply, messages=(), tool_rounds=0)


def failed_result(error_code: str, reply: str) -> AgentResult:
    """构造带安全错误码的预期失败结果"""
    return AgentResult(
        reply=reply,
        messages=(),
        tool_rounds=0,
        error_code=error_code,
    )


class FakeConversation:
    """记录输入并按脚本返回结果或抛出异常的假对话"""

    def __init__(self, results: Sequence[AgentResult | BaseException]) -> None:
        self.conversation_id = "fake-conversation"
        self._results = list(results)
        self.sent: list[str] = []

    def send(self, user_message: str) -> AgentResult:
        self.sent.append(user_message)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ScriptedInput:
    """按脚本返回输入；脚本耗尽后模拟 EOF"""

    def __init__(self, items: Sequence[str | BaseException]) -> None:
        self._items = list(items)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._items:
            raise EOFError
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def reset_my_agent_logging() -> Iterator[None]:
    """每个测试结束后移除本模块安装的 handler，避免跨测试写入已关闭流"""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(logging.WARNING)


def test_run_cli_handles_two_normal_turns_and_exit() -> None:
    conversation = FakeConversation([ok_result("你好"), ok_result("2 + 3 = 5")])
    input_fn = ScriptedInput(["你好", "2+3", "/exit"])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=input_fn,
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == ["你好", "2+3"]
    assert "助手: 你好" in outputs
    assert "助手: 2 + 3 = 5" in outputs
    assert outputs[-1] == "再见。"
    assert len(input_fn.prompts) == 3


def test_run_cli_ignores_empty_input_without_calling_conversation() -> None:
    conversation = FakeConversation([ok_result("收到")])
    input_fn = ScriptedInput(["", "   ", "收到吗", "/exit"])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=input_fn,
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == ["收到吗"]
    assert outputs.count("助手: 收到") == 1


@pytest.mark.parametrize("command", ["/exit", "/quit", "/exit  ", "/quit   "])
def test_run_cli_exits_on_exit_and_quit_commands(command: str) -> None:
    conversation = FakeConversation([])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=ScriptedInput([command]),
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == []
    assert outputs[-1] == "再见。"


def test_run_cli_treats_unknown_slash_command_as_normal_text() -> None:
    conversation = FakeConversation([ok_result("普通文本")])
    input_fn = ScriptedInput(["/unknown", "/exit"])

    exit_code = main_module.run_cli(
        conversation,
        input_fn=input_fn,
        output_fn=lambda _: None,
    )

    assert exit_code == 0
    assert conversation.sent == ["/unknown"]


def test_run_cli_exits_cleanly_on_eof() -> None:
    conversation = FakeConversation([])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=ScriptedInput([]),
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == []
    assert outputs == ["再见。"]


def test_run_cli_exits_cleanly_on_input_keyboard_interrupt() -> None:
    conversation = FakeConversation([])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=ScriptedInput([KeyboardInterrupt()]),
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == []
    assert outputs[-1] == "再见。"


def test_run_cli_continues_after_execution_keyboard_interrupt() -> None:
    conversation = FakeConversation([KeyboardInterrupt(), ok_result("继续")])
    input_fn = ScriptedInput(["第一条", "第二条", "/exit"])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=input_fn,
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == ["第一条", "第二条"]
    assert "已取消本轮。" in outputs
    assert "助手: 继续" in outputs


def test_run_cli_continues_after_expected_turn_failure() -> None:
    conversation = FakeConversation(
        [
            failed_result("model_error", "模型调用失败，请稍后重试。"),
            ok_result("下一轮正常"),
        ]
    )
    input_fn = ScriptedInput(["失败一轮", "再来一轮", "/exit"])
    outputs: list[str] = []

    exit_code = main_module.run_cli(
        conversation,
        input_fn=input_fn,
        output_fn=outputs.append,
    )

    assert exit_code == 0
    assert conversation.sent == ["失败一轮", "再来一轮"]
    assert "错误: 模型调用失败，请稍后重试。" in outputs
    assert "助手: 下一轮正常" in outputs


def test_run_cli_logs_and_propagates_unexpected_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", secret=SECRET)
    conversation = FakeConversation([RuntimeError("unexpected graph failure")])

    with pytest.raises(RuntimeError, match="unexpected graph failure"):
        main_module.run_cli(
            conversation,
            input_fn=ScriptedInput(["触发"]),
            output_fn=lambda _: None,
        )

    captured = capsys.readouterr()
    assert "CLI turn failed unexpectedly" in captured.err
    assert "Traceback" in captured.err
    assert "RuntimeError" in captured.err
    assert SECRET not in captured.err


def test_logging_filter_redacts_secret_from_messages_and_tracebacks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", secret=SECRET)
    logger = logging.getLogger("my_agent.test")

    logger.error("api_key=%s", SECRET)
    try:
        raise RuntimeError(f"boom {SECRET}")
    except RuntimeError:
        logger.exception("turn failed")

    captured = capsys.readouterr()
    assert SECRET not in captured.err
    assert "[REDACTED]" in captured.err
    assert "Traceback" in captured.err


def test_get_tools_registers_add_numbers() -> None:
    registry = get_tools()

    assert isinstance(registry, ToolRegistry)
    assert add_numbers.name in registry


def test_build_conversation_wires_model_tools_agent_and_conversation() -> None:
    config = make_config()
    model = object()
    registry = ToolRegistry([add_numbers])
    agent = object()
    conversation = object()
    calls: dict[str, object] = {}

    def model_factory(received: AgentConfig) -> object:
        calls["model_config"] = received
        return model

    def tools_factory() -> ToolRegistry:
        calls["tools_called"] = True
        return registry

    def agent_factory(**kwargs: object) -> object:
        calls["agent"] = kwargs
        return agent

    def conversation_factory(**kwargs: object) -> object:
        calls["conversation"] = kwargs
        return conversation

    result = main_module.build_conversation(
        config,
        model_factory=model_factory,  # type: ignore[arg-type]
        tools_factory=tools_factory,
        agent_factory=agent_factory,  # type: ignore[arg-type]
        conversation_factory=conversation_factory,  # type: ignore[arg-type]
    )

    assert result is conversation
    assert calls["model_config"] is config
    assert calls["tools_called"] is True
    assert calls["agent"] == {
        "model": model,
        "tools": registry,
        "max_tool_rounds": config.max_tool_rounds,
    }
    assert calls["conversation"] == {
        "conversation_id": "cli-default",
        "agent": agent,
        "system_prompt": SYSTEM_PROMPT,
    }


def test_main_runs_injected_conversation_without_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(main_module, "load_config", lambda **_: make_config())
    conversation = FakeConversation([ok_result("你好")])
    outputs: list[str] = []

    exit_code = main_module.main(
        env_file=None,
        agent_factory=lambda: conversation,
        input_fn=ScriptedInput(["你好", "/exit"]),
        output_fn=outputs.append,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert conversation.sent == ["你好"]
    assert "助手: 你好" in outputs
    assert any("my-agent 已就绪" in line for line in outputs)
    assert SECRET not in captured.out
    assert SECRET not in captured.err
    assert "Traceback" not in captured.out


def test_main_reports_configuration_errors_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Passing env_file=None keeps load_config() from reading the developer's .env file.
    monkeypatch.setenv("CHAT_MODEL_API_KEY", "")

    exit_code = main_module.main(env_file=None)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "configuration error: CHAT_MODEL_API_KEY is required\n"


def test_main_does_not_swallow_unrelated_value_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_load(*args: object, **kwargs: object) -> None:
        raise ValueError("programming error")

    monkeypatch.setattr(main_module, "load_config", fail_to_load)

    with pytest.raises(ValueError, match="programming error"):
        main_module.main(env_file=None)


def test_main_handles_configuration_error_subclass(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_to_load(*args: object, **kwargs: object) -> None:
        raise ConfigurationError("CHAT_MODEL_ID", "is required")

    monkeypatch.setattr(main_module, "load_config", fail_to_load)

    assert main_module.main(env_file=None) == 1
    assert capsys.readouterr().err == "configuration error: CHAT_MODEL_ID is required\n"


def test_main_reports_initialization_failure_safely(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(main_module, "load_config", lambda **_: make_config())

    def fail_to_build() -> None:
        raise ModelInitializationError("failed to initialize chat model")

    exit_code = main_module.main(env_file=None, agent_factory=fail_to_build)

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "初始化失败" in captured.err
    assert SECRET not in captured.err


def test_main_logs_safe_summary_for_valid_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """保留旧 safe_summary 回归意图：合法配置可见摘要且不泄露 key"""
    monkeypatch.setenv("CHAT_MODEL_API_KEY", SECRET)
    monkeypatch.setenv("CHAT_MODEL_ID", "test-model")
    monkeypatch.setenv("CHAT_MODEL_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("WORKSPACE_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    conversation = FakeConversation([])
    outputs: list[str] = []

    exit_code = main_module.main(
        env_file=None,
        agent_factory=lambda: conversation,
        input_fn=ScriptedInput(["/exit"]),
        output_fn=outputs.append,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert any("my-agent 已就绪（模型: test-model）" in line for line in outputs)
    assert "model_id=test-model" in captured.err
    assert "api_key_configured': True" in captured.err
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_main_logs_sanitized_base_url_for_credentialed_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(base_url=CREDENTIALED_BASE_URL, log_level="DEBUG")
    monkeypatch.setattr(main_module, "load_config", lambda **_: config)
    conversation = FakeConversation([])

    exit_code = main_module.main(
        env_file=None,
        agent_factory=lambda: conversation,
        input_fn=ScriptedInput(["/exit"]),
        output_fn=lambda _: None,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "base_url=https://example.invalid" in captured.err
    assert CREDENTIALED_BASE_URL not in captured.err
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in captured.out
        assert fragment not in captured.err


def test_main_redacts_url_credentials_from_turn_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(base_url=CREDENTIALED_BASE_URL)
    monkeypatch.setattr(main_module, "load_config", lambda **_: config)
    conversation = FakeConversation(
        [RuntimeError(f"proxy rejected {CREDENTIALED_BASE_URL} key={SECRET}")]
    )

    with pytest.raises(RuntimeError):
        main_module.main(
            env_file=None,
            agent_factory=lambda: conversation,
            input_fn=ScriptedInput(["触发"]),
            output_fn=lambda _: None,
        )

    captured = capsys.readouterr()
    assert "Traceback" in captured.err
    assert "[REDACTED]" in captured.err
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in captured.out
        assert fragment not in captured.err


def test_run_converts_unexpected_failure_to_fixed_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("CHAT_MODEL_API_KEY", SECRET)
    monkeypatch.setenv("CHAT_MODEL_BASE_URL", CREDENTIALED_BASE_URL)
    excepthook_calls: list[object] = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: excepthook_calls.append(args))

    def fail() -> int:
        raise RuntimeError(f"cannot reach {CREDENTIALED_BASE_URL} with {SECRET}")

    monkeypatch.setattr(main_module, "main", fail)

    exit_code = main_module.run()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "发生未预期错误" in captured.err
    assert excepthook_calls == []
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in captured.out
        assert fragment not in captured.err


def test_run_reports_turn_failure_through_redacting_logging(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(base_url=CREDENTIALED_BASE_URL)
    monkeypatch.setattr(main_module, "load_config", lambda **_: config)
    conversation = FakeConversation(
        [RuntimeError(f"cannot reach {CREDENTIALED_BASE_URL} with {SECRET}")]
    )
    real_main = main_module.main

    def call_main() -> int:
        return real_main(
            env_file=None,
            agent_factory=lambda: conversation,
            input_fn=ScriptedInput(["触发"]),
            output_fn=lambda _: None,
        )

    monkeypatch.setattr(main_module, "main", call_main)

    exit_code = main_module.run()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unhandled CLI failure" in captured.err
    assert "RuntimeError" in captured.err
    assert "发生未预期错误" in captured.err
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in captured.out
        assert fragment not in captured.err


class BrokenResultConversation:
    """返回缺少结果协议的对象，模拟 try 之外的协议错误"""

    conversation_id = "broken-conversation"

    def send(self, user_message: str) -> object:
        return object()


def test_run_covers_protocol_error_outside_turn_try(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        main_module, "load_config", lambda **_: make_config(log_level="INFO")
    )
    conversation = BrokenResultConversation()
    real_main = main_module.main

    def call_main() -> int:
        return real_main(
            env_file=None,
            agent_factory=lambda: conversation,
            input_fn=ScriptedInput(["触发"]),
            output_fn=lambda _: None,
        )

    monkeypatch.setattr(main_module, "main", call_main)

    exit_code = main_module.run()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "AttributeError" in captured.err
    assert "发生未预期错误" in captured.err
    assert SECRET not in captured.out
    assert SECRET not in captured.err


def test_real_entry_point_keeps_credentials_out_of_process_output() -> None:
    """在真实解释器中验证 run() 边界，而不是依赖测试内的异常钩子"""
    script = textwrap.dedent(
        """
        import os

        import my_agent.main as main_module

        base_url = os.environ["CHAT_MODEL_BASE_URL"]
        api_key = os.environ["CHAT_MODEL_API_KEY"]

        def fail() -> int:
            raise RuntimeError(f"boom {base_url} key={api_key}")

        main_module.main = fail
        raise SystemExit(main_module.run())
        """
    )
    environment = {
        **os.environ,
        "CHAT_MODEL_API_KEY": SECRET,
        "CHAT_MODEL_BASE_URL": CREDENTIALED_BASE_URL,
        "PYTHONIOENCODING": "utf-8",
    }

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        cwd=PROJECT_ROOT,
        timeout=120,
    )

    combined = completed.stdout + completed.stderr
    assert completed.returncode == 1
    assert "发生未预期错误" in completed.stderr
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in combined


def _raise_from_known_line() -> int:
    """在固定源码行抛错，供 DEBUG 日志定位断言"""
    raise RuntimeError(f"cannot reach {CREDENTIALED_BASE_URL} with {SECRET}")


def _remove_my_agent_handlers() -> None:
    """移除本模块 handler，模拟配置日志之前的早期边界"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()


def test_run_without_handler_prints_single_redacted_line_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _remove_my_agent_handlers()
    monkeypatch.setenv("CHAT_MODEL_API_KEY", SECRET)
    monkeypatch.setenv("CHAT_MODEL_BASE_URL", CREDENTIALED_BASE_URL)
    monkeypatch.setattr(main_module, "main", _raise_from_known_line)

    exit_code = main_module.run()

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert exit_code == 1
    assert "发生未预期错误" in captured.err
    assert "Traceback" not in combined
    assert 'File "' not in combined
    assert "RuntimeError" in captured.err
    assert sum("unexpected failure:" in line for line in captured.err.splitlines()) == 1
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in combined


def test_run_with_handler_debug_logs_locate_source_and_redact_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(
        "DEBUG",
        secret=SECRET,
        secrets=(SECRET, *URL_CREDENTIALS),
    )
    monkeypatch.setattr(main_module, "main", _raise_from_known_line)

    exit_code = main_module.run()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unhandled CLI failure traceback" in captured.err
    assert "test_main.py" in captured.err
    assert "in _raise_from_known_line" in captured.err
    assert "[REDACTED]" in captured.err
    assert "发生未预期错误" in captured.err
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in captured.out
        assert fragment not in captured.err


def test_run_with_handler_info_keeps_bare_traceback_out_of_terminal(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(
        "INFO",
        secret=SECRET,
        secrets=(SECRET, *URL_CREDENTIALS),
    )
    monkeypatch.setattr(main_module, "main", _raise_from_known_line)

    exit_code = main_module.run()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "unhandled CLI failure" in captured.err
    assert "RuntimeError" in captured.err
    assert "发生未预期错误" in captured.err
    assert "Traceback" not in captured.err
    assert 'File "' not in captured.err
    assert "in _raise_from_known_line" not in captured.err
    for fragment in (SECRET, *URL_CREDENTIALS):
        assert fragment not in captured.out
        assert fragment not in captured.err
