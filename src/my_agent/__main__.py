"""支持 ``python -m my_agent`` 启动 CLI"""

from my_agent.main import run

if __name__ == "__main__":
    raise SystemExit(run())
