"""应用配置的加载与校验"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from numbers import Integral, Real
from pathlib import Path
from stat import S_ISDIR
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from dotenv import dotenv_values

VALID_LOG_LEVEL_ORDER = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
VALID_LOG_LEVELS = frozenset(VALID_LOG_LEVEL_ORDER)

TEMPERATURE_MINIMUM = 0
TEMPERATURE_MAXIMUM = 2
_TEMPERATURE_RANGE_DESCRIPTION = (
    f"a finite number between {TEMPERATURE_MINIMUM} and {TEMPERATURE_MAXIMUM}"
)
_UNSET = object()

VALID_REASONING_EFFORTS = ("low", "high", "max")
"""DeepSeek 思考模式官方支持的 reasoning_effort 取值"""

DEFAULT_REASONING_EFFORT = "high"
"""官方默认推理强度；仅在 thinking_enabled 时才会发送"""

DEFAULT_CONTEXT_TARGET_TOKENS = 32_000
"""V0.2 上下文压缩的软阈值，达到后触发压缩"""

DEFAULT_CONTEXT_HARD_LIMIT_TOKENS = 48_000
"""V0.2 硬上限，超过后只允许硬截断"""

DEFAULT_KEEP_RECENT_TURNS = 3
"""V0.2 压缩后保留的原始完整轮数"""

DEFAULT_MAX_STALL_ROUNDS = 3
"""V0.3 进度账本防卡死的连续重复轮数阈值"""

DEFAULT_WRITE_MAX_BYTES = 1024 * 1024
"""V0.4 单次写入内容的字节上限"""

DEFAULT_SHELL_TIMEOUT_SECONDS = 120
"""V0.5 命令执行默认超时（也是 timeout_seconds 参数的上限）"""

VALID_SHELL_APPROVALS = ("ask", "never")
"""审批配置只有两态：ask（交给决策者链）与 never（确定性拒绝）"""

_THINKING_VALUES = {"enabled": True, "disabled": False}

UNPRINTABLE_URL = "<redacted-url>"
"""无法安全解析时用于替代原始 URL 的固定占位符"""

_MIN_SECRET_FRAGMENT_LENGTH = 4
"""短于此长度的 URL 片段不参与脱敏，避免把普通文本大面积替换掉"""


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


def _require_string(name: str, value: object) -> str:
    """校验字符串非空并去除首尾空白"""
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(name, "must be a non-empty string")
    return value.strip()


def _require_integer(
    name: str,
    value: object,
    *,
    minimum: int,
    maximum: int | None = None,
    raw_value: object = _UNSET,
) -> int:
    """校验整数类型及其数值范围并返回规范化后的整数"""
    range_description = _integer_range_description(minimum, maximum)
    display_value = value if raw_value is _UNSET else raw_value
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ConfigurationError(
            name,
            f"must be {range_description}, got: {display_value!r}",
        )
    normalized = int(value)
    if normalized < minimum or (maximum is not None and normalized > maximum):
        raise ConfigurationError(
            name,
            f"must be {range_description}, got: {display_value!r}",
        )
    return normalized


def _integer_range_description(minimum: int, maximum: int | None) -> str:
    """根据整数边界生成唯一的范围描述"""
    if maximum is None:
        return f"an integer greater than or equal to {minimum}"
    return f"an integer between {minimum} and {maximum}"


def _require_temperature(value: object, *, raw_value: object = _UNSET) -> float:
    """校验 temperature 是允许范围内的有限数值"""
    display_value = value if raw_value is _UNSET else raw_value

    def invalid_temperature() -> ConfigurationError:
        return ConfigurationError(
            "temperature",
            f"must be {_TEMPERATURE_RANGE_DESCRIPTION}, got: {display_value!r}",
        )

    if isinstance(value, bool) or not isinstance(value, Real):
        raise invalid_temperature()
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise invalid_temperature() from error
    if (
        not math.isfinite(normalized)
        or not TEMPERATURE_MINIMUM <= normalized <= TEMPERATURE_MAXIMUM
    ):
        raise invalid_temperature()
    return normalized


def _require_workspace_root(value: object) -> Path:
    """校验工作目录路径存在且确实是目录并返回规范化路径"""
    if not isinstance(value, Path):
        raise ConfigurationError("workspace_root", "must be a path")

    workspace_root: Path | None = None
    try:
        workspace_root = value.expanduser().resolve()
        file_stat = workspace_root.stat()
    except FileNotFoundError as error:
        display_path = workspace_root if workspace_root is not None else value
        raise ConfigurationError(
            "workspace_root",
            f"does not exist: {display_path}",
        ) from error
    except NotADirectoryError as error:
        display_path = workspace_root if workspace_root is not None else value
        raise ConfigurationError(
            "workspace_root",
            f"is not a directory: {display_path}",
        ) from error
    except (OSError, RuntimeError) as error:
        display_path = workspace_root if workspace_root is not None else value
        raise ConfigurationError(
            "workspace_root",
            f"could not be accessed: {display_path}",
        ) from error
    if not S_ISDIR(file_stat.st_mode):
        raise ConfigurationError(
            "workspace_root",
            f"is not a directory: {workspace_root}",
        )
    return workspace_root


def _require_log_level(value: object) -> str:
    """校验日志级别并将其规范化为大写字符串"""
    normalized = _require_string("log_level", value).upper()
    if normalized not in VALID_LOG_LEVELS:
        allowed = ", ".join(VALID_LOG_LEVEL_ORDER)
        raise ConfigurationError("log_level", f"must be one of: {allowed}")
    return normalized


def _require_thinking_enabled(value: object) -> bool:
    """校验思考模式开关是真正的布尔值"""
    if not isinstance(value, bool):
        raise ConfigurationError("thinking_enabled", "must be a boolean")
    return value


def _require_reasoning_effort(value: object) -> str:
    """校验 reasoning_effort 并规范化为小写"""
    normalized = _require_string("reasoning_effort", value).lower()
    if normalized not in VALID_REASONING_EFFORTS:
        allowed = ", ".join(VALID_REASONING_EFFORTS)
        raise ConfigurationError("reasoning_effort", f"must be one of: {allowed}")
    return normalized


def _require_directory(value: object, field: str) -> Path:
    """校验路径存在且确实是目录并返回规范化路径"""
    if not isinstance(value, Path):
        raise ConfigurationError(field, "must be a path")
    try:
        resolved = value.expanduser().resolve()
        file_stat = resolved.stat()
    except (OSError, RuntimeError) as error:
        raise ConfigurationError(
            field,
            f"could not be accessed: {value}",
        ) from error
    if not S_ISDIR(file_stat.st_mode):
        raise ConfigurationError(field, f"is not a directory: {resolved}")
    return resolved


def _require_disjoint(first: Path, second: Path, field: str) -> None:
    """要求两个真实目录互不包含（任一方向嵌套都拒绝）"""
    for parent, child in ((first, second), (second, first)):
        try:
            common = os.path.commonpath(
                [os.path.normcase(str(parent)), os.path.normcase(str(child))]
            )
        except ValueError as error:  # 不同盘符视为不相交
            del error
            continue
        if common == os.path.normcase(str(parent)):
            raise ConfigurationError(
                field,
                "must be separate from the workspace root",
            )


def _split_url(url: str) -> SplitResult | None:
    """解析 URL，无法解析时返回 None 而不是抛出异常"""
    try:
        return urlsplit(url)
    except ValueError:
        return None


def safe_url_summary(url: str) -> str:
    """返回只含 scheme/host/port 的 URL 摘要

    丢弃 userinfo、路径、查询和片段，因此形如
    ``https://alice:sk-xxx@example.invalid/v1?token=yyy`` 的地址不会把代理凭据
    或查询参数中的 token 带进日志；无法解析的地址统一显示为固定占位符。
    """
    parts = _split_url(url)
    if parts is None or not parts.scheme or not parts.hostname:
        return UNPRINTABLE_URL
    try:
        port = parts.port
    except ValueError:
        return UNPRINTABLE_URL

    host = parts.hostname
    if ":" in host:
        host = f"[{host}]"
    if port is None:
        return f"{parts.scheme}://{host}"
    return f"{parts.scheme}://{host}:{port}"


def url_secret_fragments(url: str) -> tuple[str, ...]:
    """提取 URL 中可能作为凭据的片段，供日志脱敏使用

    覆盖 userinfo（用户名、密码以及完整的 ``user:password`` 串）、查询参数值
    和片段值。第三方库常把完整 URL 写进异常文本，把这些片段交给日志过滤器
    即可在 traceback 中一并抹掉。
    """
    parts = _split_url(url)
    if parts is None:
        return ()

    fragments: set[str] = set()
    if "@" in parts.netloc:
        userinfo = parts.netloc.rpartition("@")[0]
        fragments.add(userinfo)
        fragments.add(unquote(userinfo))
    if parts.username:
        fragments.add(parts.username)
        fragments.add(unquote(parts.username))
    if parts.password:
        fragments.add(parts.password)
        fragments.add(unquote(parts.password))
    for values in parse_qs(parts.query, keep_blank_values=True).values():
        for value in values:
            fragments.add(value)
            fragments.add(unquote(value))
    if parts.fragment:
        fragments.add(parts.fragment)
        fragments.add(unquote(parts.fragment))

    return tuple(
        sorted(
            fragment
            for fragment in fragments
            if len(fragment) >= _MIN_SECRET_FRAGMENT_LENGTH
        )
    )


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
    thinking_enabled: bool = False
    reasoning_effort: str = DEFAULT_REASONING_EFFORT
    summarizer_model_id: str | None = None
    summarizer_base_url: str | None = None
    summarizer_api_key: str | None = None
    context_target_tokens: int = DEFAULT_CONTEXT_TARGET_TOKENS
    context_hard_limit_tokens: int = DEFAULT_CONTEXT_HARD_LIMIT_TOKENS
    keep_recent_turns: int = DEFAULT_KEEP_RECENT_TURNS
    max_stall_rounds: int = DEFAULT_MAX_STALL_ROUNDS
    knowledge_base_root: Path | None = None
    write_max_bytes: int = DEFAULT_WRITE_MAX_BYTES
    shell_approval: str = "ask"
    shell_timeout_seconds: int = DEFAULT_SHELL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        """校验并规范化所有配置字段"""
        object.__setattr__(
            self,
            "api_key",
            _require_string("api_key", self.api_key),
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
        object.__setattr__(
            self,
            "thinking_enabled",
            _require_thinking_enabled(self.thinking_enabled),
        )
        object.__setattr__(
            self,
            "reasoning_effort",
            _require_reasoning_effort(self.reasoning_effort),
        )
        # V0.2 摘要模型缺省回落主模型；显式提供时沿用同一套字符串校验
        if self.summarizer_model_id is None:
            object.__setattr__(self, "summarizer_model_id", self.model_id)
        else:
            object.__setattr__(
                self,
                "summarizer_model_id",
                _require_string("summarizer_model_id", self.summarizer_model_id),
            )
        if self.summarizer_base_url is None:
            object.__setattr__(self, "summarizer_base_url", self.base_url)
        else:
            object.__setattr__(
                self,
                "summarizer_base_url",
                _require_string("summarizer_base_url", self.summarizer_base_url),
            )
        if self.summarizer_api_key is None:
            object.__setattr__(self, "summarizer_api_key", self.api_key)
        else:
            object.__setattr__(
                self,
                "summarizer_api_key",
                _require_string("summarizer_api_key", self.summarizer_api_key),
            )
        object.__setattr__(
            self,
            "context_target_tokens",
            _require_integer(
                "context_target_tokens",
                self.context_target_tokens,
                minimum=1000,
                maximum=1_000_000,
            ),
        )
        object.__setattr__(
            self,
            "context_hard_limit_tokens",
            _require_integer(
                "context_hard_limit_tokens",
                self.context_hard_limit_tokens,
                minimum=1000,
                maximum=2_000_000,
            ),
        )
        if self.context_hard_limit_tokens < self.context_target_tokens:
            raise ConfigurationError(
                "context_hard_limit_tokens",
                "must be greater than or equal to context_target_tokens",
            )
        object.__setattr__(
            self,
            "keep_recent_turns",
            _require_integer(
                "keep_recent_turns",
                self.keep_recent_turns,
                minimum=1,
                maximum=50,
            ),
        )
        object.__setattr__(
            self,
            "max_stall_rounds",
            _require_integer(
                "max_stall_rounds",
                self.max_stall_rounds,
                minimum=2,
                maximum=5,
            ),
        )
        if self.knowledge_base_root is not None:
            knowledge_root = _require_directory(
                self.knowledge_base_root,
                "knowledge_base_root",
            )
            # 知识库目录只读且必须与工作目录分离，任一方向嵌套都拒绝
            _require_disjoint(
                self.workspace_root,
                knowledge_root,
                "knowledge_base_root",
            )
            object.__setattr__(self, "knowledge_base_root", knowledge_root)
        object.__setattr__(
            self,
            "write_max_bytes",
            _require_integer(
                "write_max_bytes",
                self.write_max_bytes,
                minimum=1,
                maximum=16 * 1024 * 1024,
            ),
        )
        if self.shell_approval not in VALID_SHELL_APPROVALS:
            raise ConfigurationError(
                "shell_approval",
                f"must be one of: {', '.join(VALID_SHELL_APPROVALS)}",
            )
        object.__setattr__(
            self,
            "shell_timeout_seconds",
            _require_integer(
                "shell_timeout_seconds",
                self.shell_timeout_seconds,
                minimum=5,
                maximum=600,
            ),
        )

    def safe_summary(self) -> dict[str, object]:
        """返回不含 API key 和 URL 凭据的启动诊断摘要

        ``base_url`` 只保留 scheme/host/port，避免 userinfo 或查询参数中的
        token 通过诊断信息回显。
        """
        return {
            "model_id": self.model_id,
            "base_url": safe_url_summary(self.base_url),
            "request_timeout_seconds": self.request_timeout_seconds,
            "max_tool_rounds": self.max_tool_rounds,
            "temperature": self.temperature,
            "workspace_root": str(self.workspace_root),
            "log_level": self.log_level,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
            "summarizer_model_id": self.summarizer_model_id,
            "summarizer_base_url": safe_url_summary(self.summarizer_base_url),
            "context_target_tokens": self.context_target_tokens,
            "context_hard_limit_tokens": self.context_hard_limit_tokens,
            "keep_recent_turns": self.keep_recent_turns,
            "max_stall_rounds": self.max_stall_rounds,
            "knowledge_base_root": (
                str(self.knowledge_base_root)
                if self.knowledge_base_root is not None
                else None
            ),
            "write_max_bytes": self.write_max_bytes,
            "shell_approval": self.shell_approval,
            "shell_timeout_seconds": self.shell_timeout_seconds,
            "api_key_configured": True,
        }


CURRENT_FILE = Path(__file__).resolve()
PROJECT_ROOT = CURRENT_FILE.parent.parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def _read_required(environment: Mapping[str, str | None], name: str) -> str:
    """读取必填字符串配置并拒绝缺失、空字符串和纯空白值"""
    raw_value = environment.get(name)
    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        raise ConfigurationError(name, "is required")
    if not isinstance(raw_value, str):
        raise ConfigurationError(name, "must be a non-empty string")
    return raw_value.strip()


def _read_optional(
    environment: Mapping[str, str | None],
    name: str,
    default: str,
) -> str:
    """读取可选字符串配置并在缺失时返回默认值"""
    raw_value = environment.get(name)
    if raw_value is None:
        raw_value = default
    if not isinstance(raw_value, str):
        raise ConfigurationError(name, "must be a string")
    return raw_value.strip()


def _read_defaulted_required(
    environment: Mapping[str, str | None],
    name: str,
    default: str,
) -> str:
    """读取带默认值的必填字符串并拒绝最终为空的值"""
    raw_value = _read_optional(environment, name, default)
    if not raw_value:
        raise ConfigurationError(name, "is required")
    return raw_value


def _parse_integer(
    environment: Mapping[str, str | None],
    name: str,
    default: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """读取环境配置，将其解析为整数并校验指定的数值范围

    解析或校验失败时抛出结构化且便于阅读的 ConfigurationError
    """
    raw_value = _read_optional(environment, name, default)
    try:
        normalized = int(raw_value)
    except ValueError as error:
        range_description = _integer_range_description(minimum, maximum)
        raise ConfigurationError(
            name,
            f"must be {range_description}, got: {raw_value!r}",
        ) from error
    return _require_integer(
        name,
        normalized,
        minimum=minimum,
        maximum=maximum,
        raw_value=raw_value,
    )


def _parse_temperature(environment: Mapping[str, str | None]) -> float:
    """读取环境中的 temperature，解析为浮点数并校验其范围"""
    raw_value = _read_optional(environment, "CHAT_MODEL_TEMPERATURE", "0")
    try:
        normalized = float(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            "CHAT_MODEL_TEMPERATURE",
            f"must be {_TEMPERATURE_RANGE_DESCRIPTION}, got: {raw_value!r}",
        ) from error
    try:
        return _require_temperature(normalized, raw_value=raw_value)
    except ConfigurationError as error:
        raise error.renamed("CHAT_MODEL_TEMPERATURE") from None


def _parse_workspace_root(environment: Mapping[str, str | None]) -> Path:
    """读取环境中的工作目录路径，未配置时回退到项目根目录"""
    raw_value = _read_optional(environment, "WORKSPACE_ROOT", "")
    return Path(raw_value) if raw_value else PROJECT_ROOT


def _parse_thinking_enabled(environment: Mapping[str, str | None]) -> bool:
    """读取显式思考模式开关；默认关闭以保留 OpenAI 兼容原路径"""
    raw_value = _read_optional(environment, "CHAT_MODEL_THINKING", "disabled")
    normalized = raw_value.lower()
    if normalized not in _THINKING_VALUES:
        raise ConfigurationError(
            "CHAT_MODEL_THINKING",
            "must be one of: enabled, disabled",
        )
    return _THINKING_VALUES[normalized]


def _parse_reasoning_effort(environment: Mapping[str, str | None]) -> str:
    """读取思考模式推理强度并校验官方取值"""
    raw_value = _read_optional(
        environment,
        "CHAT_MODEL_REASONING_EFFORT",
        DEFAULT_REASONING_EFFORT,
    )
    normalized = raw_value.lower()
    if normalized not in VALID_REASONING_EFFORTS:
        allowed = ", ".join(VALID_REASONING_EFFORTS)
        raise ConfigurationError(
            "CHAT_MODEL_REASONING_EFFORT",
            f"must be one of: {allowed}, got: {raw_value!r}",
        )
    return normalized


def _parse_optional_secret(
    environment: Mapping[str, str | None],
    name: str,
) -> str | None:
    """读取可选密钥配置；显式提供的空值视为未配置"""
    raw_value = environment.get(name)
    if raw_value is None or not raw_value.strip():
        return None
    if not isinstance(raw_value, str):
        raise ConfigurationError(name, "must be a string")
    return raw_value.strip()


def _parse_knowledge_base_root(
    environment: Mapping[str, str | None],
) -> Path | None:
    """读取可选知识库目录；未配置时返回 None（工具不注册）"""
    raw_value = _read_optional(environment, "KNOWLEDGE_BASE_ROOT", "")
    if not raw_value:
        return None
    return Path(raw_value)


def _parse_shell_approval(environment: Mapping[str, str | None]) -> str:
    """读取命令审批两态配置；默认 ask（交给决策者链）"""
    raw_value = _read_optional(environment, "SHELL_APPROVAL", "ask").lower()
    if raw_value not in VALID_SHELL_APPROVALS:
        raise ConfigurationError(
            "SHELL_APPROVAL",
            f"must be one of: {', '.join(VALID_SHELL_APPROVALS)}, got: {raw_value!r}",
        )
    return raw_value


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

    try:
        return AgentConfig(
            api_key=_read_required(merged, "CHAT_MODEL_API_KEY"),
            model_id=_read_defaulted_required(
                merged,
                "CHAT_MODEL_ID",
                "deepseek-v4-flash",
            ),
            base_url=_read_defaulted_required(
                merged,
                "CHAT_MODEL_BASE_URL",
                "https://api.deepseek.com",
            ),
            request_timeout_seconds=_parse_integer(
                merged,
                "CHAT_MODEL_REQUEST_TIMEOUT_SECONDS",
                "60",
                minimum=1,
            ),
            max_tool_rounds=_parse_integer(
                merged,
                "MAX_TOOL_ROUNDS",
                "4",
                minimum=1,
                maximum=10,
            ),
            temperature=_parse_temperature(merged),
            workspace_root=_parse_workspace_root(merged),
            log_level=_read_optional(merged, "LOG_LEVEL", "INFO"),
            thinking_enabled=_parse_thinking_enabled(merged),
            reasoning_effort=_parse_reasoning_effort(merged),
            summarizer_model_id=(
                _read_optional(merged, "SUMMARIZER_MODEL_ID", "") or None
            ),
            summarizer_base_url=(
                _read_optional(merged, "SUMMARIZER_MODEL_BASE_URL", "") or None
            ),
            summarizer_api_key=_parse_optional_secret(
                merged,
                "SUMMARIZER_MODEL_API_KEY",
            ),
            context_target_tokens=_parse_integer(
                merged,
                "CONTEXT_TARGET_TOKENS",
                str(DEFAULT_CONTEXT_TARGET_TOKENS),
                minimum=1000,
                maximum=1_000_000,
            ),
            context_hard_limit_tokens=_parse_integer(
                merged,
                "CONTEXT_HARD_LIMIT_TOKENS",
                str(DEFAULT_CONTEXT_HARD_LIMIT_TOKENS),
                minimum=1000,
                maximum=2_000_000,
            ),
            keep_recent_turns=_parse_integer(
                merged,
                "KEEP_RECENT_TURNS",
                str(DEFAULT_KEEP_RECENT_TURNS),
                minimum=1,
                maximum=50,
            ),
            max_stall_rounds=_parse_integer(
                merged,
                "MAX_STALL_ROUNDS",
                str(DEFAULT_MAX_STALL_ROUNDS),
                minimum=2,
                maximum=5,
            ),
            knowledge_base_root=_parse_knowledge_base_root(merged),
            write_max_bytes=_parse_integer(
                merged,
                "WRITE_MAX_BYTES",
                str(DEFAULT_WRITE_MAX_BYTES),
                minimum=1,
                maximum=16 * 1024 * 1024,
            ),
            shell_approval=_parse_shell_approval(merged),
            shell_timeout_seconds=_parse_integer(
                merged,
                "SHELL_TIMEOUT_SECONDS",
                str(DEFAULT_SHELL_TIMEOUT_SECONDS),
                minimum=5,
                maximum=600,
            ),
        )
    except ConfigurationError as error:
        field_names = {
            "api_key": "CHAT_MODEL_API_KEY",
            "model_id": "CHAT_MODEL_ID",
            "base_url": "CHAT_MODEL_BASE_URL",
            "request_timeout_seconds": "CHAT_MODEL_REQUEST_TIMEOUT_SECONDS",
            "max_tool_rounds": "MAX_TOOL_ROUNDS",
            "temperature": "CHAT_MODEL_TEMPERATURE",
            "workspace_root": "WORKSPACE_ROOT",
            "log_level": "LOG_LEVEL",
            "thinking_enabled": "CHAT_MODEL_THINKING",
            "reasoning_effort": "CHAT_MODEL_REASONING_EFFORT",
            "summarizer_model_id": "SUMMARIZER_MODEL_ID",
            "summarizer_base_url": "SUMMARIZER_MODEL_BASE_URL",
            "summarizer_api_key": "SUMMARIZER_MODEL_API_KEY",
            "context_target_tokens": "CONTEXT_TARGET_TOKENS",
            "context_hard_limit_tokens": "CONTEXT_HARD_LIMIT_TOKENS",
            "keep_recent_turns": "KEEP_RECENT_TURNS",
            "max_stall_rounds": "MAX_STALL_ROUNDS",
            "knowledge_base_root": "KNOWLEDGE_BASE_ROOT",
            "write_max_bytes": "WRITE_MAX_BYTES",
            "shell_approval": "SHELL_APPROVAL",
            "shell_timeout_seconds": "SHELL_TIMEOUT_SECONDS",
        }
        public_field = field_names.get(error.field, error.field)
        if public_field == error.field:
            raise
        raise error.renamed(public_field) from None
