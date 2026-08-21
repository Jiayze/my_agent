"""第一批内置工具：完全确定、无副作用、无需网络。"""

from langchain_core.tools import tool


@tool
def add_numbers(left: float, right: float) -> float:
    """计算两个数字之和。"""
    return left + right
