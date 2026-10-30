"""V0.5 审批决策者链测试：ask/never、失败归一与 fail closed。"""

import pytest

from my_agent.approval import (
    ApprovalRequest,
    ApprovalService,
    HumanCliDecider,
    NullDecider,
)


def _request() -> ApprovalRequest:
    return ApprovalRequest(
        request_id="req-1",
        kind="host_command",
        command="python -m pytest -q",
        cwd=str(__file__),
        timeout_seconds=120,
    )


class FakeDecider:
    def __init__(self, verdict: object, decided_by: str = "fake") -> None:
        self._verdict = verdict
        self.decided_by = decided_by
        self.calls = 0

    def decide(self, request: ApprovalRequest) -> object:
        del request
        self.calls += 1
        if isinstance(self._verdict, BaseException):
            raise self._verdict
        return self._verdict


def test_ask_mode_returns_human_verdict() -> None:
    service = ApprovalService([FakeDecider("allowed_once")], mode="ask")

    verdict, decided_by = service.adjudicate(_request())

    assert verdict == "allowed_once"
    assert decided_by == "fake"


def test_defer_falls_through_to_next_decider() -> None:
    service = ApprovalService(
        [
            FakeDecider("defer", decided_by="first"),
            FakeDecider("allowed_once", decided_by="second"),
        ],
        mode="ask",
    )

    verdict, decided_by = service.adjudicate(_request())

    assert verdict == "allowed_once"
    assert decided_by == "second"


def test_never_mode_rejects_without_asking_anyone() -> None:
    decider = FakeDecider("allowed_once")
    service = ApprovalService([decider], mode="never")

    verdict, decided_by = service.adjudicate(_request())

    assert verdict == "rejected"
    assert decided_by == "policy:never"
    assert decider.calls == 0


def test_invalid_mode_rejected() -> None:
    with pytest.raises(ValueError, match="mode"):
        ApprovalService([FakeDecider("allowed_once")], mode="yolo")


def test_raising_decider_normalizes_to_unavailable() -> None:
    service = ApprovalService([FakeDecider(RuntimeError("boom"))], mode="ask")

    verdict, decided_by = service.adjudicate(_request())

    # 任何情况下都不得 fail open
    assert verdict == "unavailable"
    assert "decider-error" in decided_by


def test_non_vocabulary_verdict_normalizes_to_unavailable() -> None:
    service = ApprovalService([FakeDecider("sure-why-not")], mode="ask")

    verdict, decided_by = service.adjudicate(_request())

    assert verdict == "unavailable"
    assert "decider-error" in decided_by


def test_chain_without_terminal_decider_fails_closed() -> None:
    service = ApprovalService([NullDecider(), NullDecider()], mode="ask")

    verdict, decided_by = service.adjudicate(_request())

    assert verdict == "unavailable"
    assert decided_by == "fail-closed"


def test_empty_chain_fails_closed() -> None:
    service = ApprovalService([], mode="ask")

    verdict, _decided_by = service.adjudicate(_request())

    assert verdict == "unavailable"


def test_human_cli_decider_approves_and_rejects() -> None:
    outputs: list[str] = []

    approve = HumanCliDecider(lambda _prompt: "y", outputs.append)
    reject = HumanCliDecider(lambda _prompt: "n", outputs.append)

    assert approve.decide(_request()) == "allowed_once"
    assert reject.decide(_request()) == "rejected"
    # 审批提示必须包含命令全文，用户看到的就是将要执行的
    assert any("python -m pytest -q" in line for line in outputs)


def test_human_cli_decider_eof_is_unavailable_not_rejected() -> None:
    outputs: list[str] = []

    def raise_eof(_prompt: str) -> str:
        raise EOFError

    decider = HumanCliDecider(raise_eof, outputs.append)

    assert decider.decide(_request()) == "unavailable"


def test_human_cli_decider_interrupt_is_rejected() -> None:
    def raise_interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    decider = HumanCliDecider(raise_interrupt, lambda _line: None)

    assert decider.decide(_request()) == "rejected"


def test_human_cli_decider_decided_by_is_distinguishable() -> None:
    decider = HumanCliDecider(lambda _prompt: "y", lambda _line: None)

    assert decider.decided_by == "human:cli"
    assert HumanCliDecider.decided_by.fget is not None  # type: ignore[attr-defined]
