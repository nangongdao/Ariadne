"""Anthropic LLM 适配器测试。

用 httpx.MockTransport 模拟 Messages API，不连真实网络：
- 文本提取（content block 拼接）
- claimed_done 判定（stop_reason）
- 成本结算（PricingTable，含 cache token 分开计）
- 请求头（x-api-key 从 settings，不硬编码）
- 速率限制配额解析（anthropic-ratelimit-* 响应头）
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from ariadne.config import LlmSettings
from ariadne.runtime_module.llm.anthropic import (
    AnthropicLLMClient,
    _parse_rate_limit_headers,
)


def make_settings(**overrides: object) -> LlmSettings:
    base = {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "api_key": SecretStr("test-key"),
        "base_url": "https://api.anthropic.com",
    }
    base.update(overrides)
    return LlmSettings(**base)


def make_client(
    json_body: dict, *, status: int = 200, headers: dict[str, str] | None = None
) -> tuple[AnthropicLLMClient, list[dict]]:
    """构造 client，捕获请求供断言。"""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "json": request.content,
            }
        )
        return httpx.Response(status, json=json_body, headers=headers or {})

    transport = httpx.MockTransport(handler)
    client = AnthropicLLMClient(make_settings())
    # 复用 __init__ 的真实 headers，只替换 transport
    real = client._client
    client._client = httpx.AsyncClient(
        base_url=real.base_url,
        transport=transport,
        headers=dict(real.headers),
        timeout=real.timeout,
    )
    return client, captured


def text_response(text: str, *, stop_reason: str = "end_turn") -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 100, "output_tokens": 200},
    }


class TestTextExtraction:
    async def test_single_text_block(self) -> None:
        client, _ = make_client(text_response("def add(a, b): return a + b"))
        resp = await client.complete("写函数", model="claude-sonnet-5")
        assert resp.output == "def add(a, b): return a + b"
        assert resp.input_tokens == 100
        assert resp.output_tokens == 200
        await client.aclose()

    async def test_multiple_text_blocks_joined(self) -> None:
        body = {
            "content": [
                {"type": "text", "text": "第一部分"},
                {"type": "text", "text": "第二部分"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client, _ = make_client(body)
        resp = await client.complete("x", model="claude-sonnet-5")
        assert resp.output == "第一部分\n第二部分"
        await client.aclose()

    async def test_ignores_tool_blocks(self) -> None:
        body = {
            "content": [
                {"type": "tool_use", "name": "calc", "input": {"a": 1}},
                {"type": "text", "text": "答案是 2"},
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        client, _ = make_client(body)
        resp = await client.complete("x", model="claude-sonnet-5")
        assert resp.output == "答案是 2"
        assert resp.claimed_done is False
        await client.aclose()


class TestClaimedDone:
    async def test_end_turn_is_claimed_done(self) -> None:
        client, _ = make_client(text_response("ok", stop_reason="end_turn"))
        resp = await client.complete("x", model="claude-sonnet-5")
        assert resp.claimed_done is True
        await client.aclose()

    async def test_max_tokens_is_not_claimed_done(self) -> None:
        """stop_reason=max_tokens → 截断，模型并未"完成"。"""
        client, _ = make_client(text_response("未完", stop_reason="max_tokens"))
        resp = await client.complete("x", model="claude-sonnet-5")
        assert resp.claimed_done is False
        await client.aclose()

    async def test_tool_use_is_not_claimed_done(self) -> None:
        client, _ = make_client(text_response("要用工具", stop_reason="tool_use"))
        resp = await client.complete("x", model="claude-sonnet-5")
        assert resp.claimed_done is False
        await client.aclose()


class TestCost:
    async def test_cost_computed_from_pricing(self) -> None:
        """claude-sonnet-5：输入 $3/M，输出 $15/M，cache_read $0.30/M。"""
        body = {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "cache_read_input_tokens": 2000,
                "cache_creation_input_tokens": 300,
            },
        }
        client, _ = make_client(body)
        resp = await client.complete("x", model="claude-sonnet-5")
        # 1000*3 + 500*15 + 2000*0.30 + 300*3.75 (cache_write) = 3000+7500+600+1125 = 12225 / 1e6
        assert float(resp.cost_usd) == pytest.approx(0.012225)
        await client.aclose()

    async def test_unknown_model_zero_cost(self) -> None:
        """未知模型不猜价（成本 0），避免污染报表。"""
        body = text_response("ok")
        client, _ = make_client(body)
        resp = await client.complete("x", model="gpt-4o-unknown-model")
        assert resp.cost_usd == 0
        await client.aclose()


class TestRequest:
    async def test_sends_api_key_header(self) -> None:
        client, captured = make_client(text_response("ok"))
        await client.complete("hi", model="claude-sonnet-5")
        assert captured[0]["headers"]["x-api-key"] == "test-key"
        assert "anthropic-version" in captured[0]["headers"]
        await client.aclose()

    async def test_sends_prompt_and_model(self) -> None:
        client, captured = make_client(text_response("ok"))
        await client.complete("写一个函数", model="claude-haiku-4-5")
        payload = captured[0]["json"]
        assert b"claude-haiku-4-5" in payload
        assert "写一个函数".encode() in payload
        await client.aclose()

    async def test_api_error_raises(self) -> None:
        """provider 返回 4xx/5xx 时抛 httpx 异常，由 engine 重试/修正。"""
        client, _ = make_client({"error": {"message": "overloaded"}}, status=529)
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("x", model="claude-sonnet-5")
        await client.aclose()


class TestFactory:
    def test_build_anthropic(self) -> None:
        from ariadne.runtime_module.llm import build_llm_client

        client = build_llm_client(make_settings())
        assert isinstance(client, AnthropicLLMClient)

    def test_unsupported_provider_raises(self) -> None:
        from ariadne.runtime_module.llm import build_llm_client

        with pytest.raises(ValueError):
            build_llm_client(make_settings(provider="gemini"))


class TestRateLimitQuotaParsing:
    """测试 anthropic-ratelimit-* 响应头解析。"""

    def test_parse_all_headers(self) -> None:
        headers = httpx.Headers({
            "anthropic-ratelimit-requests-remaining": "49",
            "anthropic-ratelimit-tokens-remaining": "49500",
            "anthropic-ratelimit-requests-reset": "2024-01-01T00:01:00Z",
            "anthropic-ratelimit-tokens-reset": "2024-01-01T00:01:00Z",
        })
        quota = _parse_rate_limit_headers(headers)
        assert quota.requests_remaining == 49
        assert quota.tokens_remaining == 49500
        assert quota.requests_reset == "2024-01-01T00:01:00Z"
        assert quota.tokens_reset == "2024-01-01T00:01:00Z"

    def test_parse_missing_headers(self) -> None:
        """缺失响应头返回 None（旧版 API 或非标准响应）。"""
        headers = httpx.Headers({})
        quota = _parse_rate_limit_headers(headers)
        assert quota.requests_remaining is None
        assert quota.tokens_remaining is None
        assert quota.requests_reset is None
        assert quota.tokens_reset is None

    def test_parse_partial_headers(self) -> None:
        """部分响应头存在时只解析存在的字段。"""
        headers = httpx.Headers({
            "anthropic-ratelimit-requests-remaining": "10",
            "anthropic-ratelimit-requests-reset": "2024-01-01T00:00:30Z",
        })
        quota = _parse_rate_limit_headers(headers)
        assert quota.requests_remaining == 10
        assert quota.tokens_remaining is None
        assert quota.requests_reset == "2024-01-01T00:00:30Z"
        assert quota.tokens_reset is None

    def test_parse_invalid_int_returns_none(self) -> None:
        """非数字的剩余配额返回 None 而非抛异常（容错）。"""
        headers = httpx.Headers({
            "anthropic-ratelimit-requests-remaining": "not-a-number",
            "anthropic-ratelimit-tokens-remaining": "49500",
        })
        quota = _parse_rate_limit_headers(headers)
        assert quota.requests_remaining is None
        assert quota.tokens_remaining == 49500

    def test_parse_zero_remaining(self) -> None:
        """剩余配额为 0 时正确解析（边界情况：下次请求会 429）。"""
        headers = httpx.Headers({
            "anthropic-ratelimit-requests-remaining": "0",
            "anthropic-ratelimit-tokens-remaining": "0",
        })
        quota = _parse_rate_limit_headers(headers)
        assert quota.requests_remaining == 0
        assert quota.tokens_remaining == 0

    async def test_quota_logged_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        """成功响应包含配额信息时记录到日志。"""
        headers = {
            "anthropic-ratelimit-requests-remaining": "48",
            "anthropic-ratelimit-tokens-remaining": "45000",
            "anthropic-ratelimit-requests-reset": "2024-01-01T00:01:00Z",
            "anthropic-ratelimit-tokens-reset": "2024-01-01T00:01:00Z",
        }
        client, _ = make_client(text_response("ok"), headers=headers)

        import logging
        with caplog.at_level(logging.DEBUG):
            await client.complete("test", model="claude-sonnet-5")

        # 验证日志包含配额信息
        records = [r.message for r in caplog.records if "rate limit quota" in r.message]
        assert len(records) == 1
        await client.aclose()

    async def test_no_quota_log_when_headers_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """响应头缺失时不记录配额日志（避免无意义的 None 日志）。"""
        client, _ = make_client(text_response("ok"), headers={})

        import logging
        with caplog.at_level(logging.DEBUG):
            await client.complete("test", model="claude-sonnet-5")

        records = [r.message for r in caplog.records if "rate limit quota" in r.message]
        assert len(records) == 0
        await client.aclose()