"""V0.5 命令执行策略：allow / prompt / forbidden 三档决议

契约要点（agent_plan.md 决策 20）：

- ``decision ∈ {allow | prompt | forbidden}`` 是本次策略的最终结果；
  ``forbidden`` 位于审批和一次性 grant 之上，任何授权都不能绕过。
- 三档对应：只读操作 ``allow``；测试、安装、启动、普通写入 ``prompt``；
  删除、push、Git 历史改写等 ``forbidden``。
- **forbidden 的判定手段只是尽力而为的纵深防线**（返工 2026-11-01 措辞
  收敛）：字符串匹配**不能保证**操作类别被永久禁止——脱字符归一化能覆盖
  ``d^el`` 这类变形，但变量展开、脚本代码（``python -c "os.remove(...)"``）、
  解释器内嵌等形态无法被字符串分类穷尽。禁止清单的作用是把明显形态挡在
  审批之前；**每条命令的逐次人工审批才是最终边界**，不能把它当作 OS 隔离
  的替代证据。
- 无法可靠解析的复合命令一律降级为 ``prompt``，绝不默认 ``allow``；
  当前部署的 allow 集合为空。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_COMMAND_CHARS = 8192

# 复合命令分隔符：命令可能包含多个子命令，策略必须作用于整条命令
_COMPOUND_SPLIT = re.compile(r"&&|\|\||&|\||;|\n|\r")

# 逐 token 切分：空白与常见 shell 元字符都视为边界
_TOKEN_SPLIT = re.compile(r"[\s<>()^\"',]+")

# 危险词按"命令位置"匹配：裸命令、或紧跟 shell 包装器（cmd /c 等）之后。
# 这避免把参数中的同名子命令误杀（``ruff format``、``npm run clean``、
# ``echo clean``），同时仍覆盖 ``cmd /c format`` 这类嵌套形态。
# 被 & ; | 等分隔符切开的段首天然处于命令位置。
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
        # Git push / 历史改写 / 工作区清理（git <sub> 另有专门扫描）
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

# cmd 的脱字符（^）是转义前缀：``d^el`` 实际执行的就是 ``del``。
# 匹配前把 ^ 全部移除，覆盖最常见的转义变形；更复杂的变形
# （变量展开、脚本代码）依赖人工审批兜底。
_CARET = "^"

# 出现这些 token 后，下一个 token 仍处于"命令位置"
_SHELL_WRAPPERS: frozenset[str] = frozenset(
    {
        "cmd",
        "cmd.exe",
        "start",
        "call",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "/c",
        "/k",
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
    """切分单个命令段为小写 token 序列（脱字符归一化后）"""
    normalized = segment.lower()
    if _CARET in normalized:
        # cmd 中 ^ 转义下一个字符：移除后即实际执行的词形
        normalized = normalized.replace(_CARET, "")
    return [token for token in _TOKEN_SPLIT.split(normalized) if token]


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


def _first_forbidden_token(tokens: list[str]) -> str | None:
    """返回第一个处于命令位置的禁止词；不在命令位置的忽略

    命令位置 = 段首，或紧跟 shell 包装器（cmd /c、start、powershell 等）
    之后。参数位置的同名 token（``ruff format``、``echo clean``）不算。
    """
    command_position = True
    for token in tokens:
        if command_position and token in _FORBIDDEN_WORDS:
            return token
        command_position = token in _SHELL_WRAPPERS
    return None


def evaluate_command(command: str) -> PolicyDecision:
    """对一条命令行做策略分类

    解析顺序：基础校验 → 逐段（脱字符归一化）扫描禁止词与 ``git``
    禁止子命令 → 默认 ``prompt``。allow 集合当前为空；任何无法解析的
    形态都落在 ``prompt``，由人工审批兜底，绝不自动放行。
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
                reason=f"git {git_hit} 属于 push/历史改写/清理类，被策略拒绝",
            )
        forbidden = _first_forbidden_token(tokens)
        if forbidden is not None:
            return PolicyDecision(
                decision="forbidden",
                category="forbidden_command",
                reason=f"{forbidden} 属于删除/系统修改类常见形态，被策略拒绝"
                "（字符串检测尽力而为，人工审批是最终边界）",
            )

    return PolicyDecision(
        decision="prompt",
        category="unclassified",
        reason="命令需要逐次人工批准（当前部署没有自动允许的命令）",
    )
