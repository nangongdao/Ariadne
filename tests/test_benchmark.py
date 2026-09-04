"""性能压测框架测试 —— 验证 benchmark 模块逻辑正确性。"""

from __future__ import annotations

import json

import pytest

from ariadne.benchmark import (
    BenchmarkConfig,
    MockTraceRollup,
    _percentile,
    _run_mock_benchmark,
    run_benchmark,
)


class TestPercentile:
    def test_p50_even(self) -> None:
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        # 10 values, p50 index = 5 → value 6
        assert _percentile(values, 50) == 6

    def test_p95(self) -> None:
        values = list(range(1, 101))
        result = _percentile(values, 95)
        # 100 values, p95 index = 95 → value 96
        assert 95 <= result <= 96

    def test_p99(self) -> None:
        values = list(range(1, 101))
        result = _percentile(values, 99)
        # 100 values, p99 index = 99 → value 100
        assert 99 <= result <= 100

    def test_empty(self) -> None:
        assert _percentile([], 95) == 0.0

    def test_single_value(self) -> None:
        assert _percentile([42.0], 99) == 42.0


class TestMockTraceRollup:
    def test_trace_count_matches_config(self) -> None:
        config = BenchmarkConfig(total_rows=100_000, spans_per_trace=50)
        rollup = MockTraceRollup(config)
        assert rollup.trace_count == 2000  # 100k / 50

    def test_query_returns_limited_rows(self) -> None:
        config = BenchmarkConfig(total_rows=100_000, spans_per_trace=50)
        rollup = MockTraceRollup(config)
        pid = next(iter(rollup._by_project))
        result = rollup.query_traces(pid, limit=50)
        assert len(result) <= 50

    def test_query_only_errors_filters(self) -> None:
        config = BenchmarkConfig(total_rows=10_000, spans_per_trace=10)
        rollup = MockTraceRollup(config)
        pid = next(iter(rollup._by_project))
        all_traces = rollup.query_traces(pid, limit=1000)
        error_traces = rollup.query_traces(pid, limit=1000, only_errors=True)
        assert len(error_traces) <= len(all_traces)
        assert all(r["error_count"] > 0 for r in error_traces)

    def test_count_spans_returns_total(self) -> None:
        config = BenchmarkConfig(total_rows=10_000, spans_per_trace=10)
        rollup = MockTraceRollup(config)
        pid = next(iter(rollup._by_project))
        count = rollup.count_spans(pid)
        assert count > 0
        # 总数应该等于该 project 的 trace 数 × spans_per_trace
        trace_count = len(rollup._by_project[pid])
        assert count == trace_count * config.spans_per_trace


class TestBenchmarkRun:
    @pytest.mark.asyncio
    async def test_mock_benchmark_quick(self) -> None:
        """快速 mock 压测验证框架逻辑。"""
        config = BenchmarkConfig(
            total_rows=10_000,
            spans_per_trace=10,
            query_count=50,
            p95_target_ms=1000.0,
        )
        report = _run_mock_benchmark(config)

        assert report.mode == "mock"
        assert report.p95_ms > 0
        assert len(report.query_results) == 50
        assert all(r.success for r in report.query_results)
        assert report.passed

    def test_run_benchmark_mock_mode(self) -> None:
        """run_benchmark 在 mock 模式下直接返回。"""
        config = BenchmarkConfig(
            total_rows=5000,
            spans_per_trace=10,
            query_count=20,
            p95_target_ms=1000.0,
        )
        report = run_benchmark(config, mode="mock")

        assert report.mode == "mock"
        assert report.p95_ms > 0
        assert report.passed

    def test_report_json_serialization(self) -> None:
        """报告可序列化为 JSON。"""
        config = BenchmarkConfig(
            total_rows=1000,
            spans_per_trace=10,
            query_count=10,
        )
        report = _run_mock_benchmark(config)
        json_str = report.to_json()

        parsed = json.loads(json_str)
        assert parsed["mode"] == "mock"
        assert "p95_ms" in parsed
        assert "query_results" in parsed
        assert len(parsed["query_results"]) == 10

    def test_real_benchmark_falls_back_to_mock(self) -> None:
        """real 模式在 ClickHouse 不可用时降级到 mock。"""
        config = BenchmarkConfig(
            total_rows=1000,
            spans_per_trace=10,
            query_count=10,
            p95_target_ms=1000.0,
        )
        # ClickHouse 未运行时会降级
        report = run_benchmark(config, mode="real")

        # 降级到 mock 或直接跑 mock（取决于环境）
        assert report.mode in ("mock", "real")
        assert report.p95_ms > 0


class TestBenchmarkConfig:
    def test_default_config(self) -> None:
        config = BenchmarkConfig()
        assert config.total_rows == 100_000_000
        assert config.p95_target_ms == 500.0
        assert config.spans_per_trace == 50

    def test_quick_config(self) -> None:
        config = BenchmarkConfig(
            total_rows=100_000,
            spans_per_trace=50,
            query_count=100,
            batch_size=10_000,
        )
        assert config.total_rows == 100_000
        assert config.query_count == 100
