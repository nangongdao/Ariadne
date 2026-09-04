"""自动埋点。

原则：只包装各库的**公开方法**，不 patch 私有属性 —— 后者在上游
小版本升级时必碎。因此需要用户显式调用 wrap_*，而非隐式全局 hook。
"""

from ariadne_sdk.instrument.anthropic import wrap_anthropic
from ariadne_sdk.instrument.common import (
    extract_anthropic_usage,
    extract_openai_usage,
    preview_messages,
    preview_output,
)
from ariadne_sdk.instrument.openai import wrap_openai

__all__ = [
    "extract_anthropic_usage",
    "extract_openai_usage",
    "preview_messages",
    "preview_output",
    "wrap_anthropic",
    "wrap_openai",
]
