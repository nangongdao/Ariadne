"""自动埋点共用的用量提取与预览生成。

各 provider 的 usage 字段名不统一，且都在演进中。这里集中处理，
新增 provider 只需补一个 extract_*_usage 函数。
"""

from __future__ import annotations

from typing import Any, Final

_MAX_PREVIEW: Final = 2048
_MAX_MESSAGES: Final = 10


def _get(obj: Any, *names: str, default: Any = 0) -> Any:
    """兼容对象属性与字典键两种访问方式（SDK 返回值类型不一）。"""
    for name in names:
        if obj is None:
            return default
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    return default


def extract_openai_usage(response: Any) -> dict[str, int]:
    """OpenAI 的 usage。

    缓存命中在 prompt_tokens_details.cached_tokens 里，且**已包含在
    prompt_tokens 中** —— 必须减掉，否则缓存部分会被按全价重复计一次。
    """
    usage = _get(response, "usage", default=None)
    if usage is None:
        return {}

    prompt = int(_get(usage, "prompt_tokens", "input_tokens") or 0)
    completion = int(_get(usage, "completion_tokens", "output_tokens") or 0)

    details = _get(usage, "prompt_tokens_details", "input_tokens_details", default=None)
    cached = int(_get(details, "cached_tokens", default=0) or 0) if details else 0

    out_details = _get(usage, "completion_tokens_details", default=None)
    reasoning = (
        int(_get(out_details, "reasoning_tokens", default=0) or 0) if out_details else 0
    )

    return {
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": completion,
        "cache_read_tokens": cached,
        "reasoning_tokens": reasoning,
    }


def extract_anthropic_usage(response: Any) -> dict[str, int]:
    """Anthropic 的 usage。

    与 OpenAI 相反：cache_read_input_tokens 与 cache_creation_input_tokens
    是**独立于** input_tokens 的字段，不需要减。
    """
    usage = _get(response, "usage", default=None)
    if usage is None:
        return {}

    return {
        "input_tokens": int(_get(usage, "input_tokens") or 0),
        "output_tokens": int(_get(usage, "output_tokens") or 0),
        "cache_read_tokens": int(_get(usage, "cache_read_input_tokens") or 0),
        "cache_write_tokens": int(_get(usage, "cache_creation_input_tokens") or 0),
    }


def preview_messages(messages: Any) -> str:
    """把 messages 数组压成可读摘要。"""
    if not isinstance(messages, (list, tuple)):
        return str(messages)[:_MAX_PREVIEW]

    lines: list[str] = []
    for message in list(messages)[:_MAX_MESSAGES]:
        role = str(_get(message, "role", default="?"))
        content = _get(message, "content", default="")
        lines.append(f"{role}: {_stringify_content(content)}")
    return "\n".join(lines)[:_MAX_PREVIEW]


def _stringify_content(content: Any) -> str:
    """content 可能是字符串，也可能是多模态 block 数组。"""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            block_type = str(_get(block, "type", default=""))
            if block_type in ("text", ""):
                parts.append(str(_get(block, "text", default="")))
            else:
                parts.append(f"[{block_type}]")
        return " ".join(parts)
    return str(content)


def preview_output(response: Any) -> str:
    """从响应对象里提取文本输出。"""
    if isinstance(response, str):
        return response[:_MAX_PREVIEW]

    # OpenAI chat: choices[0].message.content
    choices = _get(response, "choices", default=None)
    if choices:
        message = _get(choices[0], "message", default=None)
        if message is not None:
            return _stringify_content(_get(message, "content", default=""))[:_MAX_PREVIEW]
        return str(_get(choices[0], "text", default=""))[:_MAX_PREVIEW]

    # Anthropic messages: content[] blocks
    content = _get(response, "content", default=None)
    if content:
        return _stringify_content(content)[:_MAX_PREVIEW]

    # OpenAI embeddings: 不回显向量，只记维度
    data = _get(response, "data", default=None)
    if data:
        first = data[0] if isinstance(data, (list, tuple)) and data else None
        embedding = _get(first, "embedding", default=None) if first else None
        if embedding is not None:
            return f"[embedding dim={len(embedding)}]"

    return str(response)[:_MAX_PREVIEW]
