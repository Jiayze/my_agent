"""V0.5 命令执行服务：Agent 通道的进程控制层

契约要点（agent_plan.md 决策 19/20/21、V0.5.1 章节）：

- Shell plugin 不在工具函数里散落 ``subprocess`` 调用：进程启动、环境
  清洗、超时、输出限制和进程树终止全部收敛在本服务。
- 决议顺序固定：基础校验 → 策略分类（forbidden 直接拒绝，任何授权都
  不能绕过）→ 审批决策者链 → 执行。被拒绝、审批不可用与执行失败映射到
  不同结果状态，绝不压成普通成功。
- 执行前先持久化 intent（prepared），审批通过后切换 executing；
  崩溃后遗留 executing 标记结果未知，禁止自动重试（``retryable`` 恒为
  ``false``）。
- 结果协议状态：``succeeded`` / ``failed`` / ``timed_out`` / ``denied`` /
  ``unavailable`` / ``interrupted`` / ``unknown``。
- 安全边界诚实声明：当前部署没有 OS 级沙盒（受限 token / AppContainer
  POC 是 Shell 全面启用的前置条件，尚未落地）；补偿控制是每条命令的
  显式人工批准 + 禁止清单 + 有界输出 + 进程树超时终止 + 审计。
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from my_agent.approval import ApprovalRequest, ApprovalService
from my_agent.bounded_process import child_environment, run_bounded
from my_agent.exec_policy import MAX_COMMAND_CHARS, evaluate_command
from my_agent.side_effects.intent_store import IntentRecord, IntentStore, utc_now_iso
from my_agent.tools.path_policy import redact_text, truncate_utf8

logger = logging.getLogger(__name__)

RESPONSE_HEAD_BYTES = 32 * 1024
RESPONSE_TAIL_BYTES = 32 * 1024
_TRUNCATION_MARKER = "\n…[输出已截断，仅保留头尾]…\n"


class CommandInterpreterError(RuntimeError):
    """无法定位固定的 cmd.exe 解释器（fail closed，不执行任何命令）"""


@dataclass(frozen=True)
class CommandOutcome:
    """一次命令请求的最终结果

    ``status`` 使用结果协议词汇；``stdout``/``stderr`` 已脱敏并按
    头尾截断。``decision_reason`` 仅在 denied 时携带策略/审批原因。
    """

    status: str  # succeeded|failed|timed_out|denied|unavailable|interrupted|unknown
    request_id: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: float | None = None
    error_code: str | None = None
    message: str = ""
    decision_reason: str | None = None


def resolve_cmd_interpreter() -> Path:
    """启动时一次解析并固定 cmd.exe 绝对路径

    候选顺序：``COMSPEC`` → ``SystemRoot\\System32\\cmd.exe``；找不到时
    抛出异常（组合根会把 Shell 工具整体关闭），绝不回退到 PATH 解析。
    """
    candidates: list[Path] = []
    comspec = os.environ.get("COMSPEC")
    if comspec:
        candidates.append(Path(comspec))
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if system_root:
        candidates.append(Path(system_root) / "System32" / "cmd.exe")
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_absolute() and resolved.is_file():
            return resolved
    raise CommandInterpreterError("could not locate a trusted cmd.exe interpreter")


class CommandExecutionService:
    """Agent 通道的命令执行服务

    ``workspace_root`` / ``state_dir`` 只能由应用装配层注入。每次执行
    都完整走：intent → 策略 → 审批 → 执行 → 审计 → 结果。
    """

    def __init__(
        self,
        workspace_root: Path | str,
        state_dir: Path | str,
        *,
        approval: ApprovalService,
        default_timeout_seconds: int = 120,
        interpreter: Path | None = None,
    ) -> None:
        """绑定工作目录、状态目录与审批服务，并固定解释器"""
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.store = IntentStore(Path(state_dir) / "intents.db")
        self.approval = approval
        self.default_timeout_seconds = default_timeout_seconds
        self._interpreter = (
            interpreter if interpreter is not None else resolve_cmd_interpreter()
        )

    def run(self, command: str, timeout_seconds: int | None = None) -> CommandOutcome:
        """执行一条命令；所有预期失败都返回结构化结果"""
        if not isinstance(command, str) or not command.strip():
            return CommandOutcome(
                status="denied",
                request_id="",
                error_code="INVALID_ARGUMENT",
                message="command must be a non-empty string",
            )
        if len(command) > MAX_COMMAND_CHARS:
            return CommandOutcome(
                status="denied",
                request_id="",
                error_code="INVALID_ARGUMENT",
                message=f"command exceeds the {MAX_COMMAND_CHARS} character limit",
            )
        return self._run(command, timeout_seconds)

    def _run(
        self,
        command: str,
        timeout_seconds: int | None,
    ) -> CommandOutcome:
        request_id = _new_request_id()
        effective_timeout = self._normalize_timeout(timeout_seconds)
        command_hash = hashlib.sha256(command.encode("utf-8")).hexdigest()
        now = utc_now_iso()
        intent = IntentRecord(
            request_id=request_id,
            operation="host_command",
            relative_path=".",
            canonical_path=str(self.workspace_root),
            content_hash=command_hash,
            snapshot_id="",
            status="prepared",
            created_at=now,
            updated_at=now,
        )
        self.store.create_intent(intent)

        policy = evaluate_command(command)
        if policy.decision == "forbidden":
            return self._finalize_without_execution(
                request_id,
                command,
                reason=policy.reason,
                audit_decision="forbidden",
                decided_by="policy:forbidden",
            )
        if policy.decision != "prompt":
            # 当前部署没有 allow 档；出现其他值属于编程错误，fail closed
            return self._finalize_without_execution(
                request_id,
                command,
                reason="无法识别的策略决议，按拒绝处理",
                audit_decision="unavailable",
                decided_by="policy:fail-closed",
                unavailable=True,
            )

        request = ApprovalRequest(
            request_id=request_id,
            kind="host_command",
            command=command,
            cwd=str(self.workspace_root),
            timeout_seconds=effective_timeout,
        )
        verdict, decided_by = self.approval.adjudicate(request)
        if verdict == "rejected":
            return self._finalize_without_execution(
                request_id,
                command,
                reason="用户拒绝执行该命令",
                audit_decision="rejected",
                decided_by=decided_by,
            )
        if verdict == "unavailable":
            return self._finalize_without_execution(
                request_id,
                command,
                reason="审批通道不可用，无人可以批准",
                audit_decision="unavailable",
                decided_by=decided_by,
                unavailable=True,
            )
        if verdict != "allowed_once":
            # defer 到链尾仍无决定 → adjudicate 已 fail closed；防御分支
            return self._finalize_without_execution(
                request_id,
                command,
                reason="审批链没有决定性答复",
                audit_decision="unavailable",
                decided_by="fail-closed",
                unavailable=True,
            )

        self.store.record_shell_audit(
            request_id,
            command=command,
            decision="allowed_once",
            decided_by=decided_by,
            status="executing",
        )
        if not self.store.mark_command_executing(request_id):
            raise RuntimeError(
                f"intent transition prepared->executing failed for {request_id}"
            )

        started = time.perf_counter()
        argv = [str(self._interpreter), "/d", "/s", "/c", command]
        try:
            process_result = run_bounded(
                argv,
                cwd=self.workspace_root,
                timeout_seconds=effective_timeout,
                env=child_environment(),
            )
        except KeyboardInterrupt:
            # 取消不是普通失败：进程树已由 run_bounded 终止，
            # intent 记为 failed（未完成）后向上传播，由 CLI 按"取消本轮"处理
            duration_ms = (time.perf_counter() - started) * 1000
            self.store.mark_failed(request_id, "KeyboardInterrupt")
            self.store.record_shell_audit(
                request_id,
                command=command,
                decision="allowed_once",
                decided_by=decided_by,
                status="interrupted",
                duration_ms=duration_ms,
            )
            logger.warning(
                "command interrupted by user; request_id=%s duration_ms=%.1f",
                request_id,
                duration_ms,
            )
            raise
        except OSError as error:
            duration_ms = (time.perf_counter() - started) * 1000
            self.store.mark_failed(request_id, type(error).__name__)
            self.store.record_shell_audit(
                request_id,
                command=command,
                decision="allowed_once",
                decided_by=decided_by,
                status="failed",
                duration_ms=duration_ms,
            )
            logger.error(
                "command could not be started; request_id=%s exception_type=%s",
                request_id,
                type(error).__name__,
            )
            return CommandOutcome(
                status="failed",
                request_id=request_id,
                duration_ms=duration_ms,
                error_code="SPAWN_FAILED",
                message="命令无法启动，未产生任何副作用。",
            )

        duration_ms = (time.perf_counter() - started) * 1000
        exit_code = process_result.returncode
        timed_out = process_result.timed_out
        if timed_out:
            status = "timed_out"
        elif exit_code == 0:
            status = "succeeded"
        else:
            status = "failed"
        self.store.mark_done(request_id)
        self.store.record_shell_audit(
            request_id,
            command=command,
            decision="allowed_once",
            decided_by=decided_by,
            status=status,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
        )
        logger.info(
            "command finished; request_id=%s status=%s exit_code=%s "
            "duration_ms=%.1f truncated=%s",
            request_id,
            status,
            exit_code,
            duration_ms,
            process_result.truncated,
        )
        return CommandOutcome(
            status=status,
            request_id=request_id,
            exit_code=exit_code,
            stdout=_presentable_output(process_result.stdout),
            stderr=_presentable_output(process_result.stderr),
            truncated=process_result.truncated,
            duration_ms=duration_ms,
        )

    def _finalize_without_execution(
        self,
        request_id: str,
        command: str,
        *,
        reason: str,
        audit_decision: str,
        decided_by: str,
        unavailable: bool = False,
    ) -> CommandOutcome:
        """策略拒绝或审批未通过：intent 标记 rejected，写入审计，绝不执行

        ``denied``（用户明确拒绝/策略禁止）与 ``unavailable``（无人能批准）
        是两个不同的结果状态——都 fail closed，但诊断语义不同。
        """
        self.store.record_shell_audit(
            request_id,
            command=command,
            decision=audit_decision,
            decided_by=decided_by,
            status="denied",
        )
        self.store.mark_rejected(request_id)
        logger.info(
            "command denied without execution; request_id=%s decision=%s reason=%s",
            request_id,
            audit_decision,
            reason,
        )
        status = "unavailable" if unavailable else "denied"
        return CommandOutcome(
            status=status,
            request_id=request_id,
            decision_reason=reason,
            message=reason,
        )

    def _normalize_timeout(self, timeout_seconds: int | None) -> int:
        """把请求超时收敛到允许区间；非法值回落默认值"""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds < 5
        ):
            return self.default_timeout_seconds
        return min(timeout_seconds, self.default_timeout_seconds)


def _new_request_id() -> str:
    """生成一次命令请求的稳定 ID"""
    return uuid.uuid4().hex


def _presentable_output(data: bytes) -> str:
    """把子进程输出脱敏并按头尾截断为可展示文本"""
    text = data.decode("utf-8", errors="replace")
    redacted = redact_text(text)
    limit = RESPONSE_HEAD_BYTES + RESPONSE_TAIL_BYTES
    if len(redacted.encode("utf-8")) <= limit:
        return redacted
    head = truncate_utf8(redacted, RESPONSE_HEAD_BYTES)
    tail = truncate_utf8(redacted[-RESPONSE_TAIL_BYTES * 2 :], RESPONSE_TAIL_BYTES)
    return f"{head}{_TRUNCATION_MARKER}{tail}"
