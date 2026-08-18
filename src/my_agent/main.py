import sys
from pathlib import Path

from my_agent.config import DEFAULT_ENV_PATH, load_config


def main(env_file: str | Path | None = DEFAULT_ENV_PATH) -> int:
    try:
        config = load_config(env_file=env_file)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    print("agent ready")

    for name, value in config.safe_summary().items():
        print(f"{name}: {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
