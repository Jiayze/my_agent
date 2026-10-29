"""标准库日志配置与密钥脱敏"""

from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import Iterable
from typing import TextIO

REDACTED = "[REDACTED]"
HANDLER_MARKER = "_my_agent_handler"


def _normalize_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    """去重并按长度降序排列，保证长秘密先于其子串被替换"""
    unique = {secret for secret in secrets if isinstance(secret, str) and secret}
    return tuple(sorted(unique, key=len, reverse=True))


def redact_text(text: str, secrets: Iterable[str]) -> str:
    """将文本中出现的已知秘密替换为固定占位符

    供日志过滤器与进程边界的兜底输出共用。
    """
    for secret in _normalize_secrets(secrets):
        text = text.replace(secret, REDACTED)
    return text


class SecretRedactionFilter(logging.Filter):
    """在日志真正输出前替换已知密钥，作为最后一道保险

    正常错误路径不会把密钥写进日志；本过滤器只处理遗漏或第三方库
    把密钥带进消息、异常文本的情况。
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """保存需要脱敏的密钥，忽略空值"""
        super().__init__()
        self._secrets = _normalize_secrets(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        """就地脱敏消息、异常文本和堆栈信息后放行"""
        if not self._secrets:
            return True

        message = record.getMessage()
        redacted = redact_text(message, self._secrets)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        if record.exc_info is not None and not record.exc_text:
            record.exc_text = redact_text(
                "".join(traceback.format_exception(*record.exc_info)),
                self._secrets,
            )
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact_text(record.exc_text, self._secrets)

        if record.stack_info:
            record.stack_info = redact_text(record.stack_info, self._secrets)
        return True


def configure_logging(
    level: str,
    *,
    secret: str | None = None,
    secrets: Iterable[str] = (),
    stream: TextIO | None = None,
) -> logging.Handler:
    """按配置设置根日志级别并安装带密钥过滤的 handler

    ``secret`` 是主要 API key；``secrets`` 用于补充 URL userinfo、查询参数等
    其他敏感片段。重复调用会先移除本模块安装的旧 handler，避免测试或多入口
    重复输出。日志默认写入 stderr，stdout 始终留给用户可见的对话内容。
    """
    normalized = str(level).upper()
    numeric_level = logging.getLevelName(normalized)
    if not isinstance(numeric_level, int):
        raise ValueError(f"unknown log level: {level!r}")

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(sys.stderr if stream is None else stream)
    handler.setLevel(numeric_level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    all_secrets = list(secrets)
    if secret:
        all_secrets.append(secret)
    # 始终安装过滤器，保证 DEBUG 的 exc_info 也走同一条脱敏路径
    handler.addFilter(SecretRedactionFilter(all_secrets))
    setattr(handler, HANDLER_MARKER, True)

    root.setLevel(numeric_level)
    root.addHandler(handler)
    return handler
