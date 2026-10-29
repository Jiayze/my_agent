"""第一批内置工具：完全确定、无副作用、无需网络"""

from langchain_core.tools import tool


@tool
def add_numbers(left: float, right: float) -> float:
    """计算两个数字之和"""
    return left + right


# 显式声明能力等级；Agent 拒绝缺失该元数据的工具，不做 read_only 缺省
add_numbers.metadata = {"effect": "read_only"}
