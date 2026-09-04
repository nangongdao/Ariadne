"""尾部采样测试 —— loop_id 粒度，30s 缓冲，超时降级。

M6 §4.4：同一 Loop 的所有轮次必须一起保留或一起丢弃。
M6 §4.5：自监控绕过采样。
"""

from __future__ import annotations

import uuid

from ariadne.telemetry.models import AriadneSpan, SpanKind, TokenUsage
from ariadne.telemetry.sampling import (
    INTERNAL_PROJECT_ID,
    HeadSampler,
    TailSampler,
)

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
INTERNAL_PROJECT = INTERNAL_PROJECT_ID


def make_span(
    *,
    loop_id: str = "",
    trace_id: str = "a" * 32,
    span_id: str = "",
    error_type: str = "",
    project_id: uuid.UUID | None = None,
) -> AriadneSpan:
    if not span_id:
        span_id = uuid.uuid4().hex[:16]
    return AriadneSpan(
        project_id=project_id or PROJECT_ID,
        trace_id=trace_id,
        span_id=span_id,
        name="test",
        kind=SpanKind.LLM,
        operation="chat",
        provider="openai",
        model_request="gpt-4o",
        started_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
        duration_ms=100,
        status="error" if error_type else "ok",
        error_type=error_type,
        usage=TokenUsage(),
        loop_id=loop_id,
        attributes={},
        tags=[],
    )


class TestHeadSampler:
    def test_rate_1_always_samples(self) -> None:
        sampler = HeadSampler(rate=1.0)
        span = make_span(trace_id="a" * 32)
        assert sampler.should_sample(span)

    def test_rate_0_never_samples(self) -> None:
        sampler = HeadSampler(rate=0.0)
        span = make_span(trace_id="a" * 32)
        assert not sampler.should_sample(span)

    def test_deterministic_same_trace_same_decision(self) -> None:
        """同一 trace_id 总是得到相同决策。"""
        sampler = HeadSampler(rate=0.5)
        span = make_span(trace_id="deadbeef" * 4)
        assert sampler.should_sample(span) == sampler.should_sample(span)

    def test_internal_project_always_samples(self) -> None:
        sampler = HeadSampler(rate=0.0)
        span = make_span(project_id=INTERNAL_PROJECT)
        assert sampler.should_sample(span)


class TestTailSampler:
    def test_non_loop_span_immediate_head_decision(self) -> None:
        """无 loop_id 的 span 走头部采样，立即决策。"""
        sampler = TailSampler(HeadSampler(rate=1.0))
        span = make_span(loop_id="")
        sampler.add(span)
        kept, dropped = sampler.drain_ready()
        assert len(kept) == 1
        assert dropped == 0

    def test_non_loop_span_dropped_by_head_rate(self) -> None:
        sampler = TailSampler(HeadSampler(rate=0.0))
        span = make_span(loop_id="")
        sampler.add(span)
        kept, dropped = sampler.drain_ready()
        assert len(kept) == 0
        assert dropped == 1

    def test_loop_spans_buffered_until_marked_done(self) -> None:
        """有 loop_id 的 span 缓冲，直到 mark_loop_done 才决策。"""
        sampler = TailSampler(HeadSampler(rate=1.0))
        s1 = make_span(loop_id="loop-1", trace_id="a1" * 16)
        s2 = make_span(loop_id="loop-1", trace_id="a1" * 16)
        sampler.add(s1)
        sampler.add(s2)
        # 未标记完成 → drain 不返回（但可能因超时返回，这里立即调用所以不超时）
        kept, _ = sampler.drain_ready()
        assert len(kept) == 0
        assert sampler.buffered_loop_count == 1
        assert sampler.buffered_span_count == 2

        # 标记完成 → 全部保留
        sampler.mark_loop_done("loop-1")
        kept, dropped = sampler.drain_ready()
        assert len(kept) == 2
        assert dropped == 0

    def test_loop_all_or_nothing(self) -> None:
        """rate=0 时整个 loop 丢弃，不拆分。"""
        sampler = TailSampler(HeadSampler(rate=0.0))
        s1 = make_span(loop_id="loop-1", trace_id="a1" * 16)
        s2 = make_span(loop_id="loop-1", trace_id="a1" * 16)
        s3 = make_span(loop_id="loop-1", trace_id="a1" * 16)
        for s in (s1, s2, s3):
            sampler.add(s)
        sampler.mark_loop_done("loop-1")
        kept, dropped = sampler.drain_ready()
        assert len(kept) == 0
        assert dropped == 3

    def test_error_loop_always_kept(self) -> None:
        """有 error_type 的 loop 即使 rate=0 也保留。"""
        sampler = TailSampler(HeadSampler(rate=0.0), keep_errors=True)
        s1 = make_span(loop_id="loop-1", trace_id="a1" * 16)
        s2 = make_span(loop_id="loop-1", trace_id="a1" * 16, error_type="timeout")
        sampler.add(s1)
        sampler.add(s2)
        sampler.mark_loop_done("loop-1")
        kept, dropped = sampler.drain_ready()
        assert len(kept) == 2
        assert dropped == 0

    def test_internal_project_bypasses_sampling(self) -> None:
        """自监控 span 绕过采样。"""
        sampler = TailSampler(HeadSampler(rate=0.0))
        span = make_span(project_id=INTERNAL_PROJECT, loop_id="loop-x")
        sampler.add(span)
        kept, dropped = sampler.drain_ready()
        assert len(kept) == 1
        assert dropped == 0

    def test_mark_done_nonexistent_loop_noop(self) -> None:
        sampler = TailSampler(HeadSampler(rate=1.0))
        sampler.mark_loop_done("nonexistent")
        kept, _ = sampler.drain_ready()
        assert len(kept) == 0
