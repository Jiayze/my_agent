"""根据经过校验的应用配置创建聊天模型"""

from __future__ import annotations

import logging
import traceback
from dataclasses import replace

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from my_agent.config import AgentConfig, url_secret_fragments
from my_agent.deepseek_chat import DeepSeekChatModel

logger = logging.getLogger(__name__)


class ModelInitializationError(RuntimeError):
    """配置有效但无法创建模型客户端时抛出的错误"""


def _format_safe_traceback(error: Exception, *, secrets: tuple[str, ...]) -> str:
    """格式化诊断 traceback 并移除已知的配置秘密"""
    formatted = "".join(
        traceback.format_exception(type(error), error, error.__traceback__)
    ).rstrip()
    for secret in secrets:
        if secret:
            formatted = formatted.replace(secret, "[REDACTED]")
    return formatted


def create_chat_model(
    config: AgentConfig,
    *,
    model_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    """创建聊天模型并复用 AgentConfig 校验显式覆盖参数

    非法覆盖参数遵循与直接构造 AgentConfig 相同的配置契约
    客户端库初始化失败会单独包装，使调用方能够区分输入错误和启动依赖错误；
    原始异常仍保留在 __cause__ 中
    """
    overrides: dict[str, object] = {}
    if model_id is not None:
        overrides["model_id"] = model_id
    if base_url is not None:
        overrides["base_url"] = base_url
    if api_key is not None:
        overrides["api_key"] = api_key
    if temperature is not None:
        overrides["temperature"] = temperature

    effective = replace(config, **overrides) if overrides else config

    client_kwargs: dict[str, object] = {
        "api_key": effective.api_key,
        "model": effective.model_id,
        "base_url": effective.base_url,
        "timeout": effective.request_timeout_seconds,
    }

    try:
        if effective.thinking_enabled:
            # DeepSeek 思考模式不支持 temperature；省略该参数而不是传一个无效值。
            # thinking 必须放在 extra_body，reasoning_effort 才是官方顶层参数。
            return DeepSeekChatModel(
                **client_kwargs,
                extra_body={"thinking": {"type": "enabled"}},
                reasoning_effort=effective.reasoning_effort,
            )
        # 未显式打开思考模式时保持原有 OpenAI 兼容路径与 temperature 语义。
        return ChatOpenAI(**client_kwargs, temperature=effective.temperature)
    except Exception as error:
        logger.error(
            "chat model initialization failed\n%s",
            _format_safe_traceback(
                error,
                secrets=(effective.api_key, *url_secret_fragments(effective.base_url)),
            ),
        )
        raise ModelInitializationError(
            "failed to initialize chat model; check model configuration"
        ) from error
