"""V0.5 命令执行策略：allow / prompt / forbidden 三档决议

契约要点（agent_plan.md 决策 20）：

- ``decision ∈ {allow | prompt | forbidden}`` 是本次策略的最终结果；
  ``forbidden`` 位于审批和一次性 grant 之上，任何授权都不能绕过。
- 三档对应：只读操作 ``allow``；测试、安装、启动、普通写入 ``prompt``；
  删除、push、Git 历史改写等 ``forbidden``。
- ``forbidden`` 的判定手段只是尽力而为的纵深防线：字符串/前缀匹配必然
  能被变形绕过，真正的边界是 OS 后端；无法可靠解析的复合命令一律降级为
  ``prompt``，绝不默认 ``allow``。
- 当前部署的 allow 集合为空：每一条命令要么需要逐次人工批准（prompt），
  要么被直接拒绝（forbidden）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COMMAND_CHARS = 8192

# 复合命令分隔符：命令可能包含多个子命令，策略必须作用于整条命令
_COMPOUND_SPLIT = re.compile(r"&&|\|\||&|\||;|\n|\r")

# 逐 token 切分：空白与常见 shell 元字符都视为边界
_TOKEN_SPLIT = re.compile(r"[\s<>()^\"']+")

# 禁止类别的裸命令名/模式（大小写不敏感，逐词匹配）
_FORBIDDEN_WORDS: frozenset[str] = frozenset(
    {
        # 文件/目录删除（cmd 与 PowerShell 两种词形）
        "del",
        "erase",
        "rd",
        "rmdir",
        "rm",
        "remove-item",
        "ri",
        # Git push / 历史改写 / 工作区清理
        "push",
        "reset",
        "revert",
        "rebase",
        "clean",
        "filter-branch",
        "replace",
        # 系统配置与系统状态修改
        "reg",
        "regedit",
        "setx",
        "schtasks",
        "sc",
        "net",
        "netsh",
        "bcdedit",
        "diskpart",
        "format",
        "shutdown",
        "logoff",
        "restart-computer",
        "stop-computer",
        "set-service",
        "set-itemproperty",
        "new-service",
        "wmic",
        "cipher",
        "vssadmin",
        "wevtutil",
        "cacls",
        "icacls",
        "takeown",
        "attrib",
        "mklink",
        "subst",
        "taskkill",
        "tskill",
    }
)


@dataclass(frozen=True)
class PolicyDecision:
    """一次命令分类的最终决议

    ``decision`` 只取 ``allow`` / ``prompt`` / ``forbidden``；
    ``category`` 与 ``reason`` 面向用户审批提示与审计，不含任何路径。
    """

    decision: str
    category: str
    reason: str


def _split_segments(command: str) -> list[str]:
    """把复合命令拆为候选段；无法可靠解析时保留整条命令一段"""
    segments = [
        segment for segment in _COMPOUND_SPLIT.split(command) if segment.strip()
    ]
    return segments if segments else [command]


def _tokens(segment: str) -> list[str]:
    """切分单个命令段为小写 token 序列"""
    return [token for token in _TOKEN_SPLIT.split(segment.lower()) if token]


def _git_forbidden_subcommands(tokens: list[str]) -> str | None:
    """识别 ``git <forbidden-subcommand>``；未识别返回 None

    跳过 git 的全局选项（含取值选项）后再检查子命令；遇到第一个无法
    识别的 token 即停止，不做更深的猜测。
    """
    _VALUE_OPTIONS = frozenset({"-c", "--git-dir", "--work-tree", "-C", "--namespace"})
    for index, token in enumerate(tokens):
        if token != "git":
            continue
        offset = index + 1
        while offset < len(tokens):
            candidate = tokens[offset]
            if candidate in _VALUE_OPTIONS:
                offset += 2  # 选项本身 + 它的取值
                continue
            if candidate.startswith("-"):
                offset += 1
                continue
            if candidate in (
                "push",
                "reset",
                "revert",
                "rebase",
                "clean",
                "filter-branch",
                "replace",
            ):
                return candidate
            return None
    return None


def evaluate_command(command: str) -> PolicyDecision:
    """对一条命令行做策略分类

    解析顺序：基础校验 → 逐段逐 token 扫描禁止词与 ``git`` 禁止子命令 →
    默认 ``prompt``。allow 集合当前为空；任何无法解析的形态都落在
    ``prompt``，由人工审批兜底，绝不自动放行。
    """
    if not isinstance(command, str) or not command.strip():
        return PolicyDecision(
            decision="prompt",
            category="empty",
            reason="命令为空，需要人工确认",
        )
    if len(command) > MAX_COMMAND_CHARS:
        return PolicyDecision(
            decision="prompt",
            category="oversized",
            reason=f"命令超过 {MAX_COMMAND_CHARS} 字符上限，无法可靠解析",
        )

    for segment in _split_segments(command):
        tokens = _tokens(segment)
        git_hit = _git_forbidden_subcommands(tokens)
        if git_hit is not None:
            return PolicyDecision(
                decision="forbidden",
                category="git_history_or_remote",
                reason=f"git {git_hit} 属于 push/历史改写/清理类，永久拒绝",
            )
        for token in tokens:
            if token in _FORBIDDEN_WORDS:
                return PolicyDecision(
                    decision="forbidden",
                    category="forbidden_command",
                    reason=f"{token} 属于删除/系统修改类，永久拒绝",
                )

    return PolicyDecision(
        decision="prompt",
        category="unclassified",
        reason="命令需要逐次人工批准（当前部署没有自动允许的命令）",
    )
