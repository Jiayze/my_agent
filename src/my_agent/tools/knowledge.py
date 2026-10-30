"""V0.3 知识库检索工具：确定性全文匹配，后端可替换

契约要点（agent_plan.md V0.3）：

- ``search_knowledge_base(query)`` 的目录来自 ``KNOWLEDGE_BASE_ROOT``，
  与 ``WORKSPACE_ROOT`` 分离：知识库目录只读，工作目录以后允许写入。
- 第一版检索用确定性实现（文件名、标题、全文匹配），不默认引入 StoryRAG。
- 路径边界与敏感策略复用 ``path_policy``，不另建一套校验。
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.tools import BaseTool, tool

from my_agent.tools.path_policy import collect_regular_files, redact_text

MAX_KB_QUERY_CHARS = 256
MAX_KB_FILE_BYTES = 256 * 1024
MAX_KB_RESULTS = 10
MAX_KB_LINES_PER_FILE = 3
MAX_KB_LINE_CHARS = 500

# 确定性打分：文件名命中权重最高，标题次之，全文最低
_SCORE_FILENAME = 3
_SCORE_HEADING = 2
_SCORE_CONTENT = 1


def _json(payload: dict[str, Any]) -> str:
    """以确定性格式序列化工具结果"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _error(code: str, message: str) -> str:
    """构造结构化错误结果"""
    return _json({"ok": False, "error": code, "message": message})


def _heading_of(text: str, stem: str) -> str:
    """提取文件标题：Markdown 一级标题优先，否则用文件名主干"""
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return stem


def _match_lines(text: str, query: str, limit: int) -> list[dict[str, Any]]:
    """返回文件内前 limit 个不重叠的匹配行（大小写不敏感）"""
    matches: list[dict[str, Any]] = []
    lowered_query = query.casefold()
    for number, line in enumerate(text.splitlines(), start=1):
        if lowered_query in line.casefold():
            matches.append(
                {
                    "line": number,
                    "text": redact_text(line[:MAX_KB_LINE_CHARS]),
                }
            )
            if len(matches) >= limit:
                break
    return matches


def build_knowledge_base_tools(kb_root: Path | str) -> tuple[BaseTool, ...]:
    """构造绑定到固定知识库目录的检索工具

    ``kb_root`` 只能由应用装配层注入（``AgentConfig.knowledge_base_root``），
    配置层已校验该目录存在且与工作目录互不包含；模型无法修改根目录。
    """
    root = Path(kb_root).expanduser().resolve()

    @tool
    def search_knowledge_base(query: str) -> str:
        """只读工具：在知识库目录中做确定性检索。

        query 为字面量关键词（非正则），最多 256 字符。检索按文件名、
        标题、全文匹配打分（大小写不敏感），返回最相关的文件及其匹配行；
        知识库目录独立于工作目录且只读，敏感路径与符号链接不会出现在结果中。
        返回 matches（path/score/lines）、total_matches 与 truncated。
        """
        if not isinstance(query, str) or not query.strip():
            return _error("INVALID_ARGUMENT", "query must be a non-empty string")
        query = query.strip()
        if len(query) > MAX_KB_QUERY_CHARS:
            return _error(
                "INVALID_ARGUMENT",
                f"query must be at most {MAX_KB_QUERY_CHARS} characters",
            )

        files, scan_truncated = collect_regular_files(root, root)
        lowered_query = query.casefold()
        candidates: list[dict[str, Any]] = []
        skipped_undecodable = 0
        for relative in files:
            file_path = root / Path(*PurePosixPath(relative).parts)
            try:
                raw = file_path.read_bytes()[: MAX_KB_FILE_BYTES + 1]
            except OSError:
                scan_truncated = True
                continue
            if len(raw) > MAX_KB_FILE_BYTES or b"\x00" in raw[:8192]:
                skipped_undecodable += 1
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped_undecodable += 1
                continue

            stem = PurePosixPath(relative).stem
            score = 0
            if lowered_query in stem.casefold():
                score += _SCORE_FILENAME
            heading = _heading_of(text, stem)
            if lowered_query in heading.casefold():
                score += _SCORE_HEADING
            lines = _match_lines(text, query, MAX_KB_LINES_PER_FILE)
            if lines:
                score += _SCORE_CONTENT
            if score <= 0:
                continue
            candidates.append(
                {
                    "path": relative,
                    "score": score,
                    "heading": redact_text(heading[:MAX_KB_LINE_CHARS]),
                    "lines": lines,
                }
            )

        candidates.sort(key=lambda item: (-item["score"], item["path"]))
        total = len(candidates)
        window = candidates[:MAX_KB_RESULTS]
        return _json(
            {
                "ok": True,
                "query": query,
                "matches": window,
                "total_matches": total,
                "truncated": total > len(window) or scan_truncated,
                "scan_truncated": scan_truncated,
                "skipped_undecodable": skipped_undecodable,
            }
        )

    # 知识库目录只读；本工具不声明任何写入能力
    search_knowledge_base.metadata = {"effect": "read_only"}
    return (search_knowledge_base,)
