"""my_agent 公共工具接口"""

from my_agent.tools.builtin import add_numbers
from my_agent.tools.registry import (
    DuplicateToolNameError,
    ToolRegistry,
    ToolRegistryError,
    UnknownToolError,
)


def get_tools() -> ToolRegistry:
    """返回第一版 CLI 使用的工具注册表"""
    return ToolRegistry([add_numbers])


__all__ = [
    "DuplicateToolNameError",
    "ToolRegistry",
    "ToolRegistryError",
    "UnknownToolError",
    "add_numbers",
    "get_tools",
]
