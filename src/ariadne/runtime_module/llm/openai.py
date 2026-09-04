"""OpenAI Chat Completions API 适配器 —— Loop Engine 的 LLMClient 实现。

与 AnthropicLLMClient 同构，实现同一个 LLMClient Protocol。
直连 HTTP（httpx）而非 openai SDK：
- 项目已依赖 httpx，避免引入大型 SDK（依赖最小化）
- 适配器只需调一个端点、解析 JSON，SDK 的复杂度用不上

关键设计（与 anthropic.py 对称）：
- `claimed_done`：finish_reason == "stop" 时视为"自称完成"。
  **仅记录用于假完成统计，不参与收敛判定**（engine 的 Ralph 原则）。
- 成本用 PricingTable 精确结算，预算硬熔断的正确性依赖真实成本。
- API key 从 settings 读（环境变量/.env），绝不硬编码。

OpenAI vs Anthropic API 差异：
- 认证：Authorization: Bearer <key>（非 x-api-key）
- 端点：/v1/chat/completions（非 /v1/messages）
- 请求：messages + max_tokens（与 Anthropic 相同字段名，但 role/content 结构略异）
- 响应：choices[0].message.content（非 content block 列表）
- 用量：prompt_tokens / completion_tokens（非 input_tokens / output_tokens）
- 停止原因：finish_reason "stop"/"length"/"tool_calls"（非 stop_reason "end_turn"）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from ariadne.config import LlmSettings
from ariadne.loop_module.engine import LLMClient, LLMResponse
from ariadne.runtime_module.llm.errors import check_rate_limit
from ariadne.telemetry.models import TokenUsage
from ariadne.telemetry.pricing import PricingTable
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_PROVIDER = "openai"
# 输出长度上限。engine 的预算以 max_tokens_per_iteration 兜底，
# 这里给一个保守的硬上限避免单次响应过大
MAX_OUTPUT_TOKENS = 16_000


class OpenAILLMClient(LLMClient):
    """OpenAI Chat Completions API 适配器。

    用法：``await client.complete(prompt, model=model)``
    """

    def __init__(
        self,
        settings: LlmSettings,
        *,
        timeout_seconds: float = 120.0,
        pricing: PricingTable | None = None,
    ) -> None:
        self._settings = settings
        self._pricing = pricing or PricingTable()
        self._client = httpx.AsyncClient(
            base_url=settings.base_url,
            timeout=httpx.Timeout(timeout_seconds),
            headers={
                "Authorization": f"Bearer {settings.api_key.get_secret_value()}",
                "content-type": "application/json",
            },
        )

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        """调 Chat Completions API 生成。

        429 翻译为 LLMRateLimitError（含 Retry-After）；其余异常不在此
        捕获 —— 交给 engine 的 _executing 按模式决定重试/修正。
        """
        response = await self._client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        check_rate_limit(response)
        response.raise_for_status()
        data = response.json()

        text = _extract_text(data)
        usage_raw = data.get("usage") or {}
        # OpenAI 的 prompt_tokens/completion_tokens 对应 input/output
        usage = TokenUsage(
            input_tokens=_i(usage_raw, "prompt_tokens"),
            output_tokens=_i(usage_raw, "completion_tokens"),
        )
        cost = self._pricing.compute(_PROVIDER, model, usage, datetime.now(UTC))
        finish_reason = _extract_finish_reason(data)
        # "stop" 是正常完成；"length" 是截断；"tool_calls" 是工具调用
        claimed_done = finish_reason == "stop"

        logger.debug(
            "llm completion",
            extra={
                "model": model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "finish_reason": finish_reason,
                "cost_usd": str(cost),
            },
        )

        return LLMResponse(
            output=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            claimed_done=claimed_done,
            model=model,
            cost_usd=cost,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _extract_text(data: dict[str, Any]) -> str:
    """从 Chat Completions 响应提取文本。

    choices[0].message.content 是字符串（非 Anthropic 的 block 列表）。
    多 choice 只取第一个（n=1 是默认）。
    """
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    return str(content) if content is not None else ""


def _extract_finish_reason(data: dict[str, Any]) -> str:
    """提取 finish_reason（停止原因）。

    finish_reason 可能值：stop / length / tool_calls / content_filter。
    对应 Anthropic 的 stop_reason：end_turn / max_tokens / tool_use。
    """
    choices = data.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("finish_reason") or "")


def _i(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    return int(value) if value is not None else 0


__all__ = ["_PROVIDER", "OpenAILLMClient"]
