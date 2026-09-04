"""成本计算测试。重点覆盖缓存折扣与前缀匹配 —— 这两处出错会直接算错账单。"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ariadne.telemetry.models import TokenUsage
from ariadne.telemetry.pricing import Price, PriceEntry, PricingTable

AT = datetime(2026, 8, 25, tzinfo=UTC)


@pytest.fixture
def table() -> PricingTable:
    return PricingTable()


def test_cache_read_is_cheaper_than_input(table: PricingTable) -> None:
    """同样的总输入量，走缓存必须更便宜。"""
    plain = TokenUsage(input_tokens=3000, output_tokens=500)
    cached = TokenUsage(input_tokens=1000, output_tokens=500, cache_read_tokens=2000)

    cost_plain = table.compute("openai", "gpt-4o", plain, AT)
    cost_cached = table.compute("openai", "gpt-4o", cached, AT)

    assert cost_cached < cost_plain
    # gpt-4o: input $2.50/M, cache_read $1.25/M, output $10/M
    assert cost_plain == Decimal("0.01250000")
    assert cost_cached == Decimal("0.01000000")


def test_prefix_match_picks_longest(table: PricingTable) -> None:
    """gpt-4o-mini 不能被 gpt-4o 的价格抢走（差价 16 倍）。"""
    usage = TokenUsage(input_tokens=1_000_000)
    assert table.compute("openai", "gpt-4o-mini", usage, AT) == Decimal("0.15000000")
    assert table.compute("openai", "gpt-4o", usage, AT) == Decimal("2.50000000")


def test_dated_version_matches_base_model(table: PricingTable) -> None:
    """带日期后缀的版本应命中基础模型价格。"""
    usage = TokenUsage(input_tokens=1_000_000)
    assert table.compute("openai", "gpt-4o-2024-11-20", usage, AT) == Decimal("2.50000000")


def test_unknown_model_returns_zero(table: PricingTable) -> None:
    """未知模型返回 0 而非猜价：宁可缺数据，不可污染成本报表。"""
    usage = TokenUsage(input_tokens=1_000_000)
    assert table.compute("openai", "totally-unknown", usage, AT) == Decimal("0")
    assert table.compute("unknown-provider", "gpt-4o", usage, AT) == Decimal("0")


def test_anthropic_cache_write_has_premium(table: PricingTable) -> None:
    """Anthropic 的 cache write 是溢价（$3.75 > $3.00 input）。"""
    write = TokenUsage(cache_write_tokens=1_000_000)
    read = TokenUsage(cache_read_tokens=1_000_000)
    plain = TokenUsage(input_tokens=1_000_000)

    cost_write = table.compute("anthropic", "claude-sonnet-5", write, AT)
    cost_read = table.compute("anthropic", "claude-sonnet-5", read, AT)
    cost_plain = table.compute("anthropic", "claude-sonnet-5", plain, AT)

    assert cost_read < cost_plain < cost_write


def test_effective_time_range_isolates_history() -> None:
    """调价后历史记录仍按当时价格计算。"""
    old_price = Price(*(Decimal(v) for v in ("1.00", "2.00", "0.50", "0", "2.00")))
    new_price = Price(*(Decimal(v) for v in ("5.00", "10.00", "2.50", "0", "10.00")))
    cutover = datetime(2026, 6, 1, tzinfo=UTC)

    table = PricingTable((
        PriceEntry("acme", "m1", old_price, datetime(2024, 1, 1, tzinfo=UTC), cutover),
        PriceEntry("acme", "m1", new_price, cutover, None),
    ))
    usage = TokenUsage(input_tokens=1_000_000)

    before = table.compute("acme", "m1", usage, datetime(2026, 5, 1, tzinfo=UTC))
    after = table.compute("acme", "m1", usage, datetime(2026, 7, 1, tzinfo=UTC))

    assert before == Decimal("1.00000000")
    assert after == Decimal("5.00000000")


def test_naive_datetime_treated_as_utc(table: PricingTable) -> None:
    """无时区的时间戳按 UTC 处理，不应因此查不到价。"""
    usage = TokenUsage(input_tokens=1_000_000)
    naive = datetime(2026, 8, 25)
    assert table.compute("openai", "gpt-4o", usage, naive) == Decimal("2.50000000")


def test_zero_usage_costs_nothing(table: PricingTable) -> None:
    assert table.compute("openai", "gpt-4o", TokenUsage(), AT) == Decimal("0")
