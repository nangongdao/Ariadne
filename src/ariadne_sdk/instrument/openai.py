"""OpenAI 自动埋点。

采用**显式包装公开方法**而非全局 monkey patch 私有属性：
后者在上游小版本升级时必碎，且会污染同进程内其他库的调用。

用法：
    from openai import OpenAI
    from ariadne_sdk.instrument.openai import wrap_openai

    client = wrap_openai(OpenAI())
    client.chat.completions.create(...)   # 自动产生 span
"""

from __future__ import annotations

import functools
from typing import Any, TypeVar

from ariadne_sdk.client import get_client
from ariadne_sdk.instrument.common import (
    extract_openai_usage,
    preview_messages,
    preview_output,
)

T = TypeVar("T")

_PROVIDER = "openai"


def _instrument_create(original: Any, operation: str) -> Any:
    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        client = get_client()
        if client is None:
            return original(*args, **kwargs)

        model = str(kwargs.get("model", ""))
        span = client.span(
            f"{operation} {model}".strip(),
            kind="llm",
            operation=operation,
            provider=_PROVIDER,
            model=model,
        )
        with span:
            span.set_attributes(
                {
                    "temperature": kwargs.get("temperature"),
                    "max_tokens": kwargs.get("max_tokens")
                    or kwargs.get("max_completion_tokens"),
                    "stream": kwargs.get("stream", False),
                }
            )
            if messages := kwargs.get("messages"):
                span.set_input(preview_messages(messages))
            elif input_text := kwargs.get("input"):
                span.set_input(preview_output(input_text))

            response = original(*args, **kwargs)

            # 流式响应的 usage 要等迭代结束才有，M1 不拆流；标注后跳过用量
            if kwargs.get("stream"):
                span.set_attributes({"usage_unavailable": "stream"})
                return response

            usage = extract_openai_usage(response)
            span.set_usage(**usage)
            actual_model = getattr(response, "model", "") or model
            span.set_model(model, response_model=str(actual_model), provider=_PROVIDER)
            span.set_output(preview_output(response))
            if finish := _finish_reason(response):
                span.set_attributes({"finish_reason": finish})
            return response

    return wrapper


def _finish_reason(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if choices:
        return str(getattr(choices[0], "finish_reason", "") or "")
    return ""


def wrap_openai(client: T) -> T:
    """就地包装 OpenAI 客户端的 create 方法，返回同一实例。

    幂等：重复包装同一客户端不会产生嵌套 span。
    """
    targets = (
        (("chat", "completions"), "chat"),
        (("embeddings",), "embeddings"),
        (("responses",), "chat"),
    )

    for path, operation in targets:
        node: Any = client
        for attr in path:
            node = getattr(node, attr, None)
            if node is None:
                break
        if node is None:
            continue

        create = getattr(node, "create", None)
        if create is None or getattr(create, "_ariadne_wrapped", False):
            continue

        wrapped = _instrument_create(create, operation)
        wrapped._ariadne_wrapped = True
        try:
            node.create = wrapped
        except (AttributeError, TypeError):
            # 某些版本用 __slots__ 或只读属性，跳过而非崩掉业务
            continue

    return client
