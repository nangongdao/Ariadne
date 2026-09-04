"""Anthropic Messages API 适配器 —— Loop Engine 的 LLMClient 实现。

直连 HTTP（httpx）而非 anthropic SDK：
- 项目已依赖 httpx，避免引入大型 SDK（依赖最小化）
- 适配器只需调一个端点、解析 JSON，SDK 的复杂度用不上

关键设计：
- `claimed_done`：模型 stop_reason == "end_turn" 时视为"自称完成"。
  **仅记录用于假完成统计，不参与收敛判定**（engine 的 Ralph 原则）。
- 成本用 PricingTable 精确结算（cache_read/cache_write 分开计），
  预算硬熔断的正确性依赖真实成本，不能用估算。
- API key 从 settings 读（环境变量/.env），绝不硬编码。
- 主动余量调度（2026-09-03 实现）：解析成功响应的 anthropic-ratelimit-*
  响应头，记录剩余配额供监控和调度决策。R9 被动部分（429 处理）已落地。
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class RateLimitQuota:
    """Anthropic API 的速率限制配额信息。

    从成功响应的 anthropic-ratelimit-* 响应头解析。用于主动余量调度：
    在触发 429 之前预测配额耗尽，提前降低并发或延迟请求。

    字段语义：
    - requests_remaining: 当前周期内剩余请求数（0 表示下次请求会 429）
    - tokens_remaining: 当前周期内剩余 token 数
    - requests_reset / tokens_reset: 配额重置时间（ISO 8601 格式字符串）

    缺失字段为 None（旧版 API 或错误响应头）。
    """

    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    requests_reset: str | None = None
    tokens_reset: str | None = None

_PROVIDER = "anthropic"
# 输出长度上限。engine 的预算以 max_tokens_per_iteration 兜底，
# 这里给一个保守的硬上限避免单次响应过大
MAX_OUTPUT_TOKENS = 16_000


class AnthropicLLMClient(LLMClient):
    """Anthropic Messages API 适配器。

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
                "x-api-key": settings.api_key.get_secret_value(),
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        """调 Messages API 生成。

        429 翻译为 LLMRateLimitError（含 Retry-After）；其余异常不在此
        捕获 —— 交给 engine 的 _executing 按模式决定重试/修正。

        成功响应解析 anthropic-ratelimit-* 响应头并记录配额信息。
        """
        response = await self._client.post(
            "/v1/messages",
            json={
                "model": model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        check_rate_limit(response)
        response.raise_for_status()
        data = response.json()

        # 解析速率限制配额（主动余量调度）
        quota = _parse_rate_limit_headers(response.headers)
        if quota.requests_remaining is not None or quota.tokens_remaining is not None:
            logger.debug(
                "anthropic rate limit quota",
                extra={
                    "requests_remaining": quota.requests_remaining,
                    "tokens_remaining": quota.tokens_remaining,
                    "requests_reset": quota.requests_reset,
                    "tokens_reset": quota.tokens_reset,
                },
            )

        text = _extract_text(data)
        usage_raw = data.get("usage") or {}
        usage = TokenUsage(
            input_tokens=_i(usage_raw, "input_tokens"),
            output_tokens=_i(usage_raw, "output_tokens"),
            cache_read_tokens=_i(usage_raw, "cache_read_input_tokens"),
            cache_write_tokens=_i(usage_raw, "cache_creation_input_tokens"),
        )
        cost = self._pricing.compute(
            _PROVIDER, model, usage, datetime.now(UTC)
        )
        stop_reason = data.get("stop_reason")
        claimed_done = stop_reason == "end_turn"

        logger.debug(
            "llm completion",
            extra={
                "model": model,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read": usage.cache_read_tokens,
                "stop_reason": stop_reason,
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
            rate_limit_quota=quota,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _extract_text(data: dict[str, Any]) -> str:
    """从 Messages 响应提取文本。content 是 block 列表。"""
    parts: list[str] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            parts.append(str(block.get("text", "")))
    return "\n".join(parts)


def _i(raw: dict[str, Any], key: str) -> int:
    value = raw.get(key)
    return int(value) if value is not None else 0


def _parse_rate_limit_headers(headers: httpx.Headers) -> RateLimitQuota:
    """从 Anthropic 响应头解析速率限制配额。

    响应头格式（Anthropic API 文档）：
    - anthropic-ratelimit-requests-remaining: 剩余请求数
    - anthropic-ratelimit-tokens-remaining: 剩余 token 数
    - anthropic-ratelimit-requests-reset: 请求配额重置时间（ISO 8601）
    - anthropic-ratelimit-tokens-reset: token 配额重置时间（ISO 8601）

    缺失或解析失败的字段返回 None（旧版 API 或非标准响应头）。
    """
    def _parse_int(key: str) -> int | None:
        value = headers.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            logger.warning(f"无法解析响应头 {key}: {value}")
            return None

    return RateLimitQuota(
        requests_remaining=_parse_int("anthropic-ratelimit-requests-remaining"),
        tokens_remaining=_parse_int("anthropic-ratelimit-tokens-remaining"),
        requests_reset=headers.get("anthropic-ratelimit-requests-reset"),
        tokens_reset=headers.get("anthropic-ratelimit-tokens-reset"),
    )


__all__ = ["_PROVIDER", "AnthropicLLMClient", "RateLimitQuota"]