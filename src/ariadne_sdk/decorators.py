"""@trace 装饰器。

同时支持同步与异步函数：async 函数必须返回协程，用同步 wrapper 包会破坏语义。
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, ParamSpec, TypeVar, cast

from ariadne_sdk.client import get_client
from ariadne_sdk.span import Span

P = ParamSpec("P")
R = TypeVar("R")

_MAX_ARG_PREVIEW = 512


def _preview_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """生成参数摘要。只取 repr 前若干字符，避免大对象拖慢业务线程。"""
    parts = [repr(a)[:120] for a in args[:3]]
    parts += [f"{k}={v!r}"[:120] for k, v in list(kwargs.items())[:3]]
    return ", ".join(parts)[:_MAX_ARG_PREVIEW]


def _make_span(name: str, kind: str, args: tuple[Any, ...], kwargs: dict[str, Any],
               capture_io: bool) -> Span | None:
    client = get_client()
    if client is None:
        return None
    span = client.span(name, kind=kind)
    if capture_io:
        span.set_input(_preview_args(args, kwargs))
    return span


def trace(
    name: str | None = None,
    *,
    kind: str = "internal",
    capture_io: bool = True,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """把函数调用记录为 span。

    未调用 ariadne_sdk.init() 时装饰器是零开销直通，不会报错 ——
    这让同一份代码在有无 Ariadne 的环境下都能跑。
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        span_name = name or func.__qualname__

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                span = _make_span(span_name, kind, args, kwargs, capture_io)
                if span is None:
                    return await cast("Callable[P, Awaitable[Any]]", func)(*args, **kwargs)
                with span:
                    result = await cast("Callable[P, Awaitable[Any]]", func)(*args, **kwargs)
                    if capture_io and result is not None:
                        span.set_output(repr(result))
                    return result

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            span = _make_span(span_name, kind, args, kwargs, capture_io)
            if span is None:
                return func(*args, **kwargs)
            with span:
                result = func(*args, **kwargs)
                if capture_io and result is not None:
                    span.set_output(repr(result))
                return result

        return sync_wrapper

    return decorator
