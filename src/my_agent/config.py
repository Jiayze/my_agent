import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
TRUE_VALUES = frozenset({"true", "1", "yes"})
FALSE_VALUES = frozenset({"false", "0", "no"})


@dataclass(frozen=True)
class AgentConfig:
    api_key: str
    model_id: str
    base_url: str
    request_timeout_seconds: int
    max_tool_rounds: int
    temperature: float
    workspace_root: Path
    log_level: str
    allow_shell: bool

    def safe_summary(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "base_url": self.base_url,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_tool_rounds": self.max_tool_rounds,
            "temperature": self.temperature,
            "workspace_root": str(self.workspace_root),
            "log_level": self.log_level,
            "allow_shell": self.allow_shell,
            "api_key_configured": bool(self.api_key),
        }


CURRENT_FILE = Path(__file__).resolve()
PROJECT_ROOT = CURRENT_FILE.parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def load_config(env_file: str | Path | None = DEFAULT_ENV_PATH) -> AgentConfig:
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required")

    model_id = os.getenv("MODEL_ID", "deepseek-v4-flash").strip()
    if not model_id:
        raise ValueError("MODEL_ID is required")

    base_url = os.getenv("BASE_URL", "https://api.deepseek.com").strip()
    if not base_url:
        raise ValueError("BASE_URL is required")

    raw_request_timeout = os.getenv("REQUEST_TIMEOUT_SECONDS", "60").strip()
    try:
        request_timeout_seconds = int(raw_request_timeout)
    except ValueError:
        raise ValueError(
            "REQUEST_TIMEOUT_SECONDS must be an integer greater than 0, "
            f"got: {raw_request_timeout!r}"
        ) from None
    if request_timeout_seconds <= 0:
        raise ValueError(
            "REQUEST_TIMEOUT_SECONDS must be greater than 0, "
            f"got: {raw_request_timeout!r}"
        )

    raw_max_tool_rounds = os.getenv("MAX_TOOL_ROUNDS", "4").strip()
    try:
        max_tool_rounds = int(raw_max_tool_rounds)
    except ValueError:
        raise ValueError(
            "MAX_TOOL_ROUNDS must be an integer between 1 and 10, "
            f"got: {raw_max_tool_rounds!r}"
        ) from None
    if not 1 <= max_tool_rounds <= 10:
        raise ValueError(
            f"MAX_TOOL_ROUNDS must be between 1 and 10, got: {raw_max_tool_rounds!r}"
        )

    raw_temperature = os.getenv("TEMPERATURE", "0").strip()
    try:
        temperature = float(raw_temperature)
    except ValueError:
        raise ValueError(
            f"TEMPERATURE must be a number between 0 and 2, got: {raw_temperature!r}"
        ) from None
    if not 0 <= temperature <= 2:
        raise ValueError(
            f"TEMPERATURE must be between 0 and 2, got: {raw_temperature!r}"
        )

    raw_workspace_root = os.getenv("WORKSPACE_ROOT", "").strip()
    if raw_workspace_root:
        workspace_root = Path(raw_workspace_root).expanduser().resolve()
    else:
        workspace_root = PROJECT_ROOT
    if not workspace_root.exists():
        raise ValueError(f"WORKSPACE_ROOT does not exist: {workspace_root}")
    if not workspace_root.is_dir():
        raise ValueError(f"WORKSPACE_ROOT is not a directory: {workspace_root}")

    raw_log_level = os.getenv("LOG_LEVEL", "INFO").strip()
    log_level = raw_log_level.upper()
    if log_level not in VALID_LOG_LEVELS:
        raise ValueError(
            "log_level should be one of "
            "'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL', "
            f"got: {raw_log_level!r}"
        )

    raw_allow_shell = os.getenv("ALLOW_SHELL", "false").strip().lower()
    if raw_allow_shell in TRUE_VALUES:
        allow_shell = True
    elif raw_allow_shell in FALSE_VALUES:
        allow_shell = False
    else:
        raise ValueError(
            "ALLOW_SHELL must be one of: true, false, 1, 0, yes, no, "
            f"got: {raw_allow_shell!r}"
        )

    return AgentConfig(
        api_key=api_key,
        model_id=model_id,
        base_url=base_url,
        request_timeout_seconds=request_timeout_seconds,
        max_tool_rounds=max_tool_rounds,
        temperature=temperature,
        workspace_root=workspace_root,
        log_level=log_level,
        allow_shell=allow_shell,
    )
