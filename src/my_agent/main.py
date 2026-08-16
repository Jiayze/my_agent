import sys

from my_agent.config import load_config


def main() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    print("agent ready")

    for name, value in config.safe_summary().items():
        print(f"{name}: {value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
