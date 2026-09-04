"""R9 速率限制测试 —— 429 类型化翻译 + 自适应余量监控 + 退避抖动。

覆盖三层防线：
- 客户端层：429 → LLMRateLimitError（Retry-After 结构化传递），其余状态码原样
- 监控层：AdaptiveRateLimitMonitor 的冷却 / 折减 / 恢复语义
- 池层：_backoff_delay 的抖动边界与 Retry-After 下限

RetryMode 分类与引擎原地重试见 test_chaos.py / test_loop_engine.py。
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from ariadne.config import LlmSettings
from ariadne.loop_module.parallel import _backoff_delay, _is_rate_limited
from ariadne.loop_module.rate_limit import (
    DEFAULT_COOLDOWN,
    MIN_FACTOR,
    RECOVERY_STEP,
    AdaptiveRateLimitMonitor,
)
from ariadne.runtime_module.llm.anthropic import AnthropicLLMClient
from ariadne.runtime_module.llm.errors import LLMRateLimitError
from ariadne.runtime_module.llm.openai import OpenAILLMClient


class FakeClock:
    """可控单调时钟，驱动监控器的冷却期判定。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _anthropic_settings() -> LlmSettings:
    return LlmSettings(
        provider="anthropic",
        model="claude-sonnet-5",
        api_key=SecretStr("test-key"),
        base_url="https://api.anthropic.com",
    )


def _openai_settings() -> LlmSettings:
    return LlmSettings(
        provider="openai",
        model="gpt-4o",
        api_key=SecretStr("test-key"),
        base_url="https://api.openai.com",
    )


