"""PricingRepository 测试：model_pricing 表读写与历史计价。

表自建表以来从没被读过（PricingTable 的默认价硬编码），这些测试证明
仓储把表接成定价数据源：
- load() 构造带时间区间的表 → lookup(at) 按时刻计价，调价前的记录
  仍按当时价格算（effective_from/effective_to 的核心语义）
- upsert_price() 后写覆盖先写：关闭当前开启区间，插新开启区间
- 表缺失/空表回退默认价，成本计算不被阻断
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from ariadne.storage.postgres.repositories.pricing import PricingRepository
from ariadne.telemetry.pricing import Price, PricingTable


def _price(
    inp: str = "9.99", out: str = "19.99", cr: str = "0", cw: str = "0", r: str = "0"
) -> Price:
    return Price(
        input_=Decimal(inp),
        output=Decimal(out),
        cache_read=Decimal(cr),
        cache_write=Decimal(cw),
        reasoning=Decimal(r),
    )


class TestPricingRepository:
    async def test_load_empty_table_returns_defaults(self, memory_pg: Any) -> None:
        """空表 → 默认价表（内置价），成本计算不归零。"""
        async with memory_pg.session() as session:
            table = await PricingRepository(session).load()
        assert isinstance(table, PricingTable)
        # 内置默认价存在（gpt-4o 2.50/10.00）
        price = table.lookup("openai", "gpt-4o", datetime.now(UTC))
        assert price is not None
        assert price.input_ == Decimal("2.50")

    async def test_upsert_then_load_overrides_default(self, memory_pg: Any) -> None:
        """upsert 一条价 → load 出的表按新价计。"""
        async with memory_pg.session() as session:
            repo = PricingRepository(session)
            await repo.upsert_price(
                provider="openai", model="gpt-4o", price=_price(inp="3.00", out="12.00")
            )
        async with memory_pg.session() as session:
            table = await PricingRepository(session).load()
        price = table.lookup("openai", "gpt-4o", datetime.now(UTC))
        assert price is not None
        assert price.input_ == Decimal("3.00")
        assert price.output == Decimal("12.00")

    async def test_upsert_closes_previous_interval(self, memory_pg: Any) -> None:
        """后写覆盖先写：旧区间被关闭，历史时刻仍按旧价计。

        这是 effective_from/effective_to 存在的意义 —— provider 调价后，
        历史成本报表不随调价变化。
        """
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        async with memory_pg.session() as session:
            repo = PricingRepository(session)
            await repo.upsert_price(
                provider="anthropic",
                model="claude-sonnet-5",
                price=_price(inp="3.00", out="15.00"),
                effective_from=t0,
            )
            await repo.upsert_price(
                provider="anthropic",
                model="claude-sonnet-5",
                price=_price(inp="4.00", out="20.00"),
                effective_from=t0 + timedelta(days=30),
            )

        async with memory_pg.session() as session:
            table = await PricingRepository(session).load()

        # 调价前的时刻 → 旧价；调价后 → 新价
        old = table.lookup("anthropic", "claude-sonnet-5", t0 + timedelta(days=10))
        new = table.lookup(
            "anthropic", "claude-sonnet-5", t0 + timedelta(days=31)
        )
        assert old is not None and old.input_ == Decimal("3.00")
        assert new is not None and new.input_ == Decimal("4.00")

    async def test_load_fallback_on_missing_table(self, memory_pg: Any) -> None:
        """表不存在 → 回退默认价表，不抛异常（优雅降级）。"""
        async with memory_pg.session() as session:
            await session.execute(
                __import__("sqlalchemy").text("DROP TABLE model_pricing")
            )
        async with memory_pg.session() as session:
            table = await PricingRepository(session).load()
        assert isinstance(table, PricingTable)
        assert table.lookup("openai", "gpt-4o", datetime.now(UTC)) is not None

    async def test_upsert_rejects_negative_price(self, memory_pg: Any) -> None:
        """负价拒绝写入（计价表的有效性防线）。"""
        async with memory_pg.session() as session:
            repo = PricingRepository(session)
            with pytest.raises(ValueError):
                await repo.upsert_price(
                    provider="openai", model="gpt-4o", price=_price(inp="-1.00")
                )

    async def test_seed_rows_load_as_default_prices(self, memory_pg: Any) -> None:
        """迁移播种的 4 行默认价 → 仓储读出的表与内置默认表等值。

        验证「种子迁移产物 ↔ 仓储读取」闭环：迁移只往 SQLite 可执行的
        INSERT 里塞行，load() 能把这些行变成可查价的表。
        """
        from sqlalchemy import text

        epoch = "2024-01-01 00:00:00+00:00"
        seeds = [
            ("openai", "gpt-4o", "2.50", "10.00", "1.25", "0", "10.00"),
            ("openai", "gpt-4o-mini", "0.15", "0.60", "0.075", "0", "0.60"),
            ("anthropic", "claude-sonnet-5", "3.00", "15.00", "0.30", "3.75", "15.00"),
            ("anthropic", "claude-haiku-4-5", "1.00", "5.00", "0.10", "1.25", "5.00"),
        ]
        async with memory_pg.session() as session:
            for p, m, i, o, cr, cw, r in seeds:
                await session.execute(
                    text(
                        "INSERT INTO model_pricing "
                        "(provider, model, input_per_million, output_per_million, "
                        " cache_read_per_million, cache_write_per_million, "
                        " reasoning_per_million, effective_from, effective_to, "
                        " created_at, updated_at) "
                        "VALUES (:p, :m, :i, :o, :cr, :cw, :r, :f, NULL, "
                        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                    ),
                    {"p": p, "m": m, "i": i, "o": o, "cr": cr, "cw": cw, "r": r, "f": epoch},
                )

        async with memory_pg.session() as session:
            table = await PricingRepository(session).load()

        now = datetime.now(UTC)
        assert table.lookup("openai", "gpt-4o", now).input_ == Decimal("2.50")
        assert (
            table.lookup("anthropic", "claude-sonnet-5", now).cache_write
            == Decimal("3.75")
        )

    def test_migration_prices_mirror_builtin_default_table(self) -> None:
        """迁移种子与 telemetry 内置默认价必须同步 —— 防止只改一处造成漂移。

        漂移的表现：迁移播种价与代码默认价不一致，同一种模型在不同部署
        得到不同成本。用加载迁移文件的方式直接比对两边的 4 行。
        """
        import importlib.util
        from pathlib import Path

        from ariadne.telemetry.pricing import _DEFAULT_TABLE

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "alembic"
            / "versions"
            / "f7a8b9c0d1e2_seed_model_pricing.py"
        )
        spec = importlib.util.spec_from_file_location("_seed_pricing", migration_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        seeds = module._DEFAULT_PRICES
        # 迁移元组顺序: (provider, model, input, output, cache_read, cache_write, reasoning)
        assert len(seeds) == len(_DEFAULT_TABLE)
        for entry, seed in zip(_DEFAULT_TABLE, seeds, strict=True):
            assert entry.provider == seed[0]
            assert entry.model == seed[1]
            assert str(entry.price.input_) == seed[2]
            assert str(entry.price.output) == seed[3]
            assert str(entry.price.cache_read) == seed[4]
            assert str(entry.price.cache_write) == seed[5]
            assert str(entry.price.reasoning) == seed[6]


class TestCollectorPricing:
    """Collector 成本归因走 model_pricing 表 —— 全链路闭环。

    SpanProcessor（Collector 内部）默认用内置价；set_db_pricing 注入
    PricingRepository.load() 的结果后，成本按表计算。这是成本归因报表
    （/v1/costs）的数据来源。
    """

    async def test_db_pricing_drives_span_cost(self, memory_pg: Any) -> None:
        """upsert 一条自定义价 → load 出的表注入 processor → span 成本按表算。"""
        from datetime import UTC, datetime
        from uuid import UUID

        from ariadne.config import ApiSettings, Settings
        from ariadne.telemetry.models import AriadneSpan, SpanKind, TokenUsage
        from ariadne.worker.processor import SpanProcessor

        async with memory_pg.session() as session:
            repo = PricingRepository(session)
            # effective_from 显式给 2026-01-01（早于 span 时间 2026-06-01），
            # 否则默认 now(=今天) 会让 lookup(at=6月) 落在开启区间之前 → None
            await repo.upsert_price(
                provider="openai",
                model="gpt-4o",
                price=Price(
                    input_=Decimal("20.00"),
                    output=Decimal("40.00"),
                    cache_read=Decimal("0"),
                    cache_write=Decimal("0"),
                    reasoning=Decimal("0"),
                ),
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
            )

        async with memory_pg.session() as session:
            table = await PricingRepository(session).load()

        settings = Settings(
            api=ApiSettings(
                static_api_key="ak_test_key", default_project_id=UUID(int=0)
            )
        )
        processor = SpanProcessor(settings)
        processor.set_db_pricing(table)

        span = AriadneSpan(
            project_id=UUID(int=0),
            trace_id="a" * 32,
            span_id="b" * 16,
            name="llm",
            kind=SpanKind.LLM,
            provider="openai",
            model_request="gpt-4o",
            started_at=datetime(2026, 6, 1, tzinfo=UTC),
            usage=TokenUsage(input_tokens=1_000_000, output_tokens=500_000),
        )

        result = processor.process_batch(
            "native", [span.model_dump(mode="json")], span.project_id
        )
        assert len(result.spans) == 1
        enriched = result.spans[0]
        # input 20 美元/million × 1M + output 40 × 0.5M = 20 + 20 = 40 美元
        assert enriched.cost_usd == Decimal("40.00")
