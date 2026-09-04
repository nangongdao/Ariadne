"""PricingRepository —— model_pricing 表的读写。

存在理由：`PricingTable` 的默认价硬编码在 telemetry/pricing.py 里，全仓 4 处
调用点（SpanProcessor / AnthropicLLMClient / OpenAILLMClient）都直接用裸
PricingTable()，model_pricing 表（含 effective_from/effective_to 两列）只建了
表、从没被读过。效果：provider 调价后**任何**入口都无法更新价格 ——
内建默认价是系统里唯一的定价来源。

本仓储把表变成 PricingTable 的数据源：
- load() 读出全部生效行 → 构造带时间区间的表（历史记录仍按当时价格计，
  这正是 effective_from/effective_to 存在的意义）
- upsert_price() 写价（管理侧用；先删同 provider/model 的开启区间，
  再插新区间 —— 语义：后写覆盖先写，不产生重叠区间）

优雅降级：表缺失/读失败时返回内置默认表并告警 —— 定价缺失不该阻断
成本计算，成本计算不该阻断 LLM 调用。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.models import ModelPricing
from ariadne.telemetry.pricing import Price, PriceEntry, PricingTable
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


def _aware(dt: datetime) -> datetime:
    """SQLite 读回的 naive 时间补 UTC；PG 返回 aware 时原样透传。"""
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def _aware_opt(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return _aware(dt)

# 保留区间数量上限：防止误操作/恶意操作把表堆满。
MAX_ACTIVE_ENTRIES = 200
# 历史区间的保留时长：一次 upsert 会关闭当前开启区间（写 effective_to），
# 默认保留这些已关闭区间，防止点错把历史价格永久清掉。
RETENTION_DAYS = 365


class PricingRepository:
    """model_pricing 表读写。"""

    def __init__(self, session: AsyncSession, *, defaults: PricingTable | None = None) -> None:
        """defaults：表缺失/空的回退表，缺省用内置默认价。"""
        self._session = session
        self._defaults = defaults or PricingTable()

    async def load(self) -> PricingTable:
        """读全表构造 PricingTable；表缺失/读失败时回退内置默认表。"""
        try:
            rows = list(
                (
                    await self._session.execute(
                        select(ModelPricing)
                        .order_by(
                            ModelPricing.provider,
                            ModelPricing.model,
                            ModelPricing.effective_from,
                        )
                        .limit(MAX_ACTIVE_ENTRIES * 2)
                    )
                )
                .scalars()
                .all()
            )
        except Exception as exc:
            logger.warning(
                "读取 model_pricing 失败，回退内置默认计价表",
                extra={"error": str(exc)},
            )
            return self._defaults

        entries: list[PriceEntry] = []
        for row in rows:
            price = Price(
                input_=row.input_per_million,
                output=row.output_per_million,
                cache_read=row.cache_read_per_million,
                cache_write=row.cache_write_per_million,
                reasoning=row.reasoning_per_million,
            )
            entries.append(
                PriceEntry(
                    provider=row.provider,
                    model=row.model,
                    price=price,
                    # SQLite 把 DateTime(timezone=True) 读回成 naive，
                    # PricingTable.lookup 的区间比较发生在 Python 侧，
                    # naive 与 aware 相撞会抛 TypeError（与 loop_runs
                    # 同源的坑，只是这里判定必须留在内存里做）。
                    effective_from=_aware(row.effective_from),
                    effective_to=_aware_opt(row.effective_to),
                )
            )
        if not entries:
            # 空表回退 default 表 —— 与"调价前"等价，且不会让成本归因突然归零
            return self._defaults
        return PricingTable(tuple(entries))

    async def upsert_price(
        self,
        *,
        provider: str,
        model: str,
        price: Price,
        effective_from: datetime | None = None,
    ) -> None:
        """写入/更新一条价格，先关闭同 (provider, model) 的当前开启区间。

        语义：后写覆盖先写。同 provider/model 若已有 effective_to 为 NULL
        的行（当前生效价），先写 effective_to = 新 effective_from，再插入新行。
        effective_from 缺省为当前时刻。

        Raises:
            ValueError: provider/model 为空，或价格含负数。
        """
        if not provider or not model:
            raise ValueError("provider 和 model 不能为空")
        for name, value in price._asdict().items():
            if value < 0:
                raise ValueError(f"{name} 不能为负: {value}")

        at = effective_from or datetime.now(UTC)
        now = datetime.now(UTC)
        if at.tzinfo is None:
            at = at.replace(tzinfo=UTC)

        current = (
            await self._session.execute(
                select(ModelPricing.id).where(
                    ModelPricing.provider == provider,
                    ModelPricing.model == model,
                    ModelPricing.effective_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        if current is not None:
            await self._session.execute(
                update(ModelPricing)
                .where(ModelPricing.id == current)
                .values(effective_to=at)
            )

        # 清理超过保留期的已关闭区间
        cutoff = now - timedelta(days=RETENTION_DAYS)
        await self._session.execute(
            delete(ModelPricing)
            .where(
                ModelPricing.provider == provider,
                ModelPricing.model == model,
                ModelPricing.effective_to.is_not(None),
                ModelPricing.effective_to < cutoff,
            )
        )

        row = ModelPricing(
            id=int(int.from_bytes(uuid.uuid4().bytes, "big") & 0x7FFFFFFFFFFFFFFF),
            provider=provider,
            model=model,
            input_per_million=price.input_,
            output_per_million=price.output,
            cache_read_per_million=price.cache_read,
            cache_write_per_million=price.cache_write,
            reasoning_per_million=price.reasoning,
            effective_from=at,
            effective_to=None,
        )
        self._session.add(row)
        await self._session.flush()
        logger.info(
            "upsert pricing",
            extra={"provider": provider, "model": model, "effective_from": at.isoformat()},
        )


__all__ = ["MAX_ACTIVE_ENTRIES", "RETENTION_DAYS", "PricingRepository"]
