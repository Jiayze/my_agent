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


def test_caret_escaped_deletion_is_caught() -> None:
    """返工 P2：cmd 的 ^ 转义变形必须被归一化识别"""
    for command in (
        "cmd /c d^el harmless.txt",
        "d^el harmless.txt",
        "r^d /s /q build",
        "CMD /C D^EL harmless.txt",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "forbidden", command


def test_arguments_with_forbidden_words_are_not_forbidden() -> None:
    """返工 P2：参数/子命令位置的同名 token 不应被误杀"""
    for command in (
        "python -m ruff format .",
        "npm run clean",
        "echo clean",
        "echo format",
        "pip cache clean --force",
        'python -c "print(format)"',
    ):
        decision = evaluate_command(command)
        assert decision.decision == "prompt", command


def test_forbidden_still_caught_in_command_position() -> None:
    """命令位置（裸命令或 cmd /c 等包装器之后）的禁词保持拒绝"""
    for command in (
        "format e:",
        "cmd /c format e:",
        "clean .",
        "start format e:",
        "cmd /c rd /s /q build",
    ):
        decision = evaluate_command(command)
        assert decision.decision == "forbidden", command


def test_script_code_forms_land_on_human_approval() -> None:
    """已知残余：脚本代码无法被字符串分类穷尽，落入人工审批"""
    for command in (
        "python -c \"import os; os.remove('harmless.txt')\"",
        'powershell -Command "Get-Content x"',
    ):
        decision = evaluate_command(command)
        assert decision.decision == "prompt", command


def test_policy_decision_reasons_do_not_overpromise() -> None:
    """返工 P2：对外措辞不得宣称'永久禁止'"""
    decision = evaluate_command("del note.txt")

    assert "永久" not in decision.reason
    assert "人工审批" in decision.reason


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
