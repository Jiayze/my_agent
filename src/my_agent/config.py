import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv
@dataclass(frozen=True)
class AgentConfig:
    api_key: str
    model_id: str
    base_url: str
    workspace_root: Path
    log_level: str
    allow_shell: bool

    def safe_summary(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "base_url": self.base_url,
            "workspace_root": str(self.workspace_root),
            "log_level": self.log_level,
            "allow_shell": self.allow_shell,
            "api_key_configured": bool(self.api_key),
        }


CURRENT_FILE=Path(__file__).resolve()
PROJECT_ROOT = CURRENT_FILE.parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"

def load_config(env_file: str | Path |None = DEFAULT_ENV_PATH) -> AgentConfig:
    if env_file is not None:
        load_dotenv(dotenv_path=env_file)

    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ValueError("DEEPSEEK_API_KEY is required")

    model_id=os.getenv("MODEL_ID","deepseek-v4-flash").strip()
    if not model_id:
        raise ValueError("MODEL_ID is required")

    base_url=os.getenv("BASE_URL","https://api.deepseek.com").strip()


    raw_workspace_root = os.getenv("WORKSPACE_ROOT", "").strip()
    if raw_workspace_root:
        workspace_root = Path(raw_workspace_root).expanduser().resolve()
    else:
        workspace_root = PROJECT_ROOT
    if not workspace_root.exists():
        raise ValueError(
            f"WORKSPACE_ROOT does not exist: {workspace_root}"
        )
    if not workspace_root.is_dir():
        raise ValueError(
            f"WORKSPACE_ROOT is not a directory: {workspace_root}"
        )

    log_level=os.getenv("LOG_LEVEL","INFO").strip().upper()
    if not log_level in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(
            "log_level should be one of 'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'"
        )

    raw_allow_shell = os.getenv("ALLOW_SHELL", "false").strip().lower()
    if raw_allow_shell in {"true","1", "yes"}:
        allow_shell = True
    elif raw_allow_shell in {"false","0", "no"}:
        allow_shell = False
    else:
        raise ValueError(
            "ALLOW_SHELL must be one of: true, false, 1, 0, yes, no"
        )

    return AgentConfig(
        api_key=api_key,
        model_id=model_id,
        base_url=base_url,
        workspace_root=workspace_root,
        log_level=log_level,
        allow_shell=allow_shell,
    )
if __name__ == "__main__":
    config = load_config()
    print(config.safe_summary())