"""Anthropic 自动埋点。

与 OpenAI 版同构：显式包装 messages.create 这一公开方法。

用法：
    from anthropic import Anthropic
    from ariadne_sdk.instrument.anthropic import wrap_anthropic

    client = wrap_anthropic(Anthropic())
"""

from __future__ import annotations

import contextlib
import functools
from typing import Any, TypeVar

from ariadne_sdk.client import get_client
from ariadne_sdk.instrument.common import (
    extract_anthropic_usage,
    preview_messages,
    preview_output,
)

T = TypeVar("T")

_PROVIDER = "anthropic"


def _instrument_create(original: Any) -> Any:
    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        client = get_client()
        if client is None:
            return original(*args, **kwargs)

        model = str(kwargs.get("model", ""))
        span = client.span(
            f"chat {model}".strip(),
            kind="llm",
            operation="chat",
            provider=_PROVIDER,
            model=model,
        )
        with span:
            span.set_attributes(
                {
                    "temperature": kwargs.get("temperature"),
                    "max_tokens": kwargs.get("max_tokens"),
                    "stream": kwargs.get("stream", False),
                    # system prompt 单独记长度，便于判断上下文构成
                    "system_len": len(str(kwargs.get("system", ""))) or None,
                }
            )
            if messages := kwargs.get("messages"):
                span.set_input(preview_messages(messages))

            response = original(*args, **kwargs)

            if kwargs.get("stream"):
                span.set_attributes({"usage_unavailable": "stream"})
                return response

            span.set_usage(**extract_anthropic_usage(response))
            actual_model = getattr(response, "model", "") or model
            span.set_model(model, response_model=str(actual_model), provider=_PROVIDER)
            span.set_output(preview_output(response))
            if stop_reason := getattr(response, "stop_reason", ""):
                span.set_attributes({"finish_reason": str(stop_reason)})
            return response

    return wrapper


def wrap_anthropic(client: T) -> T:
    """就地包装 Anthropic 客户端。幂等。"""
    messages = getattr(client, "messages", None)
    if messages is None:
        return client

    create = getattr(messages, "create", None)
    if create is None or getattr(create, "_ariadne_wrapped", False):
        return client

    wrapped = _instrument_create(create)
    wrapped._ariadne_wrapped = True
    # 某些版本用 __slots__ 或只读属性，跳过而非崩掉业务
    with contextlib.suppress(AttributeError, TypeError):
        messages.create = wrapped
    return client
