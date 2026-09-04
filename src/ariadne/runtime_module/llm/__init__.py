"""LLM provider 适配器。

工厂函数按 settings.provider 装配。当前支持 anthropic + openai；
新增 provider 沿用注册表模式（见 telemetry.adapters / eval_module）。
"""

from __future__ import annotations

from typing import Final

from ariadne.config import LlmSettings
from ariadne.loop_module.engine import LLMClient
from ariadne.runtime_module.llm.anthropic import AnthropicLLMClient
from ariadne.runtime_module.llm.errors import LLMRateLimitError, check_rate_limit
from ariadne.runtime_module.llm.guarded import GuardedLLMAdapter, HarnessBlockError
from ariadne.runtime_module.llm.openai import OpenAILLMClient
from ariadne.telemetry.pricing import PricingTable

PROVIDERS: Final = frozenset({"anthropic", "openai"})


def build_llm_client(
    settings: LlmSettings, *, pricing: PricingTable | None = None
) -> LLMClient:
    """按配置装配 LLM 客户端。未支持 provider 显式报错。

    pricing 可注入 DB 计价表（见 resolve_project_llm）；缺省用内置默认价。
    """
    if settings.provider == "anthropic":
        return AnthropicLLMClient(settings, pricing=pricing)
    if settings.provider == "openai":
        return OpenAILLMClient(settings, pricing=pricing)
    raise ValueError(
        f"不支持的 LLM provider {settings.provider!r}，当前支持: {sorted(PROVIDERS)}"
    )


__all__ = [
    "PROVIDERS",
    "AnthropicLLMClient",
    "GuardedLLMAdapter",
    "HarnessBlockError",
    "LLMRateLimitError",
    "OpenAILLMClient",
    "build_llm_client",
    "check_rate_limit",
]