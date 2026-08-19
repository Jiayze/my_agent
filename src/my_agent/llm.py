from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import ChatOpenAI

from my_agent.config import AgentConfig
from my_agent.models import ModelConfigurationError


#这个模块只会创建聊天模型对象。它不处理任何对话循环、命令行交互或工具执行。客户端是在 `create_chat_model` 内部懒加载构建的，所以导入这个模块时不会读取真实环境或打开网络连接。

#从 `config` 创建一个聊天模型,单独的参数可以被覆盖
def create_chat_model(
    config: AgentConfig,
    *,
    model_id: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    effective_model_id = config.model_id if model_id is None else model_id
    effective_base_url = config.base_url if base_url is None else base_url
    effective_api_key = config.api_key if api_key is None else api_key
    effective_temperature = config.temperature if temperature is None else temperature

    if not effective_model_id:
        raise ModelConfigurationError("MODEL_ID must not be empty")
    if not effective_base_url:
        raise ModelConfigurationError("BASE_URL must not be empty")

    try:
        return ChatOpenAI(
            api_key=effective_api_key,
            model=effective_model_id,
            base_url=effective_base_url,
            temperature=effective_temperature,
            timeout=config.request_timeout_seconds,
        )
    except Exception as exc:
        #不要传播原始异常：其文本可能包含API密钥或其他秘密。只保留异常类型名称用于诊断
        raise ModelConfigurationError(
            f"failed to create chat model client: {type(exc).__name__}"
        ) from None
