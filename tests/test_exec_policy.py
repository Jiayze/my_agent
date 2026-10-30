"""V0.5 命令执行策略测试：allow/prompt/forbidden 分类矩阵。"""

import pytest

from my_agent.exec_policy import MAX_COMMAND_CHARS, evaluate_command


def test_empty_command_is_prompt_not_allow() -> None:
    decision = evaluate_command("   ")

    assert decision.decision == "prompt"
    assert decision.decision != "allow"


def test_plain_commands_require_prompt() -> None:
    for command in (
        "python -m pytest -q",
        "git status",
        "git log --oneline -5",
        "dir",
        "echo hello",
        ".\\.venv\\Scripts\\python.exe -m ruff check .",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "prompt", command


def test_file_deletion_is_forbidden() -> None:
    for command in (
        "del note.txt",
        "DEL /Q note.txt",
        "erase note.txt",
        "rd /s /q build",
        "rmdir build",
        "rm -rf .",
        "Remove-Item -Recurse -Force src",
        "remove-item note.txt",
        "ri note.txt",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "forbidden", command


def test_git_remote_and_history_commands_are_forbidden() -> None:
    for command in (
        "git push origin master",
        "git push",
        "git reset --hard HEAD~1",
        "git reset",
        "git revert HEAD",
        "git rebase -i main",
        "git rebase main",
        "git clean -fd",
        "git filter-branch",
        "git -c foo=bar push",
        "git --git-dir=x push",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "forbidden", command


def test_system_modification_is_forbidden() -> None:
    for command in (
        "reg add HKCU\\Test /v x /d 1",
        "reg delete HKCU\\Test",
        "setx PATH blah",
        "schtasks /create ...",
        "sc config wuauserv start= disabled",
        "net user admin password",
        "netsh advfirewall set allprofiles state off",
        "bcdedit /set testsigning on",
        "format e:",
        "diskpart",
        "shutdown /s",
        "icacls file /grant everyone:F",
        "taskkill /f /im python.exe",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "forbidden", command


def test_compound_command_with_forbidden_segment_is_forbidden() -> None:
    for command in (
        "python -m pytest -q && git push",
        "echo ok & del note.txt",
        "echo ok || rd /s /q build",
        "python -m pytest; git reset --hard",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "forbidden", command


def test_case_and_token_boundaries() -> None:
    # 大小写不敏感
    assert evaluate_command("GIT PUSH").decision == "forbidden"
    # 子串不算独立词：desc/deploy 不触发 sc/del
    assert evaluate_command("python desc.py").decision == "prompt"
    assert evaluate_command("python deploy.py").decision == "prompt"
    # 引号包裹的删除仍然被识别
    assert evaluate_command('cmd /c "del note.txt"').decision == "forbidden"


def test_oversized_command_degrades_to_prompt() -> None:
    decision = evaluate_command("echo " + "x" * (MAX_COMMAND_CHARS + 10))

    assert decision.decision == "prompt"
    assert decision.category == "oversized"


def test_policy_decision_is_structured() -> None:
    decision = evaluate_command("del note.txt")

    assert decision.category
    assert decision.reason
    with pytest.raises(AttributeError):
        decision.decision = "allow"  # type: ignore[misc]
