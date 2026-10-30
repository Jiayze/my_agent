"""V0.3 知识库检索工具测试：确定性检索、边界与装配。"""

import json
from pathlib import Path

import pytest

from my_agent.config import AgentConfig, ConfigurationError
from my_agent.tools import get_tools
from my_agent.tools.knowledge import build_knowledge_base_tools


@pytest.fixture()
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "python-notes.md").write_text(
        "# Python 指南\n\nPython 是一门动态语言。\n支持类型标注。\n",
        encoding="utf-8",
    )
    (root / "deploy.txt").write_text(
        "部署步骤：先运行 pytest，再打包。\n",
        encoding="utf-8",
    )
    return root


def _payload(result: str) -> dict[str, object]:
    return json.loads(result)


def test_search_matches_content_and_returns_lines(kb_root: Path) -> None:
    (tool,) = build_knowledge_base_tools(kb_root)

    result = _payload(tool.invoke({"query": "pytest"}))

    assert result["ok"] is True
    matches = result["matches"]
    assert len(matches) == 1
    assert matches[0]["path"] == "deploy.txt"
    assert "pytest" in str(matches[0]["lines"][0]["text"])


def test_filename_match_scores_higher_than_content(kb_root: Path) -> None:
    (tool,) = build_knowledge_base_tools(kb_root)

    result = _payload(tool.invoke({"query": "python"}))

    matches = result["matches"]
    assert matches[0]["path"] == "python-notes.md"
    assert matches[0]["score"] >= 3


def test_no_match_returns_empty(kb_root: Path) -> None:
    (tool,) = build_knowledge_base_tools(kb_root)

    result = _payload(tool.invoke({"query": "不存在的词条"}))

    assert result["ok"] is True
    assert result["matches"] == []
    assert result["total_matches"] == 0


def test_sensitive_files_are_skipped(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / ".env").write_text("SECRET=zzz\n", encoding="utf-8")
    (tool,) = build_knowledge_base_tools(root)

    result = _payload(tool.invoke({"query": "SECRET"}))

    assert result["matches"] == []


def test_query_validation(kb_root: Path) -> None:
    (tool,) = build_knowledge_base_tools(kb_root)

    assert _payload(tool.invoke({"query": "  "}))["ok"] is False
    assert _payload(tool.invoke({"query": "x" * 257}))["ok"] is False


def test_non_utf8_files_are_counted_as_skipped(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "binary.bin").write_bytes(b"\x81\x82\x83\xff\xfe")
    (tool,) = build_knowledge_base_tools(root)

    result = _payload(tool.invoke({"query": "x"}))

    assert result["skipped_undecodable"] == 1


def test_get_tools_registers_kb_tool_only_when_configured(
    tmp_path: Path,
) -> None:
    default_registry = get_tools(tmp_path)
    assert "search_knowledge_base" not in default_registry

    registry = get_tools(tmp_path, knowledge_base_root=tmp_path / "knowledge")
    assert "search_knowledge_base" in registry


def _make_config(workspace_root: Path, knowledge_base_root: Path) -> AgentConfig:
    return AgentConfig(
        api_key="test-key",
        model_id="test-model",
        base_url="https://api.example.invalid",
        request_timeout_seconds=1,
        max_tool_rounds=1,
        temperature=0,
        workspace_root=workspace_root,
        log_level="INFO",
        knowledge_base_root=knowledge_base_root,
    )


def test_config_rejects_knowledge_base_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "kb"
    inside.mkdir()

    with pytest.raises(ConfigurationError, match="separate"):
        _make_config(workspace, inside)


def test_config_rejects_workspace_inside_knowledge_base(tmp_path: Path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    nested_workspace = kb / "workspace"
    nested_workspace.mkdir()

    with pytest.raises(ConfigurationError, match="separate"):
        _make_config(nested_workspace, kb)


def test_config_accepts_disjoint_knowledge_base(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    kb = tmp_path / "kb"
    kb.mkdir()

    config = _make_config(workspace, kb)

    assert config.knowledge_base_root == kb.resolve()
