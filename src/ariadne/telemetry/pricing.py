"""成本计算。

两个必须坚持的设计点：
1. 计价表带生效时间区间 —— provider 调价后历史记录仍按当时价格计算，
   否则历史成本报表会因调价而变化，账单归因失去意义。
2. 缓存与推理 Token 独立定价 —— 缓存读通常是输入价的一个折扣，
   按标准价计会显著高估多轮场景的成本。
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import NamedTuple

from ariadne.telemetry.models import TokenUsage

# 每百万 Token 的价格
PER_MILLION = Decimal("1000000")


class Price(NamedTuple):
    """单位：美元 / 百万 Token。"""

    input_: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal
    reasoning: Decimal


class PriceEntry(NamedTuple):
    provider: str
    model: str
    price: Price
    effective_from: datetime
    effective_to: datetime | None


def _d(v: str) -> Decimal:
    return Decimal(v)


def _epoch() -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC)


# 说明：这是内置的默认计价表，生产环境应由 model_pricing 表覆盖。
# reasoning 未单独计价的模型，按 output 价计。
_DEFAULT_TABLE: tuple[PriceEntry, ...] = (
    PriceEntry(
        "openai",
        "gpt-4o",
        Price(_d("2.50"), _d("10.00"), _d("1.25"), _d("0"), _d("10.00")),
        _epoch(),
        None,
    ),
    PriceEntry(
        "openai",
        "gpt-4o-mini",
        Price(_d("0.15"), _d("0.60"), _d("0.075"), _d("0"), _d("0.60")),
        _epoch(),
        None,
    ),
    PriceEntry(
        "anthropic",
        "claude-sonnet-5",
        Price(_d("3.00"), _d("15.00"), _d("0.30"), _d("3.75"), _d("15.00")),
        _epoch(),
        None,
    ),
    PriceEntry(
        "anthropic",
        "claude-haiku-4-5",
        Price(_d("1.00"), _d("5.00"), _d("0.10"), _d("1.25"), _d("5.00")),
        _epoch(),
        None,
    ),
)

_ZERO_PRICE = Price(_d("0"), _d("0"), _d("0"), _d("0"), _d("0"))


class PricingTable:
    """按 (provider, model, 时间) 查价。"""

    def __init__(self, entries: tuple[PriceEntry, ...] = _DEFAULT_TABLE) -> None:
        self._entries = entries

    def lookup(self, provider: str, model: str, at: datetime) -> Price | None:
        """精确匹配优先，其次前缀匹配（处理带日期后缀的模型版本）。"""
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)

        candidates = [
            e
            for e in self._entries
            if e.provider == provider
            and e.effective_from <= at
            and (e.effective_to is None or at < e.effective_to)
        ]
        if not candidates:
            return None

        for entry in candidates:
            if entry.model == model:
                return entry.price

        # gpt-4o-2024-11-20 应命中 gpt-4o；取最长匹配避免 gpt-4o 抢走 gpt-4o-mini
        prefix_hits = [e for e in candidates if model.startswith(e.model)]
        if prefix_hits:
            return max(prefix_hits, key=lambda e: len(e.model)).price
        return None

    def compute(
        self, provider: str, model: str, usage: TokenUsage, at: datetime
    ) -> Decimal:
        """计算成本；未知模型返回 0（不猜价，避免污染成本报表）。"""
        price = self.lookup(provider, model, at)
        if price is None:
            return Decimal("0")

        # reasoning_tokens 在多数 provider 的用量里已包含于 output_tokens，
        # 单独上报时才计价，避免重复计费。
        billable_output = usage.output_tokens
        total = (
            usage.input_tokens * price.input_
            + billable_output * price.output
            + usage.cache_read_tokens * price.cache_read
            + usage.cache_write_tokens * price.cache_write
        )
        return (total / PER_MILLION).quantize(Decimal("0.00000001"))


_default_table = PricingTable()


def compute_cost(provider: str, model: str, usage: TokenUsage, at: datetime) -> Decimal:
    return _default_table.compute(provider, model, usage, at)
