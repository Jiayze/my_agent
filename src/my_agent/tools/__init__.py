"""my_agent 公共工具接口"""

from pathlib import Path

from my_agent.config import DEFAULT_WRITE_MAX_BYTES, PROJECT_ROOT
from my_agent.tools.builtin import add_numbers
from my_agent.tools.knowledge import build_knowledge_base_tools
from my_agent.tools.registry import (
    DuplicateToolNameError,
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
)
from my_agent.tools.repo_read import build_repo_read_tools


def get_tools(
    workspace_root: Path | str | None = None,
    *,
    knowledge_base_root: Path | str | None = None,
) -> ToolRegistry:
    """返回默认（read-only）profile 的工具注册表

    只读仓库工具绑定到调用方注入的工作区；缺省值仅用于直接调用和测试，
    CLI 始终传入 ``AgentConfig.workspace_root``。知识库目录未配置时
    不注册检索工具；默认注册表不含任何写入能力。
    """
    root = PROJECT_ROOT if workspace_root is None else workspace_root
    tools = [add_numbers, *build_repo_read_tools(root)]
    if knowledge_base_root is not None:
        tools.extend(build_knowledge_base_tools(knowledge_base_root))
    return ToolRegistry(tools)


def get_coding_tools(
    workspace_root: Path | str,
    *,
    state_dir: Path | str,
    write_max_bytes: int = DEFAULT_WRITE_MAX_BYTES,
    approval_service: object | None = None,
    enable_shell: bool = False,
    shell_timeout_seconds: int = 120,
) -> ToolRegistry:
    """返回 coding profile 的工具注册表（显式装配，默认 Agent 不使用）

    在只读工具之上加入工作目录内写入工具（V0.4），由 SideEffectService
    提供 intent/快照/回滚与版本守卫。读取账本回调把 read_text_file 的
    读取记录到服务中，作为"先读后写"的依据。``state_dir`` 必须位于
    应用状态目录内，不注册为任何 Agent 工具的根目录。

    Shell 工具（V0.5）只在 ``enable_shell=True`` 且提供了
    ``approval_service``（ApprovalService）时装配；两者缺一即不注册，
    不能由配置布尔值或模型消息临时打开。
    """
    # 延迟导入避免 tools <-> side_effects 的包级循环
    from my_agent.side_effects.service import ReadFingerprint, SideEffectService
    from my_agent.tools.repo_write import build_repo_write_tools

    service = SideEffectService(
        workspace_root,
        state_dir,
        write_max_bytes=write_max_bytes,
    )

    def record_read(
        relative_path: str,
        absolute_path: Path,
        size: int,
        mtime_ns: int,
    ) -> None:
        service.record_read(
            relative_path,
            absolute_path,
            ReadFingerprint(size=size, mtime_ns=mtime_ns),
        )

    tools: list[object] = [
        add_numbers,
        *build_repo_read_tools(workspace_root, on_read=record_read),
        *build_repo_write_tools(service),
    ]

    if enable_shell:
        if approval_service is None:
            raise ValueError(
                "enable_shell requires an ApprovalService; refusing to "
                "assemble a shell tool without an approval channel"
            )
        # 延迟导入保持默认装配路径不触碰命令执行模块
        from my_agent.command_exec import CommandExecutionService
        from my_agent.tools.shell_tool import build_shell_tool

        command_service = CommandExecutionService(
            workspace_root,
            state_dir,
            approval=approval_service,
            default_timeout_seconds=shell_timeout_seconds,
        )
        tools.extend(build_shell_tool(command_service))

    return ToolRegistry(tools)


__all__ = [
    "DuplicateToolNameError",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "add_numbers",
    "build_knowledge_base_tools",
    "build_repo_read_tools",
    "get_coding_tools",
    "get_tools",
]
