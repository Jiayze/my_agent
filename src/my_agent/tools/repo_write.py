"""V0.4 工作目录内写入工具：write_text_file 与 replace_text

契约要点（agent_plan.md 决策 8 / V0.4 章节）：

- 默认 profile 不装配写入工具；本模块只被显式的 coding profile 装配。
- 工具只接受工作区相对路径；目录外写入一律拒绝并返回惰性建议文本。
- 每次写入都经过 SideEffectService：intent → 快照 → executing → 写入
  → done，可按快照回滚；版本守卫要求覆盖前先读过且版本未变。
- 工具元数据保留风险等级（workspace_write）与批准策略。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from my_agent.side_effects.service import SideEffectService, WriteRequest
from my_agent.tools.path_policy import PathPolicyError, resolve_workspace_path


def _json(payload: dict[str, Any]) -> str:
    """以确定性格式序列化工具结果"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_repo_write_tools(service: SideEffectService) -> tuple[BaseTool, ...]:
    """构造绑定到副作用执行服务的两个写入工具

    ``service`` 只能由应用装配层（coding profile）创建并注入；模型无法
    访问服务实例、状态目录或工作区根。
    """

    @tool
    def write_text_file(relative_path: str, content: str) -> str:
        """在工作目录内创建或整体覆盖一个 UTF-8 文本文件。

        relative_path 为工作区相对路径；content 为完整文件内容。
        仅允许写入工作目录内：目录外、敏感路径（.env、密钥、.git、.venv 等）
        与规则文件（AGENTS.md 等）都会被拒绝；目录外会返回一条给用户的
        惰性建议，不会触发任何执行。覆盖已有文件前必须先用 read_text_file
        读过该文件，且文件在此期间未变化，否则返回 FS_STALE_VERSION。
        缺失的父目录会自动创建。每次写入都会先持久化 intent 与快照，
        可由用户通过回滚服务恢复；单次内容上限见配置 WRITE_MAX_BYTES。
        """
        operation = "overwrite" if _exists(service, relative_path) else "create"
        outcome = service.execute_write(
            WriteRequest(
                operation=operation,
                relative_path=relative_path,
                content=content,
            ),
        )
        return _json(
            {
                "ok": outcome.ok,
                "request_id": outcome.request_id,
                "path": outcome.relative_path,
                "bytes_written": outcome.bytes_written,
                "error": outcome.error_code,
                "message": outcome.message,
                "suggestion": outcome.suggestion,
            }
        )

    @tool
    def replace_text(
        relative_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """在工作目录内的一个 UTF-8 文本文件中做精确替换。

        relative_path 为工作区相对路径；old_string 必须与文件内容逐字符
        相同。old_string 在文件中必须唯一；若出现多次，必须显式传入
        replace_all=true 全部替换，否则返回 AMBIGUOUS_MATCH。完全找不到
        时返回 TARGET_NOT_FOUND。覆盖前同样要求先读过该文件且版本未变；
        每次替换都会先持久化 intent 与快照，可由用户通过回滚服务恢复。
        """
        outcome = service.execute_write(
            WriteRequest(
                operation="replace_text",
                relative_path=relative_path,
                old_string=old_string,
                new_string=new_string,
                replace_all=replace_all,
            ),
        )
        return _json(
            {
                "ok": outcome.ok,
                "request_id": outcome.request_id,
                "path": outcome.relative_path,
                "bytes_written": outcome.bytes_written,
                "error": outcome.error_code,
                "message": outcome.message,
                "suggestion": outcome.suggestion,
            }
        )

    for write_tool in (write_text_file, replace_text):
        # 风险等级与批准策略保留在元数据中；目录外升级通道在 V0.5.1 之前不存在
        write_tool.metadata = {
            "effect": "workspace_write",
            "risk": "workspace_write",
            "approval": "direct_within_workspace",
        }

    return (write_text_file, replace_text)


def _exists(service: SideEffectService, relative_path: str) -> bool:
    """判断目标文件当前是否已存在（仅用于区分 create/overwrite）

    只做操作选择；create 的排他创建与 overwrite 的存在性校验由服务在
    锁内权威执行。
    """
    try:
        resolved = resolve_workspace_path(
            service.root,
            relative_path,
            expect="file",
        )
    except (PathPolicyError, OSError):
        return False
    return Path(resolved.absolute).is_file()
