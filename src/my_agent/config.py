"""应用配置的加载与校验"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Integral, Real
from pathlib import Path

from dotenv import dotenv_values

VALID_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class ConfigurationError(ValueError):
    """配置字段违反公开契约时抛出的错误"""

    def __init__(self, field: str, reason: str) -> None:
        """使用字段名和失败原因初始化配置错误"""
        self.field = field
        self.reason = reason
        super().__init__(f"{field} {reason}")

    def renamed(self, field: str) -> ConfigurationError:
        """使用调用方可见的字段名创建同一个配置失败"""
        return type(self)(field, self.reason)


def _require_string(name: str, value: object, *, secret: bool = False) -> str:
    """校验字符串非空并去除首尾空白"""
    if not isinstance(value, str) or not value.strip():
        suffix = " must be configured" if secret else " must be a non-empty string"
        raise ConfigurationError(name, suffix.strip())
    return value.strip()


def _require_integer(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """校验整数类型及其数值范围并返回规范化后的整数"""
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ConfigurationError(name, "must be an integer")
    normalized = int(value)
    if normalized < minimum or (maximum is not None and normalized > maximum):
        if maximum is None:
            raise ConfigurationError(
                name,
                f"must be greater than or equal to {minimum}",
            )
        raise ConfigurationError(name, f"must be between {minimum} and {maximum}")
    return normalized


def _require_temperature(value: object) -> float:
    """校验 temperature 是 0 到 2 之间的有限数值"""
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ConfigurationError(
            "temperature",
            "must be a finite number between 0 and 2",
        )
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ConfigurationError(
            "temperature",
            "must be a finite number between 0 and 2",
        ) from error
    if not math.isfinite(normalized) or not 0 <= normalized <= 2:
        raise ConfigurationError(
            "temperature",
            "must be a finite number between 0 and 2",
        )
    return normalized


def _require_workspace_root(value: object) -> Path:
    """校验工作目录路径存在且确实是目录并返回规范化路径"""
    if not isinstance(value, (str, os.PathLike)):
        raise ConfigurationError("workspace_root", "must be a path")
    try:
        workspace_root = Path(value).expanduser().resolve()
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            "workspace_root",
            "could not be resolved",
        ) from error
    if not workspace_root.exists():
        raise ConfigurationError(
            "workspace_root",
            f"does not exist: {workspace_root}",
        )
    if not workspace_root.is_dir():
        raise ConfigurationError(
            "workspace_root",
            f"is not a directory: {workspace_root}",
        )
    return workspace_root


def _require_log_level(value: object) -> str:
    """校验日志级别并将其规范化为大写字符串"""
    normalized = _require_string("log_level", value).upper()
    if normalized not in VALID_LOG_LEVELS:
        allowed = ", ".join(sorted(VALID_LOG_LEVELS))
        raise ConfigurationError("log_level", f"must be one of: {allowed}")
    return normalized


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """经过校验且不可变的应用配置

    直接构造和从环境加载遵循同一套校验契约，
    因此下游组件可以信任每个实例都已完成规范化
    """

    api_key: str
    model_id: str
    base_url: str
    request_timeout_seconds: int
    max_tool_rounds: int
    temperature: float
    workspace_root: Path
    log_level: str

    def __post_init__(self) -> None:
        """校验并规范化所有配置字段"""
        object.__setattr__(
            self,
            "api_key",
            _require_string("api_key", self.api_key, secret=True),
        )
        object.__setattr__(self, "model_id", _require_string("model_id", self.model_id))
        object.__setattr__(self, "base_url", _require_string("base_url", self.base_url))
        object.__setattr__(
            self,
            "request_timeout_seconds",
            _require_integer(
                "request_timeout_seconds",
                self.request_timeout_seconds,
                minimum=1,
            ),
        )
        object.__setattr__(
            self,
            "max_tool_rounds",
            _require_integer(
                "max_tool_rounds",
                self.max_tool_rounds,
                minimum=1,
                maximum=10,
            ),
        )
        object.__setattr__(self, "temperature", _require_temperature(self.temperature))
        object.__setattr__(
            self,
            "workspace_root",
            _require_workspace_root(self.workspace_root),
        )
        object.__setattr__(self, "log_level", _require_log_level(self.log_level))

    def safe_summary(self) -> dict[str, object]:
        """返回不包含 API key 的启动诊断摘要"""
        return {
            "model_id": self.model_id,
            "base_url": self.base_url,
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_tool_rounds": self.max_tool_rounds,
            "temperature": self.temperature,
            "workspace_root": str(self.workspace_root),
            "log_level": self.log_level,
            "api_key_configured": True,
        }


CURRENT_FILE = Path(__file__).resolve()
PROJECT_ROOT = CURRENT_FILE.parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def _read_required(environment: Mapping[str, str | None], name: str) -> str:
    """读取必填字符串配置并拒绝缺失、空字符串和纯空白值"""
    raw_value = environment.get(name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigurationError(name, "is required")
    return raw_value


def _read_optional(
    environment: Mapping[str, str | None],
    name: str,
    default: str,
) -> str:
    """读取可选字符串配置并在缺失时返回默认值"""
    raw_value = environment.get(name, default)
    if not isinstance(raw_value, str):
        raise ConfigurationError(name, "must be a string")
    return raw_value


def _read_defaulted_required(
    environment: Mapping[str, str | None],
    name: str,
    default: str,
) -> str:
    """读取带默认值的必填字符串并拒绝最终为空的值"""
    raw_value = _read_optional(environment, name, default)
    if not raw_value.strip():
        raise ConfigurationError(name, "is required")
    return raw_value


def _parse_integer(
    environment: Mapping[str, str | None],
    name: str,
    default: str,
    *,
    minimum: int,
    maximum: int | None = None,
    range_description: str,
) -> int:
    """读取环境配置，将其解析为整数并校验指定的数值范围

    解析或校验失败时抛出结构化且便于阅读的 ConfigurationError
    """
    raw_value = _read_optional(environment, name, default).strip()
    try:
        normalized = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            name,
            f"must be {range_description}, got: {raw_value!r}",
        ) from error
    try:
        return _require_integer(
            name,
            normalized,
            minimum=minimum,
            maximum=maximum,
        )
    except ConfigurationError as error:
        raise ConfigurationError(
            name,
            f"must be {range_description}, got: {raw_value!r}",
        ) from error


def _parse_temperature(environment: Mapping[str, str | None]) -> float:
    """读取环境中的 temperature，解析为浮点数并校验其范围"""
    raw_value = _read_optional(environment, "TEMPERATURE", "0").strip()
    try:
        normalized = float(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            "TEMPERATURE",
            f"must be a finite number between 0 and 2, got: {raw_value!r}",
        ) from error
    try:
        return _require_temperature(normalized)
    except ConfigurationError as error:
        raise ConfigurationError(
            "TEMPERATURE",
            f"must be a finite number between 0 and 2, got: {raw_value!r}",
        ) from error


def load_config(
    env_file: str | Path | None = DEFAULT_ENV_PATH,
    *,
    environ: Mapping[str, str | None] | None = None,
) -> AgentConfig:
    """加载配置且不修改当前进程的环境变量

    显式传入的 environ 优先于 dotenv 文件中的值
    传入 env_file=None 可禁用 dotenv 加载，便于测试和嵌入式调用隔离环境
    """
    merged: dict[str, str | None] = {}
    if env_file is not None:
        merged.update(dotenv_values(dotenv_path=env_file, interpolate=False))
    merged.update(os.environ if environ is None else environ)

    raw_workspace_root = _read_optional(merged, "WORKSPACE_ROOT", "").strip()
    workspace_root = raw_workspace_root or str(PROJECT_ROOT)

    try:
        return AgentConfig(
            api_key=_read_required(merged, "DEEPSEEK_API_KEY"),
            model_id=_read_defaulted_required(merged, "MODEL_ID", "deepseek-v4-flash"),
            base_url=_read_defaulted_required(
                merged,
                "BASE_URL",
                "https://api.deepseek.com",
            ),
            request_timeout_seconds=_parse_integer(
                merged,
                "REQUEST_TIMEOUT_SECONDS",
                "60",
                minimum=1,
                range_description="an integer greater than 0",
            ),
            max_tool_rounds=_parse_integer(
                merged,
                "MAX_TOOL_ROUNDS",
                "4",
                minimum=1,
                maximum=10,
                range_description="an integer between 1 and 10",
            ),
            temperature=_parse_temperature(merged),
            workspace_root=Path(workspace_root),
            log_level=_read_optional(merged, "LOG_LEVEL", "INFO"),
        )
    except ConfigurationError as error:
        field_names = {
            "api_key": "DEEPSEEK_API_KEY",
            "model_id": "MODEL_ID",
            "base_url": "BASE_URL",
            "request_timeout_seconds": "REQUEST_TIMEOUT_SECONDS",
            "max_tool_rounds": "MAX_TOOL_ROUNDS",
            "temperature": "TEMPERATURE",
            "workspace_root": "WORKSPACE_ROOT",
            "log_level": "LOG_LEVEL",
        }
        public_field = field_names.get(error.field, error.field)
        if public_field == error.field:
            raise
        raise error.renamed(public_field) from error
