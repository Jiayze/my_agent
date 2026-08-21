"""M3 工具系统测试：内置工具与注册表。"""

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from my_agent.tools import (
    DuplicateToolNameError,
    ToolRegistry,
    UnknownToolError,
    add_numbers,
)


@tool
def multiply_numbers(left: float, right: float) -> float:
    """计算两个数字之积。"""
    return left * right


# ---------- 内置工具 ----------


def test_add_numbers_returns_correct_sum() -> None:
    assert add_numbers.invoke({"left": 2, "right": 3}) == 5.0


def test_add_numbers_handles_negative_and_float_values() -> None:
    assert add_numbers.invoke({"left": -1.5, "right": 0.5}) == -1.0
    assert add_numbers.invoke({"left": 0, "right": 0}) == 0.0


def test_add_numbers_missing_argument_raises_structured_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        add_numbers.invoke({"left": 2})

    assert "right" in str(exc_info.value)


def test_add_numbers_wrong_type_raises_structured_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        add_numbers.invoke({"left": "not-a-number", "right": 2})

    assert "left" in str(exc_info.value)


def test_add_numbers_has_name_and_description() -> None:
    assert add_numbers.name == "add_numbers"
    assert isinstance(add_numbers.description, str)
    assert "计算两个数字之和" in add_numbers.description


def test_add_numbers_exposes_valid_tool_schema() -> None:
    """bind_tools 会使用同一 schema；离线验证其可绑定性。"""
    schema = add_numbers.tool_call_schema.model_json_schema()

    assert schema["type"] == "object"
    assert set(schema["required"]) == {"left", "right"}
    assert schema["properties"]["left"]["type"] == "number"
    assert schema["properties"]["right"]["type"] == "number"


# ---------- 注册表 ----------


def test_registry_returns_tools_in_registration_order() -> None:
    registry = ToolRegistry([add_numbers, multiply_numbers])

    assert [t.name for t in registry.get_tools()] == [
        "add_numbers",
        "multiply_numbers",
    ]


def test_registry_tool_names_and_lookup() -> None:
    registry = ToolRegistry([add_numbers, multiply_numbers])

    assert registry.get_tool_names() == {"add_numbers", "multiply_numbers"}
    assert registry.get_tool("add_numbers") is add_numbers
    assert registry.get_tool("multiply_numbers") is multiply_numbers


def test_registry_rejects_duplicate_tool_names() -> None:
    with pytest.raises(DuplicateToolNameError) as exc_info:
        ToolRegistry([add_numbers, add_numbers])

    assert "add_numbers" in str(exc_info.value)


def test_registry_rejects_unknown_tool() -> None:
    registry = ToolRegistry([add_numbers])

    with pytest.raises(UnknownToolError) as exc_info:
        registry.get_tool("does_not_exist")

    assert "does_not_exist" in str(exc_info.value)


def test_registry_rejects_non_string_tool_name_on_lookup() -> None:
    registry = ToolRegistry([add_numbers])

    with pytest.raises(UnknownToolError):
        registry.get_tool(["add_numbers"])  # type: ignore[arg-type]


def test_registry_get_tools_returns_a_copy() -> None:
    registry = ToolRegistry([add_numbers])

    tools = registry.get_tools()
    tools.clear()

    assert len(registry.get_tools()) == 1


def test_registry_get_tool_names_returns_a_copy() -> None:
    registry = ToolRegistry([add_numbers])

    names = registry.get_tool_names()
    names.clear()

    assert registry.get_tool_names() == {"add_numbers"}


def test_registry_instances_do_not_share_state() -> None:
    first = ToolRegistry([add_numbers])
    second = ToolRegistry([multiply_numbers])

    assert first.get_tool_names() == {"add_numbers"}
    assert second.get_tool_names() == {"multiply_numbers"}
