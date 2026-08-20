"""第一版最小工具注册表。"""

from collections.abc import Iterable

from langchain_core.tools import BaseTool


class ToolRegistryError(ValueError):
    """工具注册表相关的领域错误基类。"""


class DuplicateToolNameError(ToolRegistryError):
    """注册表中出现重复工具名时抛出。"""


class UnknownToolError(ToolRegistryError):
    """按名称查找未注册工具时抛出。"""


class ToolRegistry:
    """按名称索引的不可变工具集合。

    每个实例持有自己的工具列表；测试应各自新建实例，
    不共享任何模块级可变全局状态。
    """

    def __init__(self, tools: Iterable[BaseTool]) -> None:
        self._tools = tuple(tools)
        by_name: dict[str, BaseTool] = {}
        for tool in self._tools:
            name = getattr(tool, "name", None)
            if not isinstance(name, str) or not name.strip():
                raise ToolRegistryError(
                    "every registered tool must have a non-empty string name, "
                    f"got: {name!r}"
                )
            if name in by_name:
                raise DuplicateToolNameError(f"duplicate tool name: {name!r}")
            by_name[name] = tool
        self._by_name = by_name

    def get_tools(self) -> list[BaseTool]:
        """按注册顺序返回工具列表的副本。"""
        return list(self._tools)

    def get_tool_names(self) -> set[str]:
        """返回已注册工具名的集合副本。"""
        return set(self._by_name)

    def get_tool(self, name: str) -> BaseTool:
        """按名称查找工具；未注册名称抛出 UnknownToolError。"""
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool: {name!r}") from None
