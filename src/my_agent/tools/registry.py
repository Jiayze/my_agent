"""显式构造的 LangChain 工具的不可变注册表"""

from collections.abc import Iterable, Iterator

from langchain_core.tools import BaseTool


class ToolRegistryError(ValueError):
    """工具注册表相关的领域错误基类"""


class DuplicateToolNameError(ToolRegistryError):
    """注册表中出现重复工具名时抛出"""


class UnknownToolError(ToolRegistryError):
    """按名称查找未注册工具时抛出"""


class ToolRegistry:
    """按名称索引的不可变工具集合

    每个实例持有自己的工具列表；测试应各自新建实例，
    不共享任何模块级可变全局状态
    """

    def __init__(self, tools: Iterable[BaseTool]) -> None:
        """按注册顺序保存工具并校验名称唯一且格式有效"""
        self._tools = tuple(tools)
        by_name: dict[str, BaseTool] = {}
        for tool in self._tools:
            if not isinstance(tool, BaseTool):
                raise ToolRegistryError(
                    "every registered tool must be a langchain BaseTool, "
                    f"got: {type(tool).__name__}"
                )
            name = tool.name
            if not isinstance(name, str) or not name.strip():
                raise ToolRegistryError(
                    "every registered tool must have a non-empty string name, "
                    f"got: {name!r}"
                )
            if name != name.strip():
                raise ToolRegistryError(
                    f"tool names must not contain surrounding whitespace: {name!r}"
                )
            if name in by_name:
                raise DuplicateToolNameError(f"duplicate tool name: {name!r}")
            by_name[name] = tool
        self._by_name = by_name

    def __iter__(self) -> Iterator[BaseTool]:
        """按注册顺序迭代工具"""
        return iter(self._tools)

    def __len__(self) -> int:
        """返回已注册工具的数量"""
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        """判断指定名称是否已注册"""
        return isinstance(name, str) and name in self._by_name

    def get_tools(self) -> tuple[BaseTool, ...]:
        """返回按注册顺序排列的不可变工具序列"""
        return self._tools

    def get_tool_names(self) -> frozenset[str]:
        """返回已注册工具名称的不可变集合"""
        return frozenset(self._by_name)

    def get_tool(self, name: str) -> BaseTool:
        """按名称查找工具；未注册名称抛出 UnknownToolError"""
        if not isinstance(name, str):
            raise TypeError("tool name must be a string")
        try:
            return self._by_name[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool: {name!r}") from None
