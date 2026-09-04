"""SDK exporter 测试。

核心契约：**观测组件绝不能拖垮业务**。这里验证三条：不阻塞、不外抛、
队列满时丢数据而非反压。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx
import pytest

from ariadne_sdk.exporter import SpanExporter


class FakeTransport(httpx.BaseTransport):
    """可控的 HTTP 桩：记录请求、按需返回错误或延迟。"""

    def __init__(self, status: int = 202, delay: float = 0.0) -> None:
        self.status = status
        self.delay = delay
        self.batches: list[list[dict[str, Any]]] = []
        self.lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.delay:
            time.sleep(self.delay)
        import json

        with self.lock:
            self.batches.append(json.loads(request.content)["spans"])
        return httpx.Response(self.status, json={"accepted": 1})

    @property
    def total_spans(self) -> int:
        with self.lock:
            return sum(len(b) for b in self.batches)


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


def make_exporter(transport: FakeTransport, **kwargs: Any) -> SpanExporter:
    exporter = SpanExporter(
        endpoint="http://test/v1/ingest/spans", api_key="k", **kwargs
    )
    exporter.start()
    # 替换后台线程建立的 client（start 后才有）
    deadline = time.monotonic() + 2
    while exporter._client is None and time.monotonic() < deadline:
        time.sleep(0.01)
    exporter._client = httpx.Client(transport=transport)
    return exporter


def test_spans_sent_in_batches(transport: FakeTransport) -> None:
    exporter = make_exporter(transport, batch_size=3, flush_interval=0.1)
    try:
        for i in range(6):
            exporter.submit({"span_id": f"s{i}"})
        assert exporter.flush(timeout=5)
        assert transport.total_spans == 6
        assert exporter.stats.sent == 6
    finally:
        exporter.shutdown()


def test_submit_never_blocks(transport: FakeTransport) -> None:
    """业务线程上的 submit 必须极快，即使后端很慢。"""
    slow = FakeTransport(delay=0.5)
    exporter = make_exporter(slow, batch_size=1, flush_interval=0.05)
    try:
        start = time.perf_counter()
        for i in range(100):
            exporter.submit({"span_id": f"s{i}"})
        elapsed_ms = (time.perf_counter() - start) * 1000
        # 100 次入队应远低于单次请求的 500ms
        assert elapsed_ms < 100, f"submit 阻塞了业务线程: {elapsed_ms:.1f}ms"
    finally:
        exporter.shutdown()


def test_queue_full_drops_instead_of_blocking(transport: FakeTransport) -> None:
    """队列满时丢最旧的并计数，不反压业务线程。"""
    blocked = FakeTransport(delay=5.0)
    exporter = make_exporter(blocked, queue_size=10, batch_size=1, flush_interval=10)
    try:
        start = time.perf_counter()
        for i in range(200):
            exporter.submit({"span_id": f"s{i}"})
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, "队列满时发生了反压"
        assert exporter.stats.dropped_queue_full > 0
        assert exporter.stats.submitted == 200
    finally:
        exporter.shutdown()


def test_client_error_not_retried(transport: FakeTransport) -> None:
    """4xx 重试没有意义（schema 错、key 错），必须立即放弃。"""
    bad_request = FakeTransport(status=400)
    exporter = make_exporter(bad_request, batch_size=1, flush_interval=0.05)
    try:
        exporter.submit({"span_id": "s1"})
        exporter.flush(timeout=3)
        time.sleep(0.3)
        assert len(bad_request.batches) == 1, "4xx 不应重试"
        assert exporter.stats.dropped_failed == 1
    finally:
        exporter.shutdown()


def test_server_error_is_retried(transport: FakeTransport) -> None:
    """5xx 是暂时性故障，应该重试。"""
    server_error = FakeTransport(status=503)
    exporter = make_exporter(server_error, batch_size=1, flush_interval=0.05)
    try:
        exporter.submit({"span_id": "s1"})
        time.sleep(2.5)
        assert len(server_error.batches) > 1, "5xx 应该重试"
    finally:
        exporter.shutdown()


def test_submit_never_raises() -> None:
    """即使 exporter 内部状态异常，submit 也不能向业务抛异常。"""
    exporter = SpanExporter(endpoint="http://invalid", api_key="k")
    # 刻意不 start()，后台线程不存在
    exporter.submit({"span_id": "s1"})
    assert exporter.stats.submitted == 1


def test_flush_waits_for_inflight(transport: FakeTransport) -> None:
    """flush 必须等到在途批次结算完，否则进程退出会丢最后一批。"""
    slow = FakeTransport(delay=0.3)
    exporter = make_exporter(slow, batch_size=5, flush_interval=0.05)
    try:
        for i in range(5):
            exporter.submit({"span_id": f"s{i}"})
        assert exporter.flush(timeout=5)
        # flush 返回时数据已真正发出
        assert slow.total_spans == 5
    finally:
        exporter.shutdown()


def test_stats_snapshot_shape(transport: FakeTransport) -> None:
    exporter = make_exporter(transport, batch_size=1, flush_interval=0.05)
    try:
        exporter.submit({"span_id": "s1"})
        exporter.flush(timeout=3)
        snapshot = exporter.stats.snapshot()
        assert set(snapshot) == {
            "submitted", "sent", "dropped_queue_full", "dropped_failed", "http_errors"
        }
    finally:
        exporter.shutdown()
