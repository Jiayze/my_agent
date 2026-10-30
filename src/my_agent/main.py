"""命令行入口：配置加载、对象装配与终端交互循环"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel

from my_agent.agent import Agent
from my_agent.compaction import SummarizingCompactor
from my_agent.config import (
    DEFAULT_ENV_PATH,
    AgentConfig,
    ConfigurationError,
    load_config,
    url_secret_fragments,
)
from my_agent.conversation import Conversation
from my_agent.llm import ModelInitializationError, create_chat_model
from my_agent.logging_setup import HANDLER_MARKER, configure_logging
from my_agent.models import AgentResult
from my_agent.prompts import CODING_SYSTEM_PROMPT, SYSTEM_PROMPT
from my_agent.tools import ToolRegistry, get_coding_tools, get_tools

logger = logging.getLogger(__name__)

CONVERSATION_ID = "cli-default"
EXIT_COMMANDS = frozenset({"/exit", "/quit"})

_EXIT_MESSAGE = "再见。"
_CANCEL_MESSAGE = "已取消本轮。"
_SAFE_STARTUP_FAILURE = "初始化失败，请检查模型配置后重试。"
_SAFE_UNEXPECTED_FAILURE = "发生未预期错误，请查看日志后重试。"
_SAFE_TURN_ERROR_PREFIX = "错误: "

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


class ChatConversation(Protocol):
    """CLI 只需要的最小对话契约，便于离线注入假实现"""

    def send(self, user_message: str) -> AgentResult:
        """提交一条用户消息并返回本轮结果"""
        ...


def _default_tools_factory(config: AgentConfig) -> ToolRegistry:
    """默认工具装配：工作目录只读工具 + 可选知识库检索"""
    return get_tools(
        config.workspace_root,
        knowledge_base_root=config.knowledge_base_root,
    )


def _coding_tools_factory(
    config: AgentConfig,
    state_dir: Path | None,
    *,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> ToolRegistry:
    """coding profile 工具装配：只读工具 + 写入 + 可选命令执行（V0.4/V0.5）"""
    from my_agent.approval import ApprovalService, HumanCliDecider
    from my_agent.side_effects.state_dir import default_state_root

    # V0.5 人类决策者：本地交互式 CLI 的逐命令 y/N 审批
    approval = ApprovalService(
        [HumanCliDecider(input_fn, output_fn)],
        mode=config.shell_approval,
    )
    return get_coding_tools(
        config.workspace_root,
        state_dir=state_dir if state_dir is not None else default_state_root(),
        write_max_bytes=config.write_max_bytes,
        approval_service=approval,
        enable_shell=True,
        shell_timeout_seconds=config.shell_timeout_seconds,
    )


def _create_summarizer(config: AgentConfig) -> BaseChatModel:
    """创建 V0.2 独立摘要模型：temperature=0、不绑定工具"""
    return create_chat_model(
        config,
        model_id=config.summarizer_model_id,
        base_url=config.summarizer_base_url,
        api_key=config.summarizer_api_key,
        temperature=0,
    )


def build_conversation(
    config: AgentConfig,
    *,
    profile: str = "default",
    state_dir: Path | None = None,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
    model_factory: Callable[[AgentConfig], BaseChatModel] | None = None,
    summarizer_factory: Callable[[AgentConfig], BaseChatModel] | None = None,
    tools_factory: Callable[[AgentConfig], ToolRegistry] | None = None,
    agent_factory: Callable[..., Agent] | None = None,
    conversation_factory: Callable[..., Conversation] | None = None,
) -> Conversation:
    """按 M6.1 启动顺序装配模型、工具、Agent 与 Conversation

    默认工厂在函数体内解析，测试可以注入假模型和假装配函数而不访问网络。
    V0.2 起对话默认装配 token 压力感知的 ``SummarizingCompactor``，
    摘要模型缺省回落主模型，可以与主模型相同也可以不同。
    ``profile="coding"`` 显式装配工作目录内写入（V0.4）与命令执行（V0.5，
    每条命令逐次人工批准）；默认 profile 不装配任何写入或命令工具。
    """
    if profile == "default":
        default_tools_factory: Callable[[AgentConfig], ToolRegistry] = (
            _default_tools_factory
        )
    elif profile == "coding":

        def default_tools_factory(config: AgentConfig) -> ToolRegistry:
            return _coding_tools_factory(
                config,
                state_dir,
                input_fn=input_fn,
                output_fn=output_fn,
            )

    else:
        raise ValueError(f"unknown profile: {profile!r}")

    if model_factory is None:
        model_factory = create_chat_model
    if summarizer_factory is None:
        summarizer_factory = _create_summarizer
    if tools_factory is None:
        tools_factory = default_tools_factory
    if agent_factory is None:
        agent_factory = Agent
    if conversation_factory is None:
        conversation_factory = Conversation

    model = model_factory(config)
    # 工作区根只从已校验的配置注入；模型无法修改工具绑定的根目录
    tools = tools_factory(config)
    agent = agent_factory(
        model=model,
        tools=tools,
        max_tool_rounds=config.max_tool_rounds,
        max_stall_rounds=config.max_stall_rounds,
    )
    summarizer = summarizer_factory(config)
    compactor = SummarizingCompactor(
        summarizer,
        target_tokens=config.context_target_tokens,
        hard_limit_tokens=config.context_hard_limit_tokens,
        keep_recent_turns=config.keep_recent_turns,
    )
    system_prompt = CODING_SYSTEM_PROMPT if profile == "coding" else SYSTEM_PROMPT
    return conversation_factory(
        conversation_id=CONVERSATION_ID,
        agent=agent,
        system_prompt=system_prompt,
        context_compactor=compactor,
    )


def run_cli(
    conversation: ChatConversation,
    *,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> int:
    """运行最小交互循环并返回进程退出码

    空输入被忽略；``/exit``、``/quit``、EOF 和输入阶段 Ctrl+C 正常退出；
    单轮取消或预期失败只影响本轮，随后继续读取下一条输入。
    """
    while True:
        try:
            raw_input = input_fn("你: ")
        except EOFError:
            output_fn(_EXIT_MESSAGE)
            return 0
        except KeyboardInterrupt:
            output_fn("")
            output_fn(_EXIT_MESSAGE)
            return 0

        user_message = raw_input.strip()
        if not user_message:
            continue
        if user_message in EXIT_COMMANDS:
            output_fn(_EXIT_MESSAGE)
            return 0

        try:
            result = conversation.send(user_message)
        except KeyboardInterrupt:
            output_fn(_CANCEL_MESSAGE)
            continue
        except Exception:
            # 未预期编程/框架错误不吞掉：记录脱敏 traceback 后交给调用方边界
            logger.exception(
                "CLI turn failed unexpectedly; conversation_id=%s",
                getattr(conversation, "conversation_id", "unknown"),
            )
            raise

        if result.ok:
            output_fn(f"助手: {result.reply}")
        else:
            logger.warning(
                "CLI turn returned an expected failure; conversation_id=%s "
                "error_code=%s tool_rounds=%d",
                getattr(conversation, "conversation_id", "unknown"),
                result.error_code,
                result.tool_rounds,
            )
            output_fn(f"{_SAFE_TURN_ERROR_PREFIX}{result.reply}")


def _parse_cli_profile(argv: Sequence[str] | None) -> str:
    """解析 ``--profile`` 启动参数；默认 read-only profile

    profile 是启动时由人显式选择的能力组合，不是模型或配置文件可以
    临时打开的开关。
    """
    parser = argparse.ArgumentParser(prog="my-agent", add_help=False)
    parser.add_argument(
        "--profile",
        choices=("default", "coding"),
        default="default",
        help="default: 只读工具; coding: 读/写文件 + 逐命令人工批准的命令执行",
    )
    known, _unknown = parser.parse_known_args(
        list(argv) if argv is not None else None,
    )
    return known.profile


def main(
    env_file: str | Path | None = DEFAULT_ENV_PATH,
    *,
    argv: Sequence[str] | None = None,
    agent_factory: Callable[[], ChatConversation] | None = None,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> int:
    """加载配置、装配对象并进入 CLI 循环后返回进程退出码"""
    profile = _parse_cli_profile(argv)
    try:
        config = load_config(env_file=env_file)
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    secrets = (config.api_key, *url_secret_fragments(config.base_url))
    configure_logging(config.log_level, secret=config.api_key, secrets=secrets)

    # safe_summary 中的 base_url 已去掉 userinfo/路径/查询，日志里不会回显凭据
    summary = config.safe_summary()
    logger.info(
        "agent starting; model_id=%s base_url=%s",
        summary["model_id"],
        summary["base_url"],
    )
    logger.debug("configuration summary: %s", summary)

    try:
        conversation = (
            build_conversation(
                config,
                profile=profile,
                input_fn=input_fn,
                output_fn=output_fn,
            )
            if agent_factory is None
            else agent_factory()
        )
    except ModelInitializationError as exc:
        logger.error(
            "conversation initialization failed; exception_type=%s",
            type(exc).__name__,
        )
        print(_SAFE_STARTUP_FAILURE, file=sys.stderr)
        return 1

    profile_notice = (
        "" if profile == "default" else "（coding profile：命令执行需逐条批准）"
    )
    output_fn(
        f"my-agent 已就绪（模型: {config.model_id}{profile_notice}）。"
        "输入 /exit 或 /quit 退出。"
    )
    return run_cli(conversation, input_fn=input_fn, output_fn=output_fn)


def run() -> int:
    """真实进程入口：把未预期异常收敛为固定安全提示和退出码 1

    与 :func:`main` 不同，本函数面向 ``python -m my_agent`` 和 console script，
    绝不让原始 traceback 交给默认 excepthook；诊断信息经脱敏后写入日志。
    直接调用 :func:`main` 的编程错误仍然原样传播，便于测试与嵌入方感知。
    """
    try:
        return main()
    except Exception as exc:
        _report_unexpected_failure(exc)
        print(_SAFE_UNEXPECTED_FAILURE, file=sys.stderr)
        return 1


def _report_unexpected_failure(exc: BaseException) -> None:
    """输出脱敏后的诊断信息，绝不向用户终端暴露裸 traceback"""
    if _has_my_agent_handler():
        # 用户可见部分只保留固定提示与类型/简要错误，源码位置仅在 DEBUG 日志中出现
        logger.error(
            "unhandled CLI failure; exception_type=%s message=%s",
            type(exc).__name__,
            exc,
        )
        logger.debug("unhandled CLI failure traceback", exc_info=exc)
        return

    # 启动早期（日志尚未配置）没有过滤器：只输出一行异常类型，无法定位源码行
    print(f"unexpected failure: {type(exc).__name__}", file=sys.stderr)


def _has_my_agent_handler() -> bool:
    """判断 configure_logging 是否已安装带脱敏过滤的 handler"""
    return any(
        getattr(handler, HANDLER_MARKER, False)
        for handler in logging.getLogger().handlers
    )


if __name__ == "__main__":
    raise SystemExit(run())