def _make_client(
    client_cls: type,
    settings: LlmSettings,
    *,
    status: int,
    headers: dict[str, str] | None = None,
):
    """构造带 MockTransport 的客户端，返回固定状态码/响应头的响应。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={}, headers=headers or {})

    client = client_cls(settings)
    real = client._client
    client._client = httpx.AsyncClient(
        base_url=real.base_url,
        transport=httpx.MockTransport(handler),
        headers=dict(real.headers),
        timeout=real.timeout,
    )
    return client


# ---------- 异常本身 ----------


class TestLLMRateLimitError:
    def test_retry_after_defaults_to_none(self) -> None:
        error = LLMRateLimitError("provider 速率限制")
        assert error.retry_after is None
        assert "429" in str(error) or "速率限制" in str(error)

    def test_retry_after_carried(self) -> None:
        error = LLMRateLimitError("429", retry_after=12.5)
        assert error.retry_after == 12.5

    def test_is_runtime_error(self) -> None:
        # engine 的 except Exception 分支能捕获它
        assert issubclass(LLMRateLimitError, RuntimeError)


# ---------- 客户端 429 翻译 ----------


class TestAnthropic429Translation:
    @pytest.mark.asyncio
    async def test_429_translated_without_header(self) -> None:
        client = _make_client(AnthropicLLMClient, _anthropic_settings(), status=429)
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.complete("x", model="claude-sonnet-5")
        assert exc_info.value.retry_after is None
        await client.aclose()

    @pytest.mark.asyncio
    async def test_429_translated_with_retry_after(self) -> None:
        client = _make_client(
            AnthropicLLMClient,
            _anthropic_settings(),
            status=429,
            headers={"retry-after": "17"},
        )
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.complete("x", model="claude-sonnet-5")
        assert exc_info.value.retry_after == 17.0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_429_non_numeric_retry_after_ignored(self) -> None:
        """HTTP-date 形式的 Retry-After 解析失败 → None（退回指数退避）。"""
        client = _make_client(
            AnthropicLLMClient,
            _anthropic_settings(),
            status=429,
            headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
        )
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.complete("x", model="claude-sonnet-5")
        assert exc_info.value.retry_after is None
        await client.aclose()

    @pytest.mark.asyncio
    async def test_5xx_not_translated(self) -> None:
        """5xx 保持 httpx.HTTPStatusError——类型真实，语义由 RetryMode 分类。"""
        client = _make_client(AnthropicLLMClient, _anthropic_settings(), status=529)
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("x", model="claude-sonnet-5")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_401_not_translated(self) -> None:
        client = _make_client(AnthropicLLMClient, _anthropic_settings(), status=401)
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("x", model="claude-sonnet-5")
        await client.aclose()


class TestOpenAI429Translation:
    @pytest.mark.asyncio
    async def test_429_translated_without_header(self) -> None:
        client = _make_client(OpenAILLMClient, _openai_settings(), status=429)
        with pytest.raises(LLMRateLimitError):
            await client.complete("x", model="gpt-4o")
        await client.aclose()

    @pytest.mark.asyncio
    async def test_429_translated_with_retry_after(self) -> None:
        client = _make_client(
            OpenAILLMClient,
            _openai_settings(),
            status=429,
            headers={"retry-after": "6"},
        )
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.complete("x", model="gpt-4o")
        assert exc_info.value.retry_after == 6.0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_500_not_translated(self) -> None:
        client = _make_client(OpenAILLMClient, _openai_settings(), status=500)
        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("x", model="gpt-4o")
        await client.aclose()


# ---------- 自适应余量监控 ----------


class TestAdaptiveRateLimitMonitor:
    def test_initial_full_headroom(self) -> None:
        monitor = AdaptiveRateLimitMonitor()
        assert monitor.factor == 1.0
        assert monitor.available_concurrency(8) == 8

    def test_429_enters_cooldown_and_halves(self) -> None:
        clock = FakeClock()
        monitor = AdaptiveRateLimitMonitor(clock=clock)
        monitor.note_rate_limited(None)
        assert monitor.factor == 0.5
        assert monitor.cooling_down is True
        # 冷却期内暂停派发
        assert monitor.available_concurrency(8) == 0
        # 默认冷却期过后恢复折减值（非满额）
        clock.advance(DEFAULT_COOLDOWN + 1)
        assert monitor.cooling_down is False
        assert monitor.available_concurrency(8) == 4  # int(8 * 0.5)

    def test_retry_after_drives_cooldown_length(self) -> None:
        clock = FakeClock()
        monitor = AdaptiveRateLimitMonitor(clock=clock)
        monitor.note_rate_limited(5.0)
        clock.advance(4.9)
        assert monitor.cooling_down is True
        clock.advance(0.2)
        assert monitor.cooling_down is False

    def test_default_cooldown_when_no_retry_after(self) -> None:
        clock = FakeClock()
        monitor = AdaptiveRateLimitMonitor(clock=clock)
        monitor.note_rate_limited(None)
        clock.advance(DEFAULT_COOLDOWN - 0.1)
        assert monitor.cooling_down is True
        clock.advance(0.2)
        assert monitor.cooling_down is False

    def test_retry_after_capped(self) -> None:
        """异常大的 Retry-After 被封顶，防单次 429 长时间停摆。"""
        clock = FakeClock()
        monitor = AdaptiveRateLimitMonitor(clock=clock)
        monitor.note_rate_limited(999_999.0)
        clock.advance(121.0)
        assert monitor.cooling_down is False

    def test_factor_floors_at_min(self) -> None:
        """连续 429 折半到底，保留探测流量（MIN_FACTOR）。"""
        monitor = AdaptiveRateLimitMonitor(clock=FakeClock())
        for _ in range(6):
            monitor.note_rate_limited(None)
        assert monitor.factor == MIN_FACTOR

    def test_success_recovers_gradually(self) -> None:
        monitor = AdaptiveRateLimitMonitor(clock=FakeClock())
        monitor.note_rate_limited(None)
        assert monitor.factor == 0.5
        for _ in range(int(0.5 / RECOVERY_STEP)):
            monitor.note_success()
        assert monitor.factor == 1.0

    def test_recovery_does_not_exceed_full(self) -> None:
        monitor = AdaptiveRateLimitMonitor(clock=FakeClock())
        for _ in range(20):
            monitor.note_success()
        assert monitor.factor == 1.0

    def test_fraction_truncates_down(self) -> None:
        """折减取整向下：int(5 * 0.5) = 2，宁可保守。"""
        clock = FakeClock()
        monitor = AdaptiveRateLimitMonitor(clock=clock)
        monitor.note_rate_limited(None)
        clock.advance(DEFAULT_COOLDOWN + 1)  # 出冷却期才能看到折减值
        assert monitor.available_concurrency(5) == 2

    def test_cooldown_extension_not_shrunk(self) -> None:
        """连续 429 时冷却期取 max——新信号不会缩短已有的更长冷却。"""
        clock = FakeClock()
        monitor = AdaptiveRateLimitMonitor(clock=clock)
        monitor.note_rate_limited(60.0)
        clock.advance(10)
        monitor.note_rate_limited(None)  # 默认 30s < 剩余 50s
        clock.advance(20)  # 距首个 429 共 30s，剩余冷却还有 30s
        assert monitor.cooling_down is True


# ---------- 池的退避抖动 ----------


class TestBackoffDelay:
    def test_within_jitter_bounds(self) -> None:
        """多次采样全部落在 ±25% 抖动区间内。"""
        for attempt in range(4):
            base = min(1.0 * (2**attempt), 30.0)
            for _ in range(100):
                delay = _backoff_delay(attempt, None)
                assert base * 0.75 <= delay <= base * 1.25

    def test_retry_after_is_floor(self) -> None:
        """Retry-After 大于退避值时取 Retry-After。"""
        delay = _backoff_delay(0, 45.0)
        assert delay >= 45.0

    def test_retry_after_smaller_ignored(self) -> None:
        """Retry-After 小于退避下限时不起作用（退避下限起效）。"""
        base = min(1.0 * (2**3), 30.0)
        for _ in range(50):
            delay = _backoff_delay(3, 0.1)
            assert delay >= base * 0.75

    def test_non_positive_retry_after_ignored(self) -> None:
        delay = _backoff_delay(0, 0.0)
        assert 0.75 <= delay <= 1.25

    def test_capped_at_max(self) -> None:
        """attempt 很大时退避被 RETRY_MAX_DELAY 封顶（含抖动上界）。"""
        for _ in range(50):
            delay = _backoff_delay(20, None)
            assert delay <= 30.0 * 1.25


# ---------- 协同 ----------


class TestIntegration:
    def test_typed_error_flows_through_pool_detection(self) -> None:
        """LLMRateLimitError 被 _is_rate_limited 识别——池无需感知翻译细节。"""
        assert _is_rate_limited(LLMRateLimitError("provider 速率限制 (HTTP 429)"))
