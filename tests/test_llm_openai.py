"""OpenAI LLM 适配器测试。

用 httpx.MockTransport 模拟 Chat Completions API，不连真实网络：
- 文本提取（choices[0].message.content）
- claimed_done 判定（finish_reason）
- 成本结算（PricingTable）
- 请求头（Authorization: Bearer，非 x-api-key）
- 工厂装配（provider="openai"）
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from ariadne.config import LlmSettings
from ariadne.runtime_module.llm.openai import OpenAILLMClient


def make_settings(**overrides: object) -> LlmSettings:
    base: dict[str, object] = {
        "provider": "openai",
        "model": "gpt-4o",
        "degraded_model": "gpt-4o-mini",
        "api_key": SecretStr("test-key"),
        "base_url": "https://api.openai.com",
    }
    base.update(overrides)
    return LlmSettings(**base)


def make_client(
    json_body: dict, *, status: int = 200
) -> tuple[OpenAILLMClient, list[dict]]:
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
        return httpx.Response(status, json=json_body)

    transport = httpx.MockTransport(handler)
    client = OpenAILLMClient(make_settings())
    # 复用 __init__ 的真实 headers，只替换 transport
    real = client._client
    client._client = httpx.AsyncClient(
        base_url=real.base_url,
        transport=transport,
        headers=dict(real.headers),
        timeout=real.timeout,
    )
    return client, captured


def text_response(text: str, *, finish_reason: str = "stop") -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


class TestTextExtraction:
    async def test_single_text_response(self) -> None:
        client, _ = make_client(text_response("def add(a, b): return a + b"))
        resp = await client.complete("写函数", model="gpt-4o")
        assert resp.output == "def add(a, b): return a + b"
        assert resp.input_tokens == 100
        assert resp.output_tokens == 200
        await client.aclose()

    async def test_empty_choices_returns_empty_string(self) -> None:
        """choices 为空时不抛异常，返回空字符串。"""
        body = {"choices": [], "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        client, _ = make_client(body)
        resp = await client.complete("x", model="gpt-4o")
        assert resp.output == ""
        await client.aclose()

    async def test_none_content_returns_empty_string(self) -> None:
        """content 为 null（如纯 tool_calls 响应）返回空字符串。"""
        body = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": None},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        client, _ = make_client(body)
        resp = await client.complete("x", model="gpt-4o")
        assert resp.output == ""
        await client.aclose()


class TestClaimedDone:
    async def test_stop_is_claimed_done(self) -> None:
        """finish_reason=stop → 模型自称完成。"""
        client, _ = make_client(text_response("ok", finish_reason="stop"))
        resp = await client.complete("x", model="gpt-4o")
        assert resp.claimed_done is True
        await client.aclose()

    async def test_length_is_not_claimed_done(self) -> None:
        """finish_reason=length → 截断，模型并未"完成"。"""
        client, _ = make_client(text_response("未完", finish_reason="length"))
        resp = await client.complete("x", model="gpt-4o")
        assert resp.claimed_done is False
        await client.aclose()

    async def test_tool_calls_is_not_claimed_done(self) -> None:
        """finish_reason=tool_calls → 工具调用，非自称完成。"""
        client, _ = make_client(text_response("要用工具", finish_reason="tool_calls"))
        resp = await client.complete("x", model="gpt-4o")
        assert resp.claimed_done is False
        await client.aclose()

    async def test_content_filter_is_not_claimed_done(self) -> None:
        """finish_reason=content_filter → 被过滤，非自称完成。"""
        client, _ = make_client(
            text_response("", finish_reason="content_filter")
        )
        resp = await client.complete("x", model="gpt-4o")
        assert resp.claimed_done is False
        await client.aclose()


class TestCost:
    async def test_cost_computed_from_pricing(self) -> None:
        """gpt-4o：输入 $2.50/M，输出 $10/M。"""
        body = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        }
        client, _ = make_client(body)
        resp = await client.complete("x", model="gpt-4o")
        # 1000*2.50 + 500*10 = 2500 + 5000 = 7500 / 1e6 = 0.0075
        assert float(resp.cost_usd) == pytest.approx(0.0075)
        await client.aclose()

    async def test_gpt_4o_mini_cost(self) -> None:
        """gpt-4o-mini：输入 $0.15/M，输出 $0.60/M。"""
        body = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 2000, "completion_tokens": 1000},
        }
        client, _ = make_client(body)
        resp = await client.complete("x", model="gpt-4o-mini")
        # 2000*0.15 + 1000*0.60 = 300 + 600 = 900 / 1e6 = 0.0009
        assert float(resp.cost_usd) == pytest.approx(0.0009)
        await client.aclose()

    async def test_unknown_model_zero_cost(self) -> None:
        """未知模型不猜价（成本 0），避免污染报表。"""
        body = text_response("ok")
        client, _ = make_client(body)
        resp = await client.complete("x", model="claude-sonnet-5")
        assert resp.cost_usd == 0
        await client.aclose()


class TestRequest:
    async def test_sends_authorization_bearer_header(self) -> None:
        """OpenAI 用 Authorization: Bearer 认证（非 x-api-key）。"""
        client, captured = make_client(text_response("ok"))
        await client.complete("hi", model="gpt-4o")
        assert captured[0]["headers"]["authorization"] == "Bearer test-key"
        assert "x-api-key" not in captured[0]["headers"]
        await client.aclose()

    async def test_sends_prompt_and_model(self) -> None:
        client, captured = make_client(text_response("ok"))
        await client.complete("写一个函数", model="gpt-4o-mini")
        payload = captured[0]["json"]
        assert b"gpt-4o-mini" in payload
        assert "写一个函数".encode() in payload
        await client.aclose()

    async def test_posts_to_chat_completions(self) -> None:
        """端点是 /v1/chat/completions（非 /v1/messages）。"""
        client, captured = make_client(text_response("ok"))
        await client.complete("hi", model="gpt-4o")
        assert "/v1/chat/completions" in captured[0]["url"]
        await client.aclose()

    async def test_api_error_raises(self) -> None:
        """provider 返回非 429 错误时抛 httpx 异常，由 engine 重试/修正。

        429 已翻译为 LLMRateLimitError（见 test_llm_rate_limit.py），
        此处用 500 验证其余状态码保持原样。
        """
        client, _ = make_client(
            {"error": {"message": "internal server error"}}, status=500
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("x", model="gpt-4o")
        await client.aclose()


class TestFactory:
    def test_build_openai(self) -> None:
        from ariadne.runtime_module.llm import build_llm_client

        client = build_llm_client(make_settings())
        assert isinstance(client, OpenAILLMClient)

    def test_build_anthropic_still_works(self) -> None:
        """Anthropic 仍然能正常装配。"""
        from ariadne.runtime_module.llm import build_llm_client
        from ariadne.runtime_module.llm.anthropic import AnthropicLLMClient

        settings = LlmSettings(
            provider="anthropic",
            model="claude-sonnet-5",
            api_key=SecretStr("test-key"),
            base_url="https://api.anthropic.com",
        )
        client = build_llm_client(settings)
        assert isinstance(client, AnthropicLLMClient)

    def test_unsupported_provider_raises(self) -> None:
        from ariadne.runtime_module.llm import build_llm_client

        with pytest.raises(ValueError):
            build_llm_client(make_settings(provider="gemini"))

    def test_providers_includes_openai(self) -> None:
        from ariadne.runtime_module.llm import PROVIDERS

        assert "openai" in PROVIDERS
        assert "anthropic" in PROVIDERS
