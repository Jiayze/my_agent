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
        删除文件、git push/reset/revert/rebase/clean、修改系统配置等命令
        被策略永久拒绝，不要尝试或变通。命令在工作目录中以当前用户完整
        权限运行（无 OS 沙盒；工作目录只是初始目录，不是安全边界），输出
        有界捕获并脱敏，超时会被强制终止整棵进程树。timeout_seconds 仅在
        部署配置允许的范围内生效。结果包含 status（succeeded/failed/
        timed_out/denied/unavailable）、exit_code 与脱敏后的 stdout/stderr。
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
