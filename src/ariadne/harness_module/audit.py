"""Harness 审计日志 —— 不可变 append-only 记录。

审计要求（docs/04 第 5 节）：同规则集 + 同上下文 = 同裁决。审计记录保存
**全部命中**（不止胜出者），让事后能完整复现"当时为什么放行了"。

审计记录不可变：AuditRecord 是 frozen dataclass，Postgres 层通过
REVOKE UPDATE/DELETE 保证数据库侧也不可篡改（验收项 12）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from ariadne.harness_module.models import Action, HookKind, RuleHit


@dataclass(frozen=True)
class AuditRecord:
    """单次规则求值的审计记录。

    含全部命中规则（不只胜出者），支持事后复现完整决策链。
    timestamp 由创建时刻决定，不可变。
    """

    id: str
    project_id: UUID | None
    timestamp: str  # ISO 8601 UTC，避免 datetime 可变性问题
    hook: HookKind
    action: Action
    rule_hits: tuple[RuleHit, ...]  # 全部命中
    winning_hit: RuleHit | None
    context_snapshot: dict[str, Any]  # 求值时的上下文快照
    message: str = ""
    loop_id: str = ""

    @classmethod
    def create(
        cls,
        *,
        project_id: UUID | None,
        hook: HookKind,
        action: Action,
        rule_hits: tuple[RuleHit, ...],
        winning_hit: RuleHit | None,
        context_snapshot: dict[str, Any],
        message: str = "",
        loop_id: str = "",
    ) -> AuditRecord:
        return cls(
            id=str(uuid4()),
            project_id=project_id,
            timestamp=datetime.now(UTC).isoformat(),
            hook=hook,
            action=action,
            rule_hits=rule_hits,
            winning_hit=winning_hit,
            context_snapshot=context_snapshot,
            message=message,
            loop_id=loop_id,
        )


class AuditSink(Protocol):
    """审计写入协议。实现方保证写入是 append-only。"""

    async def write(self, record: AuditRecord) -> None: ...

    async def close(self) -> None: ...


class InMemoryAuditSink:
    """测试用内存审计池。记录全部写入，可查询与计数。"""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    async def write(self, record: AuditRecord) -> None:
        self._records.append(record)

    async def close(self) -> None:
        pass

    @property
    def records(self) -> list[AuditRecord]:
        return list(self._records)

    @property
    def count(self) -> int:
        return len(self._records)

    def latest(self) -> AuditRecord | None:
        return self._records[-1] if self._records else None


class NullAuditSink:
    """空审计池。禁用审计时用，不写任何记录。"""

    async def write(self, record: AuditRecord) -> None:
        pass

    async def close(self) -> None:
        pass


@dataclass
class AuditCollector:
    """审计收集器：聚合多个 sink，一次写入广播到全部。

    典型用法：同时写内存（实时查询）+ Postgres（持久化）。
    任何一个 sink 写失败不影响其他 sink（审计不应该因为一个 sink 挂了
    就阻断主流程），但会记录 warning 日志。
    """

    _sinks: list[AuditSink] = field(default_factory=list)

    def add_sink(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    async def write(self, record: AuditRecord) -> None:
        from ariadne.utils.logging import get_logger

        logger = get_logger(__name__)
        for sink in self._sinks:
            try:
                await sink.write(record)
            except Exception:
                logger.warning(
                    "audit sink write failed",
                    extra={"sink": type(sink).__name__, "record_id": record.id},
                )

    async def close(self) -> None:
        for sink in self._sinks:
            with suppress(Exception):
                await sink.close()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AuditCollector]:
        try:
            yield self
        finally:
            await self.close()


def _serialize_hit(hit: RuleHit) -> dict[str, Any]:
    """将 RuleHit 序列化为 JSON 可存储的 dict。"""
    return {
        "rule_id": hit.rule.id,
        "category": hit.rule.category.value,
        "hook": hit.rule.hook.value,
        "action": hit.rule.action.value,
        "severity": hit.rule.severity.value,
        "when": hit.rule.when,
        "message": hit.message,
        "value": hit.value,
    }


def write_audit_sync(sink: AuditSink, record: AuditRecord, logger: Any | None = None) -> None:
    """从同步代码写异步审计 sink。异常一律吞掉（审计不阻塞主流程）。

    从 GuardedCommandRunner._write_audit_sync 提炼的公共实现 —— GuardedLLMAdapter
    的同步路径与 GuardedArtifactWriter 共用。用 current thread 里面没有运行中的
    事件循环时直接 asyncio.run；已在事件循环内（engine 的协程）则独立线程跑
    自己的循环，否则 asyncio.run 会报 nested loop。
    """
    import asyncio
    import threading

    if logger is None:
        from ariadne.utils.logging import get_logger

        logger = get_logger(__name__)

    async def _write() -> None:
        await sink.write(record)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(_write())
        except Exception:
            logger.warning("harness audit write failed", exc_info=True)
        return

    def _in_thread() -> None:
        try:
            asyncio.run(_write())
        except Exception:
            logger.warning("harness audit write failed", exc_info=True)

    t = threading.Thread(target=_in_thread, daemon=True)
    t.start()
    t.join()


class PostgresAuditSink:
    """Postgres 审计池 —— 写 audit_log 表，append-only。

    依赖 DB 侧的 REVOKE UPDATE/DELETE 保证不可篡改（验收项 12）。
    写入失败记录 warning 但不阻断主流程（审计不应因 DB 问题中断 Loop）。
    """

    def __init__(self, pg: Any) -> None:
        """pg 需提供 async with pg.session() as session 接口。

        写入走 tenant_session 包一层而非裸 session：audit_log 是 RLS 表，
        没有租户上下文时 INSERT 会被策略拒（RLS 真生效后）。这里用模块级
        函数而不是 pg.tenant_session()，是为了让替身只需实现 session()。
        """
        self._pg = pg

    async def write(self, record: AuditRecord) -> None:
        from ariadne.auth.tenant import tenant_session
        from ariadne.storage.postgres.harness_models import AuditLogRow
        from ariadne.utils.logging import get_logger

        logger = get_logger(__name__)
        if record.project_id is None:
            # audit_log.project_id 是 NOT NULL 且受 RLS 约束，无租户的记录
            # 落不了库。提前跳出而不是等 INSERT 报错被 except 吞掉。
            logger.warning("审计记录缺 project_id，跳过入库", extra={"record_id": record.id})
            return
        try:
            async with tenant_session(self._pg, record.project_id) as session:
                loop_id = None
                if record.loop_id:
                    try:
                        loop_id = UUID(record.loop_id)
                    except ValueError:
                        loop_id = None
                row = AuditLogRow(
                    id=uuid4(),
                    project_id=record.project_id,
                    loop_id=loop_id,
                    hook=record.hook.value,
                    action=record.action.value,
                    rule_hits=[_serialize_hit(h) for h in record.rule_hits],
                    winning_hit=(
                        _serialize_hit(record.winning_hit)
                        if record.winning_hit
                        else None
                    ),
                    context_snapshot=record.context_snapshot,
                    message=record.message,
                )
                session.add(row)
        except Exception as exc:
            logger.warning(
                "audit log write failed",
                extra={"record_id": record.id, "error": str(exc)},
            )

    async def close(self) -> None:
        pass


__all__ = [
    "AuditCollector",
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
    "NullAuditSink",
    "PostgresAuditSink",
    "write_audit_sync",
]
