"""V0.5 审批决策者链：允许的缝隙，但当前只有人类决策者

契约要点（agent_plan.md 决策 21）：

- 审批决策建模为按顺序尝试的决策者链；每次询问返回 ``allowed-once`` /
  ``rejected`` / ``unavailable`` / ``defer``；链上没有任何决定性答复时
  一律 fail closed。
- 审批配置只有两态：``ask``（交给已组合的决策者）与 ``never``
  （确定性拒绝，不询问任何人）；``never`` 必须在 waterfall 之前判定。
- 服务层必须容纳决策者的任何失败：抛异常的决策者归一为
  ``unavailable``，返回非词汇值的决策者同样归一为 ``unavailable``，
  任何情况下都不得 fail open。
- 当前链只组合人类适配器一个决策者；未来的自动审批以独立决策者插入，
  且 ``decided_by`` 必须能区分人类与自动决策者。
- 每一次 Agent 侧 ``prompt`` 都必须有一次针对该具体操作的显式人类裁决，
  用户的免输码窗口绝不适用于 Agent 发起的请求（决策 20）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

ApprovalVerdict = Literal["allowed_once", "rejected", "unavailable", "defer"]
"""决策者词汇表；返回任何其他值都会被服务归一为 unavailable"""

APPROVAL_MODES = ("ask", "never")


@dataclass(frozen=True)
class ApprovalRequest:
    """一次待审批的操作摘要

    展示给决策者的内容必须是操作本身（命令全文、工作目录、超时），
    不包含任何密钥或内部状态。
    """

    request_id: str
    kind: str  # host_command
    command: str
    cwd: str
    timeout_seconds: int


class ApprovalDecider(Protocol):
    """审批决策者：给出结果或 ``defer`` 委派给下一个"""

    def decide(self, request: ApprovalRequest) -> ApprovalVerdict:
        """返回 allowed_once / rejected / unavailable / defer"""
        ...

    @property
    def decided_by(self) -> str:
        """决策者标识，写入审计"""
        ...


class HumanCliDecider:
    """CLI 人类决策者：在终端逐次展示操作并等待 y/N

    该决策者只用于本地交互式 CLI 适配器。输入流结束（EOF）说明
    "没有人能批准"，返回 ``unavailable`` 而不是 ``rejected``——两者都
    fail closed，但诊断语义不同。Ctrl+C 视为用户拒绝。
    """

    def __init__(self, input_fn, output_fn) -> None:
        """绑定终端输入输出；与 CLI 循环使用同一对函数"""
        self._input_fn = input_fn
        self._output_fn = output_fn

    @property
    def decided_by(self) -> str:
        """审计中的决策者标识"""
        return "human:cli"

    def decide(self, request: ApprovalRequest) -> ApprovalVerdict:
        """展示待审批命令并读取 y/N 裁决"""
        self._output_fn("")
        self._output_fn(f"[审批] Agent 请求执行命令（request_id={request.request_id}）")
        self._output_fn(f"  类型: {request.kind}")
        self._output_fn(f"  工作目录: {request.cwd}")
        self._output_fn(f"  超时: {request.timeout_seconds}s")
        self._output_fn(f"  命令: {request.command}")
        try:
            answer = self._input_fn("批准执行? [y/N] ")
        except EOFError:
            self._output_fn("[审批] 输入流已结束，无人可以批准。")
            return "unavailable"
        except KeyboardInterrupt:
            self._output_fn("")
            self._output_fn("[审批] 已拒绝（用户中断）。")
            return "rejected"
        normalized = (answer or "").strip().lower()
        if normalized in ("y", "yes"):
            return "allowed_once"
        return "rejected"


class NullDecider:
    """链尾占位决策者：永远 ``defer``（仅用于测试链为空的行为）"""

    @property
    def decided_by(self) -> str:
        """审计中的决策者标识"""
        return "null"

    def decide(self, request: ApprovalRequest) -> ApprovalVerdict:
        """永远委派，不给决定性答复"""
        del request
        return "defer"


class ApprovalService:
    """按顺序尝试决策者链的审批服务

    没有终止决策者、决策者抛异常或返回非词汇值时一律 ``unavailable``
    并 fail closed；服务自身永不主动询问人类。
    """

    def __init__(
        self,
        deciders: Sequence[ApprovalDecider],
        *,
        mode: str = "ask",
    ) -> None:
        """组合决策者链并固定 ask/never 两态"""
        if mode not in APPROVAL_MODES:
            raise ValueError(f"mode must be one of: {', '.join(APPROVAL_MODES)}")
        self._deciders = tuple(deciders)
        self._mode = mode

    @property
    def mode(self) -> str:
        """返回当前审批模式（ask / never）"""
        return self._mode

    def adjudicate(
        self,
        request: ApprovalRequest,
    ) -> tuple[ApprovalVerdict, str]:
        """裁决一次请求，返回 ``(verdict, decided_by)``

        ``never`` 在 waterfall 之前判定，使后注册的决策者无法绕过；
        链上没有决定性答复时返回 ``unavailable``（decided_by 为
        ``fail-closed``），绝不 fail open。
        """
        if self._mode == "never":
            return "rejected", "policy:never"
        for decider in self._deciders:
            try:
                verdict = decider.decide(request)
            except Exception as error:
                logger.error(
                    "approval decider raised; decided_by=%s exception_type=%s",
                    getattr(decider, "decided_by", "unknown"),
                    type(error).__name__,
                )
                return (
                    "unavailable",
                    f"decider-error:{getattr(decider, 'decided_by', 'unknown')}",
                )
            if verdict not in ("allowed_once", "rejected", "unavailable", "defer"):
                logger.error(
                    "approval decider returned a non-vocabulary value; decided_by=%s",
                    getattr(decider, "decided_by", "unknown"),
                )
                return (
                    "unavailable",
                    f"decider-error:{getattr(decider, 'decided_by', 'unknown')}",
                )
            if verdict == "defer":
                continue
            return verdict, decider.decided_by
        return "unavailable", "fail-closed"
