"""后台批量上报。

三条硬性约束（观测组件绝不能拖垮业务）：
1. 同步路径只做入队，目标 < 1ms
2. 队列满时丢最旧的并计数，**不反压业务线程**
3. 任何异常都在内部吞掉，永不向业务抛出
"""

from __future__ import annotations

import atexit
import contextlib
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

_DEFAULT_QUEUE_SIZE: Final = 10_000
_DEFAULT_BATCH_SIZE: Final = 100
_DEFAULT_FLUSH_INTERVAL: Final = 2.0
_MAX_RETRIES: Final = 3
_SHUTDOWN_TIMEOUT: Final = 5.0
# 客户端错误重试没有意义（schema 错、key 错），只重试 5xx 与网络故障
_RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})


@dataclass
class ExporterStats:
    submitted: int = 0
    sent: int = 0
    dropped_queue_full: int = 0
    dropped_failed: int = 0
    http_errors: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "submitted": self.submitted,
            "sent": self.sent,
            "dropped_queue_full": self.dropped_queue_full,
            "dropped_failed": self.dropped_failed,
            "http_errors": self.http_errors,
        }


@dataclass
class SpanExporter:
    """有界队列 + 单后台线程 + 指数退避重试。"""

    endpoint: str
    api_key: str
    batch_size: int = _DEFAULT_BATCH_SIZE
    flush_interval: float = _DEFAULT_FLUSH_INTERVAL
    queue_size: int = _DEFAULT_QUEUE_SIZE
    timeout: float = 10.0

    stats: ExporterStats = field(default_factory=ExporterStats)
    _queue: queue.Queue[dict[str, Any]] = field(init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    # 已出队但尚未发送成功的条数。flush() 必须同时等它归零，
    # 否则进程退出时会丢掉仍在 worker 本地 buffer 里的最后一批。
    _inflight: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = queue.Queue(maxsize=self.queue_size)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            # daemon 线程：即使业务忘了 close 也不会挂住进程退出
            self._thread = threading.Thread(
                target=self._run, name="ariadne-exporter", daemon=True
            )
            self._thread.start()
            atexit.register(self.shutdown)

    def submit(self, payload: dict[str, Any]) -> None:
        """入队。这是唯一在业务线程上执行的代码，必须极快且不抛异常。"""
        self.stats.submitted += 1
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            # 丢最旧的换新的：新数据通常比旧数据更有价值
            with contextlib.suppress(queue.Empty, queue.Full):
                self._queue.get_nowait()
                self._queue.put_nowait(payload)
            self.stats.dropped_queue_full += 1

    def flush(self, timeout: float = _SHUTDOWN_TIMEOUT) -> bool:
        """阻塞等待队列排空。返回是否在超时内排完。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._inflight == 0:
                return True
            time.sleep(0.02)
        return self._queue.empty() and self._inflight == 0

    def shutdown(self) -> None:
        if self._thread is None:
            return
        self.flush()
        self._stop.set()
        self._thread.join(timeout=_SHUTDOWN_TIMEOUT)
        self._thread = None
        if self._client is not None:
            self._client.close()
            self._client = None

    def _run(self) -> None:
        self._client = httpx.Client(timeout=self.timeout)
        buffer: list[dict[str, Any]] = []
        last_flush = time.monotonic()

        while not self._stop.is_set() or not self._queue.empty():
            try:
                timeout = max(0.05, self.flush_interval - (time.monotonic() - last_flush))
                with contextlib.suppress(queue.Empty):
                    buffer.append(self._queue.get(timeout=timeout))
                    self._inflight += 1

                elapsed = time.monotonic() - last_flush
                if buffer and (len(buffer) >= self.batch_size or elapsed >= self.flush_interval):
                    self._send(buffer)
                    buffer = []
                    last_flush = time.monotonic()
            except Exception:
                buffer = []
                last_flush = time.monotonic()

        if buffer:
            self._send(buffer)

    def _send(self, spans: list[dict[str, Any]]) -> None:
        try:
            self._send_with_retry(spans)
        finally:
            self._inflight = max(self._inflight - len(spans), 0)

    def _send_with_retry(self, spans: list[dict[str, Any]]) -> None:
        if self._client is None:
            self.stats.dropped_failed += len(spans)
            return

        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.post(
                    self.endpoint,
                    json={"spans": spans},
                    headers={"X-Ariadne-Key": self.api_key},
                )
                if response.status_code < 300:
                    self.stats.sent += len(spans)
                    return
                self.stats.http_errors += 1
                if response.status_code not in _RETRYABLE_STATUS:
                    self.stats.dropped_failed += len(spans)
                    return
            except httpx.HTTPError:
                self.stats.http_errors += 1

            # 用 wait 而非 sleep：收到停止信号立即放弃剩余重试，
            # 否则最坏 3×timeout 会远超 _SHUTDOWN_TIMEOUT，拖死进程退出
            if attempt < _MAX_RETRIES - 1 and self._stop.wait(0.5 * (2**attempt)):
                break

        self.stats.dropped_failed += len(spans)
