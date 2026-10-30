"""V0.3 只读仓库工具的安全、限额与装配测试。

全部离线：不访问网络，不使用真实模型。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from pydantic import ValidationError

from my_agent.agent import Agent
from my_agent.tools import ToolRegistry, get_tools
from my_agent.tools.path_policy import (
    PathPolicyError,
    _same_file_identity,
    is_sensitive_relative,
    redact_text,
    resolve_workspace_path,
)
from my_agent.tools.repo_read import (
    MAX_READ_FILE_BYTES,
    MAX_READ_LINES,
    build_repo_read_tools,
    resolve_git,
    resolve_ripgrep,
)

GIT = resolve_git()
RG = resolve_ripgrep()


def payload(result: str) -> dict[str, Any]:
    """解析工具返回的 JSON 文本"""
    return json.loads(result)


def write(root: Path, relative: str, content: str | bytes) -> Path:
    """在工作区写入测试文件并返回路径"""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def tool_named(tools: Sequence[Any], name: str) -> Any:
    """按名称取出已构造工具"""
    for candidate in tools:
        if candidate.name == name:
            return candidate
    raise AssertionError(f"tool not found: {name}")


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """用测试宿主的 git 准备 fixture 仓库"""
    assert GIT is not None
    return subprocess.run(
        [str(GIT), *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def init_repo(repo: Path) -> None:
    """初始化带一次提交的临时仓库"""
    repo.mkdir(parents=True, exist_ok=True)
    run_git(repo, "init", "-q")
    run_git(repo, "config", "user.email", "test@example.invalid")
    run_git(repo, "config", "user.name", "Test User")
    # 工具固定屏蔽全局配置；fixture 也固定行尾处理，避免 CRLF 造成伪改动
    run_git(repo, "config", "core.autocrlf", "false")
    write(repo, "notes.txt", "alpha\n")
    write(repo, ".env", "API_KEY=placeholder-value\n")
    run_git(repo, "add", "notes.txt")
    run_git(repo, "add", "-f", ".env")
    run_git(repo, "commit", "-q", "-m", "init")


def short_name(path: Path) -> str | None:
    """用 GetShortPathNameW 获取 8.3 短名；不可用时返回 None"""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    function = ctypes.windll.kernel32.GetShortPathNameW
    function.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(str(path), buffer, len(buffer))
    if length == 0 or length >= len(buffer):
        return None
    short = Path(buffer.value)
    if os.path.normcase(str(short)) == os.path.normcase(str(path)):
        return None
    return short.name


# ---------- 路径策略 ----------


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("../outside.txt", "PATH_TRAVERSAL_DENIED"),
        ("sub/../../outside.txt", "PATH_TRAVERSAL_DENIED"),
        ("/etc/passwd", "PATH_ABSOLUTE_DENIED"),
        ("C:/Windows/win.ini", "PATH_ABSOLUTE_DENIED"),
        ("C:relative", "PATH_ABSOLUTE_DENIED"),
        ("\\\\server\\share\\file.txt", "PATH_ABSOLUTE_DENIED"),
        ("\\\\?\\C:\\Windows", "PATH_ABSOLUTE_DENIED"),
        ("notes.txt:secret", "PATH_INVALID"),
        ("nul", "PATH_INVALID"),
        ("bad\x00name", "PATH_INVALID"),
        ("trailing.", "PATH_INVALID"),
    ],
)
def test_path_policy_rejects_windows_escape_forms(
    tmp_path: Path, raw: str, code: str
) -> None:
    write(tmp_path, "notes.txt", "alpha")

    with pytest.raises(PathPolicyError) as exc_info:
        resolve_workspace_path(tmp_path, raw)

    assert exc_info.value.code == code


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.example",
        ".ENV",
        ".env.local",
        ".env.production",
        "prod.env",
        ".envrc",
        ".ENVRC",
        "server.pem",
        "token.txt",
        "SECRET_KEY.txt",
        "credentials.json",
        "access_key.txt",
        "db_password.txt",
    ],
)
def test_path_policy_rejects_sensitive_names_case_insensitively(
    tmp_path: Path, name: str
) -> None:
    write(tmp_path, name, "value")

    with pytest.raises(PathPolicyError) as exc_info:
        resolve_workspace_path(tmp_path, name)

    assert exc_info.value.code == "PATH_SENSITIVE_DENIED"


@pytest.mark.parametrize("name", ["environment.md", "notes.txt", "src/main.py"])
def test_path_policy_accepts_non_sensitive_names(tmp_path: Path, name: str) -> None:
    write(tmp_path, name, "value")

    resolved = resolve_workspace_path(tmp_path, name)

    assert resolved.relative == name


def test_path_policy_rejects_sensitive_directory_in_nested_path(tmp_path: Path) -> None:
    write(tmp_path, ".git/config", "[core]\n")

    with pytest.raises(PathPolicyError) as exc_info:
        resolve_workspace_path(tmp_path, ".GIT/config")

    assert exc_info.value.code == "PATH_SENSITIVE_DENIED"


def test_path_policy_accepts_normal_nested_path(tmp_path: Path) -> None:
    write(tmp_path, "sub/notes.txt", "alpha")

    resolved = resolve_workspace_path(tmp_path, "sub\\notes.txt", expect="file")

    assert resolved.relative == "sub/notes.txt"


def test_path_policy_rejects_symlink_pointing_inside_workspace(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    write(target, "notes.txt", "alpha")
    link = tmp_path / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this host")

    with pytest.raises(PathPolicyError) as exc_info:
        resolve_workspace_path(tmp_path, "link/notes.txt")

    assert exc_info.value.code == "PATH_SYMLINK_DENIED"


def test_path_policy_rejects_junction_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir(exist_ok=True)
    write(outside, "outside.txt", "outside")
    link = tmp_path / "junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
    )
    if completed.returncode != 0:
        pytest.skip("junction creation is not permitted on this host")

    with pytest.raises(PathPolicyError) as exc_info:
        resolve_workspace_path(tmp_path, "junction/outside.txt")

    assert exc_info.value.code == "PATH_SYMLINK_DENIED"


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows-only")
def test_path_policy_rejects_sensitive_8_3_aliases(tmp_path: Path) -> None:
    (tmp_path / "local_artifacts").mkdir()
    write(tmp_path, "local_artifacts/credentials.json", "{}")
    (tmp_path / ".pytest_cache").mkdir()

    directory_alias = short_name(tmp_path / "local_artifacts")
    file_alias = short_name(tmp_path / "local_artifacts" / "credentials.json")
    cache_alias = short_name(tmp_path / ".pytest_cache")
    if directory_alias is None or file_alias is None or cache_alias is None:
        pytest.skip("8.3 short names are not available on this volume")

    for raw in (directory_alias, f"{directory_alias}/{file_alias}", cache_alias):
        with pytest.raises(PathPolicyError) as exc_info:
            resolve_workspace_path(tmp_path, raw)
        assert exc_info.value.code == "PATH_SENSITIVE_DENIED"


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names are Windows-only")
def test_resolved_relative_uses_canonical_long_name(tmp_path: Path) -> None:
    directory = tmp_path / "LongDirectoryName"
    directory.mkdir()
    write(directory, "notes.txt", "alpha")
    alias = short_name(directory)
    if alias is None:
        pytest.skip("8.3 short names are not available on this volume")

    resolved = resolve_workspace_path(tmp_path, f"{alias}/notes.txt", expect="file")

    assert resolved.relative == "LongDirectoryName/notes.txt"
    assert resolved.absolute == directory / "notes.txt"


# ---------- 工具行为 ----------


def test_list_directory_hides_sensitive_entries_and_skips_links(tmp_path: Path) -> None:
    write(tmp_path, "notes.txt", "alpha")
    write(tmp_path, ".env", "API_KEY=placeholder")
    write(tmp_path, "server.pem", "-----BEGIN-----")
    write(tmp_path, "token_cache.txt", "value")
    write(tmp_path, "prod.env", "API_KEY=placeholder")
    write(tmp_path, ".envrc", "export API_KEY=placeholder")
    (tmp_path / "sub").mkdir()
    tools = build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT)

    result = payload(tool_named(tools, "list_directory").invoke({"relative_path": "."}))

    names = {entry["name"] for entry in result["entries"]}
    assert names == {"notes.txt", "sub"}
    assert result["truncated"] is False


def test_list_directory_paginates_at_one_hundred_entries(tmp_path: Path) -> None:
    for index in range(120):
        write(tmp_path, f"f{index:03d}.txt", "x")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "list_directory"
    )

    first = payload(tool.invoke({"relative_path": "."}))
    second = payload(
        tool.invoke({"relative_path": ".", "offset": first["next_offset"]})
    )

    assert len(first["entries"]) == 100
    assert first["next_offset"] == 100
    assert first["truncated"] is True
    assert len(second["entries"]) == 20
    assert second["next_offset"] is None


def test_read_text_file_paginates_and_marks_eof(tmp_path: Path) -> None:
    write(tmp_path, "lines.txt", "\n".join(f"line-{i}" for i in range(250)))
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    first = payload(tool.invoke({"relative_path": "lines.txt"}))
    second = payload(
        tool.invoke(
            {"relative_path": "lines.txt", "start_line": first["next_start_line"]}
        )
    )

    assert len(first["lines"]) == MAX_READ_LINES
    assert first["lines"][0] == {"number": 1, "text": "line-0"}
    assert first["next_start_line"] == 201
    assert first["eof"] is False
    assert len(second["lines"]) == 50
    assert second["eof"] is True
    assert second["next_start_line"] is None


def test_read_text_file_truncates_long_lines(tmp_path: Path) -> None:
    write(tmp_path, "long.txt", "x" * 3000)
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    result = payload(tool.invoke({"relative_path": "long.txt"}))

    assert len(result["lines"][0]["text"]) == 2000
    assert result["truncated"] is True


@pytest.mark.parametrize(
    ("content", "code"),
    [
        pytest.param(b"\x00\x01\x02", "FS_BINARY", id="binary"),
        pytest.param(b"\xff\xfe\xfd", "FS_NOT_UTF8", id="invalid-utf8"),
        pytest.param(b"a" * (MAX_READ_FILE_BYTES + 1), "FS_TOO_LARGE", id="oversized"),
    ],
)
def test_read_text_file_rejects_unsupported_content(
    tmp_path: Path, content: bytes, code: str
) -> None:
    write(tmp_path, "blob.bin", content)
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    result = payload(tool.invoke({"relative_path": "blob.bin"}))

    assert result["ok"] is False
    assert result["error"] == code
    assert str(tmp_path) not in json.dumps(result)


def test_read_text_file_reports_missing_and_directory(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    missing = payload(tool.invoke({"relative_path": "missing.txt"}))
    directory = payload(tool.invoke({"relative_path": "sub"}))

    assert missing["error"] == "FS_NOT_FOUND"
    assert directory["error"] == "FS_NOT_FILE"


def test_read_text_file_maps_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "notes.txt", "alpha")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    def deny(*args: object, **kwargs: object) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr("my_agent.tools.path_policy._open", deny)

    result = payload(tool.invoke({"relative_path": "notes.txt"}))

    assert result["error"] == "FS_PERMISSION_DENIED"


def test_read_text_file_rejects_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(tmp_path, "notes.txt", "alpha")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )
    real_fstat = os.fstat

    def changed_fstat(descriptor: int) -> Any:
        actual = real_fstat(descriptor)
        return SimpleNamespace(
            st_mode=actual.st_mode,
            st_dev=actual.st_dev + 1,
            st_ino=actual.st_ino + 1,
            st_size=actual.st_size,
            st_file_attributes=getattr(actual, "st_file_attributes", 0),
        )

    monkeypatch.setattr("my_agent.tools.path_policy._fstat", changed_fstat)

    result = payload(tool.invoke({"relative_path": "notes.txt"}))

    assert result["error"] == "FS_CHANGED"


def test_same_file_identity_detects_inode_and_reparse_changes() -> None:
    def stub(**overrides: int) -> Any:
        values = {
            "st_mode": stat.S_IFREG | 0o644,
            "st_dev": 1,
            "st_ino": 2,
            "st_file_attributes": 0,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    assert _same_file_identity(stub(), stub()) is True
    assert _same_file_identity(stub(), stub(st_ino=3)) is False
    assert _same_file_identity(stub(), stub(st_dev=2)) is False
    assert _same_file_identity(stub(), stub(st_file_attributes=0x400)) is False


def test_read_text_file_redacts_secret_patterns(tmp_path: Path) -> None:
    write(tmp_path, "config.txt", "api_key=abcdef123456\nnormal line\n")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    result = payload(tool.invoke({"relative_path": "config.txt"}))
    joined = "\n".join(line["text"] for line in result["lines"])

    assert "abcdef123456" not in joined
    assert "[REDACTED]" in joined
    assert "normal line" in joined


@pytest.mark.skipif(RG is None, reason="ripgrep is not available")
def test_search_text_skips_sensitive_files_and_limits_matches(tmp_path: Path) -> None:
    write(tmp_path, "notes.txt", "\n".join(["needle"] * 20))
    write(tmp_path, "token_store.txt", "needle\n")
    write(tmp_path, ".env", "needle\n")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "search_text"
    )

    result = payload(tool.invoke({"query": "needle"}))

    assert result["ok"] is True
    assert len(result["matches"]) == 5
    assert {match["path"] for match in result["matches"]} == {"notes.txt"}
    assert result["matches"][0]["line"] == 1
    assert result["scan_truncated"] is False


@pytest.mark.skipif(RG is None, reason="ripgrep is not available")
def test_search_text_redacts_and_rejects_long_pattern(tmp_path: Path) -> None:
    write(tmp_path, "notes.txt", "api_key=abcdef123456\n")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "search_text"
    )

    result = payload(tool.invoke({"query": "api_key"}))
    too_long = payload(tool.invoke({"query": "x" * 257}))

    assert "abcdef123456" not in result["matches"][0]["text"]
    assert too_long["error"] == "INVALID_ARGUMENT"


@pytest.mark.skipif(RG is None, reason="ripgrep is not available")
def test_search_text_paginates_results(tmp_path: Path) -> None:
    for index in range(60):
        write(tmp_path, f"note{index:02d}.txt", "needle\n")
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "search_text"
    )

    first = payload(tool.invoke({"query": "needle"}))
    second = payload(tool.invoke({"query": "needle", "offset": 50}))

    assert len(first["matches"]) == 50
    assert first["next_offset"] == 50
    assert len(second["matches"]) == 10
    assert second["next_offset"] is None


@pytest.mark.skipif(RG is None, reason="ripgrep is not available")
def test_search_text_marks_incomplete_scan_as_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a.txt", "b.txt", "c.txt"):
        write(tmp_path, name, "needle\n")
    monkeypatch.setattr("my_agent.tools.repo_read.MAX_SEARCH_FILES", 2)
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "search_text"
    )

    result = payload(tool.invoke({"query": "needle"}))

    assert result["scan_truncated"] is True
    assert result["truncated"] is True
    assert len({match["path"] for match in result["matches"]}) == 2


def test_read_tools_expose_only_fixed_schema_parameters(tmp_path: Path) -> None:
    tools = build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT)

    search_schema = tool_named(
        tools, "search_text"
    ).tool_call_schema.model_json_schema()
    status_schema = tool_named(tools, "git_status").tool_call_schema.model_json_schema()
    diff_schema = tool_named(tools, "git_diff").tool_call_schema.model_json_schema()

    assert set(search_schema["properties"]) == {"query", "relative_path", "offset"}
    assert set(status_schema["properties"]) == {"repo_path"}
    assert set(diff_schema["properties"]) == {"repo_path", "paths", "staged"}


# ---------- Git fixture ----------


def _fake_command_script(directory: Path, marker: Path) -> Path:
    """创建一旦被执行就会留下 marker 的假外部命令"""
    script = directory / "fake-command.cmd"
    script.write_text(
        f'@echo off\r\necho ran >> "{marker}"\r\n',
        encoding="ascii",
    )
    return script


def _configure_fake_git_commands(repo: Path, script: Path) -> None:
    run_git(repo, "config", "core.fsmonitor", str(script))
    run_git(repo, "config", "diff.external", str(script))
    run_git(repo, "config", "diff.fake.textconv", str(script))
    for action in ("clean", "process", "smudge"):
        run_git(repo, "config", f"filter.fake.{action}", str(script))
    write(repo, ".gitattributes", "*.txt filter=fake diff=fake\n")


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_status_filters_sensitive_paths_and_never_runs_repo_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "marker.txt"
    script = _fake_command_script(tmp_path, marker)
    _configure_fake_git_commands(repo, script)
    write(repo, "new.txt", "untracked\n")
    write(repo, ".env", "API_KEY=changed-value\n")

    tool = tool_named(
        build_repo_read_tools(repo, rg_path=RG, git_path=GIT), "git_status"
    )
    result = payload(tool.invoke({"repo_path": "."}))

    paths = {entry["path"] for entry in result["entries"]}
    assert "new.txt" in paths
    assert ".env" not in paths
    assert not marker.exists()


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_status_hides_sibling_paths_for_nested_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    workspace = repo / "ws"
    workspace.mkdir()
    write(workspace, "inside.txt", "base\n")
    run_git(repo, "add", "ws/inside.txt")
    run_git(repo, "commit", "-q", "-m", "workspace")

    write(workspace, "inside.txt", "changed\n")
    write(repo, "outside.txt", "outside\n")
    write(repo, "notes.txt", "changed at repo root\n")

    tool = tool_named(
        build_repo_read_tools(workspace, rg_path=RG, git_path=GIT), "git_status"
    )
    result = payload(tool.invoke({"repo_path": "."}))

    paths = {entry["path"] for entry in result["entries"]}
    assert "inside.txt" in paths
    assert all(not path.startswith("..") for path in paths)
    assert "outside.txt" not in paths
    assert "notes.txt" not in paths


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_status_truncation_requires_more_than_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    monkeypatch.setattr("my_agent.tools.repo_read.MAX_GIT_ENTRIES", 2)
    tool = tool_named(
        build_repo_read_tools(repo, rg_path=RG, git_path=GIT), "git_status"
    )

    write(repo, "a.txt", "a\n")
    write(repo, "b.txt", "b\n")
    exact = payload(tool.invoke({"repo_path": "."}))
    write(repo, "c.txt", "c\n")
    overflow = payload(tool.invoke({"repo_path": "."}))

    assert len(exact["entries"]) == 2
    assert exact["truncated"] is False
    assert len(overflow["entries"]) == 2
    assert overflow["truncated"] is True


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_diff_filters_sensitive_sections_and_never_runs_repo_commands(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    marker = tmp_path / "marker.txt"
    script = _fake_command_script(tmp_path, marker)
    _configure_fake_git_commands(repo, script)
    write(repo, "notes.txt", "alpha\nvisible-change\n")
    write(repo, ".env", "API_KEY=SUPERSECRETVALUE\n")

    tool = tool_named(build_repo_read_tools(repo, rg_path=RG, git_path=GIT), "git_diff")
    result = payload(tool.invoke({"repo_path": "."}))

    assert "visible-change" in result["diff"]
    assert "notes.txt" in result["diff"]
    assert "SUPERSECRETVALUE" not in result["diff"]
    assert ".env" not in result["diff"]
    assert not marker.exists()


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_diff_staged_only_shows_index_changes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    write(repo, "notes.txt", "alpha\nstaged-line\n")
    run_git(repo, "add", "notes.txt")
    write(repo, "notes.txt", "alpha\nstaged-line\nunstaged-line\n")
    tool = tool_named(build_repo_read_tools(repo, rg_path=RG, git_path=GIT), "git_diff")

    staged = payload(tool.invoke({"repo_path": ".", "staged": True}))["diff"]
    unstaged = payload(tool.invoke({"repo_path": "."}))["diff"]

    assert "staged-line" in staged
    assert "unstaged-line" not in staged
    assert "unstaged-line" in unstaged


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_diff_resolves_workspace_paths_from_repo_subdirectory(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    sub = repo / "sub"
    sub.mkdir()
    write(sub, "with space.txt", "base\n")
    run_git(repo, "add", "sub/with space.txt")
    run_git(repo, "commit", "-q", "-m", "sub")
    write(sub, "with space.txt", "base\nchanged in subdirectory\n")
    run_git(repo, "config", "diff.noprefix", "true")
    run_git(repo, "config", "diff.mnemonicPrefix", "true")
    tool = tool_named(build_repo_read_tools(repo, rg_path=RG, git_path=GIT), "git_diff")

    result = payload(tool.invoke({"repo_path": "sub", "paths": ["sub/with space.txt"]}))

    assert result["ok"] is True
    assert "changed in subdirectory" in result["diff"]
    assert "diff --git a/sub/with space.txt b/sub/with space.txt" in result["diff"]
    assert result["unparsed_sections"] == 0


@pytest.mark.skipif(GIT is None, reason="git is not available")
def test_git_diff_from_nested_workspace_uses_workspace_relative_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    init_repo(repo)
    workspace = repo / "ws"
    workspace.mkdir()
    write(workspace, "inside.txt", "base\n")
    run_git(repo, "add", "ws/inside.txt")
    run_git(repo, "commit", "-q", "-m", "workspace")
    write(workspace, "inside.txt", "base\nnested change\n")
    tool = tool_named(
        build_repo_read_tools(workspace, rg_path=RG, git_path=GIT), "git_diff"
    )

    result = payload(tool.invoke({"repo_path": ".", "paths": ["inside.txt"]}))

    assert "nested change" in result["diff"]
    assert "inside.txt" in result["diff"]
    assert "diff --git a/ws/inside.txt b/ws/inside.txt" in result["diff"]


def test_split_diff_header_handles_space_and_quoted_paths() -> None:
    from my_agent.tools.repo_read import _split_diff_header

    assert _split_diff_header("diff --git a/with space.txt b/with space.txt") == (
        "with space.txt",
        "with space.txt",
    )
    assert _split_diff_header('diff --git "a/tab\\there.txt" "b/tab\\there.txt"') == (
        "tab\there.txt",
        "tab\there.txt",
    )
    assert _split_diff_header("diff --git a/x b/y") is None


def test_run_bounded_terminates_on_timeout() -> None:
    from my_agent.tools.repo_read import _run_bounded

    result = _run_bounded(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=Path.cwd(),
        timeout_seconds=1,
    )

    assert result.timed_out is True


def test_run_bounded_caps_output_while_running() -> None:
    from my_agent.tools.repo_read import _run_bounded

    result = _run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()",
        ],
        cwd=Path.cwd(),
        timeout_seconds=30,
        max_output_bytes=1024,
    )

    assert len(result.stdout) <= 1024
    assert result.truncated is True
    assert result.timed_out is False


def test_run_bounded_caps_output_on_timeout() -> None:
    from my_agent.tools.repo_read import _run_bounded

    script = (
        "import sys, time\n"
        "sys.stdout.write('x' * 1000)\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    started = time.monotonic()
    result = _run_bounded(
        [sys.executable, "-c", script],
        cwd=Path.cwd(),
        timeout_seconds=1,
        max_output_bytes=2048,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert len(result.stdout) <= 2048
    assert elapsed < 15


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree semantics")
def test_run_bounded_timeout_with_grandchild_holding_pipe_returns(
    tmp_path: Path,
) -> None:
    from my_agent.tools.repo_read import _run_bounded

    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = _run_bounded(
        [sys.executable, str(script)],
        cwd=tmp_path,
        timeout_seconds=1,
    )
    elapsed = time.monotonic() - started

    assert result.timed_out is True
    assert elapsed < 15


# ---------- 装配与离线 Agent ----------


def test_default_registry_contains_five_read_only_tools(tmp_path: Path) -> None:
    registry = get_tools(tmp_path)

    names = registry.get_tool_names()
    assert {
        "add_numbers",
        "list_directory",
        "read_text_file",
        "search_text",
        "git_status",
        "git_diff",
    } <= names
    for name in names:
        assert registry.get_tool(name).metadata == {"effect": "read_only"}
    for forbidden in (
        "write_text_file",
        "delete_file",
        "edit_file",
        "shell",
        "git_commit",
    ):
        assert forbidden not in names


def test_sensitive_relative_helper_matches_case_variants() -> None:
    assert is_sensitive_relative(".GIT/config") is True
    assert is_sensitive_relative("src/token_store.py") is True
    assert is_sensitive_relative("prod.env") is True
    assert is_sensitive_relative(".envrc") is True
    assert is_sensitive_relative("src/my_agent/main.py") is False


def test_redact_text_covers_common_secret_shapes() -> None:
    text = "token=abcdef123456 key=sk-abcdefghijklmnop"

    redacted = redact_text(text)

    assert "abcdef123456" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted


def test_redact_text_covers_assignment_names_and_truncated_pem() -> None:
    secrets = (
        "abcdef123456",
        "wJalrXUtnFEMIK7MDENG",
        "AKIAIOSFODNN7EXAMPLE",
        "MIIEabcdefghijklmnop",
    )
    text = "\n".join(
        [
            "SECRET_KEY=abcdef123456",
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENG",
            "API_KEY=abcdef123456",
            "ACCESS_KEY=AKIAIOSFODNN7EXAMPLE",
            '"api_key": "abcdef123456"',
            "normal=value",
            "-----BEGIN RSA PRIVATE KEY-----",
            "MIIEabcdefghijklmnop",
        ]
    )

    redacted = redact_text(text)

    for secret in secrets:
        assert secret not in redacted
    assert "normal=value" in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


def test_redact_text_keeps_text_after_complete_pem_block() -> None:
    text = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEabcdefghijklmnop\n"
        "-----END PRIVATE KEY-----\n"
        "after-block\n"
    )

    redacted = redact_text(text)

    assert "MIIEabcdefghijklmnop" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted
    assert "after-block" in redacted


class ScriptedModel:
    """离线脚本模型：按顺序返回预置响应并记录收到的消息"""

    def __init__(self, responses: Sequence[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[BaseMessage, ...]] = []

    def bind_tools(self, tools: Sequence[object]) -> "ScriptedModel":
        return self

    def invoke(
        self,
        messages: list[BaseMessage],
        *,
        config: object | None = None,
    ) -> object:
        self.calls.append(tuple(messages))
        return self.responses.pop(0)


def _tool_call(name: str, args: dict[str, Any], call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


@pytest.mark.skipif(RG is None, reason="ripgrep is not available")
def test_offline_agent_executes_search_and_read_tools(tmp_path: Path) -> None:
    fact = "zebra-quasar-7731"
    write(tmp_path, "facts.txt", f"alpha {fact} omega\n")
    tools = build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT)
    model = ScriptedModel(
        [
            _tool_call("search_text", {"query": "zebra"}, "call-search"),
            _tool_call("read_text_file", {"relative_path": "facts.txt"}, "call-read"),
            AIMessage(content="已找到事实。"),
        ]
    )
    agent = Agent(model, ToolRegistry(tools), max_tool_rounds=3)

    result = agent.invoke([])

    assert result.ok
    assert result.tool_rounds == 2
    tool_messages = [
        message for message in result.messages if isinstance(message, ToolMessage)
    ]
    assert [message.tool_call_id for message in tool_messages] == [
        "call-search",
        "call-read",
    ]
    assert all(message.status == "success" for message in tool_messages)
    assert fact in str(tool_messages[0].content)
    assert fact in str(tool_messages[1].content)


def test_tool_schema_rejects_wrong_argument_types(tmp_path: Path) -> None:
    tool = tool_named(
        build_repo_read_tools(tmp_path, rg_path=RG, git_path=GIT), "read_text_file"
    )

    with pytest.raises(ValidationError):
        tool.invoke({"relative_path": ["not", "a", "string"]})


def test_git_helpers_report_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        "my_agent.tools.repo_read._KNOWN_GIT_PATH", Path("C:/nonexistent/git.exe")
    )
    monkeypatch.setattr(
        "my_agent.tools.repo_read._VENDOR_RG_RELATIVE", Path("nonexistent/rg.exe")
    )
    monkeypatch.delenv("APPDATA", raising=False)

    assert resolve_git() is None
    assert resolve_ripgrep() is None
