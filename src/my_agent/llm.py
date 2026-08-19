import math
from numbers import Real

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from my_agent.config import AgentConfig
from my_agent.models import ModelConfigurationError


def _require_non_empty_string(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _require_temperature(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ModelConfigurationError(
            "temperature must be a finite number between 0 and 2"
        )
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ModelConfigurationError(
            "temperature must be a finite number between 0 and 2"
        ) from None
    if not math.isfinite(normalized) or not 0 <= normalized <= 2:
        raise ModelConfigurationError(
            "temperature must be a finite number between 0 and 2"
        )
    return normalized


def _require_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelConfigurationError(
            "request_timeout_seconds must be a positive integer"
        )
    return value


def create_chat_model(
    config: AgentConfig,
    *,
    model_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    effective_model_id = _require_non_empty_string(
        "model_id", config.model_id if model_id is None else model_id
    )
    effective_base_url = _require_non_empty_string(
        "base_url", config.base_url if base_url is None else base_url
    )
    effective_api_key = _require_non_empty_string(
        "api_key", config.api_key if api_key is None else api_key
    )
    effective_temperature = _require_temperature(
        config.temperature if temperature is None else temperature
    )
    request_timeout_seconds = _require_timeout(config.request_timeout_seconds)

    try:
        return ChatOpenAI(
            api_key=effective_api_key,
            model=effective_model_id,
            base_url=effective_base_url,
            temperature=effective_temperature,
            timeout=request_timeout_seconds,
        )
    except Exception:
        raise ModelConfigurationError(
            "failed to initialize chat model; check model configuration"
        ) from None
