"""Harness audit 测试 —— 不可变审计记录。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ariadne.harness_module.audit import (
    AuditCollector,
    AuditRecord,
    InMemoryAuditSink,
    NullAuditSink,
)
from ariadne.harness_module.models import Action, HookKind, Rule, RuleCategory, RuleHit


def make_audit_record(
    action: Action = Action.BLOCK,
    hits: tuple[RuleHit, ...] = (),
    winning: RuleHit | None = None,
) -> AuditRecord:
    return AuditRecord.create(
        project_id=uuid4(),
        hook=HookKind.PRE_MODEL,
        action=action,
        rule_hits=hits,
        winning_hit=winning,
        context_snapshot={"input": {"text": "test"}},
    )


class TestAuditRecord:
    def test_create_generates_id(self) -> None:
        r = make_audit_record()
        assert r.id
        assert len(r.id) > 0

    def test_create_generates_timestamp(self) -> None:
        r = make_audit_record()
        assert r.timestamp
        assert "T" in r.timestamp  # ISO format

    def test_immutable(self) -> None:
        r = make_audit_record()
        with pytest.raises(AttributeError):
            r.action = Action.ALLOW  # type: ignore[misc]

    def test_each_create_unique_id(self) -> None:
        r1 = make_audit_record()
        r2 = make_audit_record()
        assert r1.id != r2.id

    def test_preserves_all_hits(self) -> None:
        rule = Rule(
            id="test",
            category=RuleCategory.INPUT,
            hook=HookKind.PRE_MODEL,
            when="true",
        )
        h1 = RuleHit(rule=rule)
        h2 = RuleHit(rule=rule)
        r = make_audit_record(hits=(h1, h2), winning=h1)
        assert len(r.rule_hits) == 2
        assert r.winning_hit is h1


class TestInMemoryAuditSink:
    def test_write_and_read(self) -> None:
        sink = InMemoryAuditSink()
        r = make_audit_record()
        import asyncio

        asyncio.run(sink.write(r))
        assert sink.count == 1
        assert sink.latest() is r

    def test_multiple_writes(self) -> None:
        sink = InMemoryAuditSink()
        import asyncio

        for _ in range(5):
            asyncio.run(sink.write(make_audit_record()))
        assert sink.count == 5

    def test_records_property_returns_copy(self) -> None:
        sink = InMemoryAuditSink()
        import asyncio

        asyncio.run(sink.write(make_audit_record()))
        records = sink.records
        records.clear()
        assert sink.count == 1  # original unaffected

    def test_close_noop(self) -> None:
        sink = InMemoryAuditSink()
        import asyncio

        asyncio.run(sink.close())  # should not raise


class TestNullAuditSink:
    def test_write_noop(self) -> None:
        sink = NullAuditSink()
        import asyncio

        asyncio.run(sink.write(make_audit_record()))
        # No records stored, no error

    def test_close_noop(self) -> None:
        sink = NullAuditSink()
        import asyncio

        asyncio.run(sink.close())


class TestAuditCollector:
    def test_collect_to_multiple_sinks(self) -> None:
        collector = AuditCollector()
        s1 = InMemoryAuditSink()
        s2 = InMemoryAuditSink()
        collector.add_sink(s1)
        collector.add_sink(s2)

        import asyncio

        r = make_audit_record()
        asyncio.run(collector.write(r))
        assert s1.count == 1
        assert s2.count == 1

    def test_sink_failure_does_not_block_others(self) -> None:
        collector = AuditCollector()

        class FailingSink:
            async def write(self, record: AuditRecord) -> None:
                raise RuntimeError("sink broken")

            async def close(self) -> None:
                pass

        good_sink = InMemoryAuditSink()
        collector.add_sink(FailingSink())  # type: ignore[arg-type]
        collector.add_sink(good_sink)

        import asyncio

        asyncio.run(collector.write(make_audit_record()))
        assert good_sink.count == 1  # good sink still got the record
