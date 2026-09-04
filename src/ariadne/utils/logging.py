"""结构化 JSON 日志。

强制携带 trace_id / span_id / loop_id / project_id（存在时），
以实现日志与 trace 的双向跳转。禁止在日志里输出完整 prompt/completion：
体积大且含隐私，只记哈希与长度。
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

# 这些是 LogRecord 的内建字段，其余 extra 字段才是业务上下文
_RESERVED: Final = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "module", "msecs",
        "message", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName", "taskName",
    }
)

_CORRELATION_KEYS: Final = ("trace_id", "span_id", "loop_id", "project_id")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        # 关联字段前置，便于人眼扫读
        ordered = {k: payload.pop(k) for k in _CORRELATION_KEYS if k in payload}
        return json.dumps({**payload, **ordered}, ensure_ascii=False, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """幂等地配置根 logger。多次调用不会叠加 handler。"""
    root = logging.getLogger()
    root.setLevel(level.upper())

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
        )
    root.addHandler(handler)

    # 第三方库降噪
    for noisy in ("httpx", "httpcore", "urllib3", "clickhouse_connect"):
        logging.getLogger(noisy).setLevel("WARNING")


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
