"""V0.5 Shell 工具：模型的结构化调用到命令执行服务的薄适配

契约要点（agent_plan.md 决策 19、V0.5.1 章节）：

- 工具函数内不散落 ``subprocess`` 调用；进程启动、环境清洗、超时、输出
  限制和进程树终止全部由 ``CommandExecutionService`` 承担。
- ``effect=host_command`` 的 ``retryable`` 恒为 ``false``：非幂等副作用，
  已启动但结果未知时重试等于第二次真实副作用。
- 工具描述诚实声明：每条命令需要逐次人工批准；当前部署没有 OS 级沙盒，
  ``cwd`` 只是初始目录、不是安全边界。
"""

from __future__ import annotations

import json

from langchain_core.tools import BaseTool, tool

from my_agent.command_exec import CommandExecutionService


def build_shell_tool(service: CommandExecutionService) -> tuple[BaseTool, ...]:
    """构造绑定到命令执行服务的 run_command 工具

    ``service`` 只能由应用装配层（coding profile + 显式 enable_shell）
    创建并注入；模型无法访问服务实例、审批链或解释器路径。
    """

    @tool
    def run_command(command: str, timeout_seconds: int = 120) -> str:
        """在用户批准后，于工作目录内执行一条 Windows 命令行（cmd.exe）。

        每一条命令都会先由用户在终端逐次批准（[y/N]），未经批准绝不执行；
        批准提示会展示命令全文，人工审批是最终边界。删除文件、
        git push/reset/revert/rebase/clean、修改系统配置等**常见形态**
        会被策略直接拒绝，但该检测是尽力而为的字符串匹配：转义变形
        （如 d^el）已覆盖，而变量展开、脚本代码（如 python -c
        "os.remove(...)"）、解释器内嵌等形态无法被分类穷尽，只会落入
        人工审批——不要尝试任何删除、push 或系统修改，也不要变通绕过。
        命令在工作目录中以当前用户完整权限运行（无 OS 沙盒；工作目录
        只是初始目录，不是安全边界）。命令会等待**整棵进程树**结束后才
        返回（含分离的后代进程）；后台常驻进程会被视为超时并整树终止。
        输出有界捕获并脱敏。timeout_seconds 仅在部署配置允许的范围内
        生效。结果包含 status（succeeded/failed/timed_out/denied/
        unavailable/unknown——终止未确认时结果未知）、exit_code 与脱敏后的
        stdout/stderr。
        命令不可重试：已启动但结果未知时再次执行等于第二次真实副作用。
        """
        outcome = service.run(command, timeout_seconds)
        return json.dumps(
            {
                "status": outcome.status,
                "request_id": outcome.request_id,
                "exit_code": outcome.exit_code,
                "stdout": outcome.stdout,
                "stderr": outcome.stderr,
                "truncated": outcome.truncated,
                "duration_ms": outcome.duration_ms,
                "error": outcome.error_code,
                "message": outcome.message,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    run_command.metadata = {
        "effect": "host_command",
        "risk": "host_command",
        "approval": "required_per_command",
        "retryable": False,
    }
    return (run_command,)
