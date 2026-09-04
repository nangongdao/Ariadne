"""头部 + 尾部采样。

M6 §4.4 约束：尾部采样粒度是 loop_id 而非 span —— 同一 Loop 的所有轮次
必须一起保留或一起丢弃，否则进化视图会出现断层。

缓冲窗口 30s：超时的 trace 按头部决策处理。

M6 §4.5：自监控绕过采样与脱敏，直写 `_internal` 项目。
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ariadne.telemetry.models import AriadneSpan

logger = logging.getLogger(__name__)

# 自监控项目（绕过采样与脱敏）
INTERNAL_PROJECT_ID = UUID("00000000-0000-0000-0000-000000000000")

# 尾部采样缓冲窗口（秒）
TAIL_BUFFER_TIMEOUT_SEC = 30.0


class Sampler(Protocol):
    """采样器接口：决定一条 span 是否保留。"""

    def should_sample(self, span: AriadneSpan) -> bool: ...


@dataclass(frozen=True)
class HeadSampler:
    """头部采样：基于 trace_id 的确定性概率采样。

    同一 trace_id 总是得到同样的决策（一致性），
    使上下游独立采样器能保持一致。
    """

    rate: float = 1.0  # 0.0 ~ 1.0

    def should_sample(self, span: AriadneSpan) -> bool:
        if span.project_id == INTERNAL_PROJECT_ID:
            return True
        if self.rate >= 1.0:
            return True
        if self.rate <= 0.0:
            return False
        # 用 trace_id 的前 8 字节做 hash，映射到 [0, 1) 与 rate 比较
        digest = hashlib.sha256(span.trace_id.encode()).digest()
        threshold = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        return threshold < self.rate


@dataclass
class LoopSpanBuffer:
    """按 loop_id 缓冲 span，等待 trace 完成后做整体决策。"""

    loop_id: str
    spans: list[AriadneSpan] = field(default_factory=list)
    first_seen: float = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        return time.monotonic() - self.first_seen

    @property
    def is_expired(self) -> bool:
        return self.age >= TAIL_BUFFER_TIMEOUT_SEC


@dataclass
class TailSampler:
    """尾部采样：按 loop_id 分组，全保留或全丢弃。

    决策规则（按优先级）：
    1. 无 loop_id 的 span 走头部采样（非 Loop 的普通 trace）
    2. Loop 完成判定由外部信号（collector 在 flush 时检查）
    3. 超时未完成的 Loop 按 HeadSampler 决策（降级处理）
    4. 错误/异常 trace 默认保留（更有诊断价值）

    collector 调用流程：
    - add(span): 将 span 加入对应 loop_id 的缓冲区
    - drain_ready(): 取出已决策的 span（完成或超时），返回保留的 +丢弃的
    - mark_loop_done(loop_id): 标记某个 loop 已完成，触发决策
    """

    head_sampler: HeadSampler
    # 错误 trace 保留策略：有 error_type 的 span 默认保留
    keep_errors: bool = True
    # 缓冲区：loop_id -> LoopSpanBuffer
    _buffers: dict[str, LoopSpanBuffer] = field(default_factory=dict)
    # 已决策但待取出的 span（保留的）
    _decided_keep: list[AriadneSpan] = field(default_factory=list)
    # 已决策待丢弃的 span 数量（统计用）
    _dropped_count: int = 0

    def add(self, span: AriadneSpan) -> None:
        """将 span 加入缓冲区。无 loop_id 的直接走头部采样。"""
        if span.project_id == INTERNAL_PROJECT_ID:
            self._decided_keep.append(span)
            return

        if not span.loop_id:
            # 非 Loop span：立即头部采样
            if self.head_sampler.should_sample(span):
                self._decided_keep.append(span)
            else:
                self._dropped_count += 1
            return

        # 有 loop_id：缓冲，等待 loop 完成或超时
        buf = self._buffers.get(span.loop_id)
        if buf is None:
            buf = LoopSpanBuffer(loop_id=span.loop_id)
            self._buffers[span.loop_id] = buf
        buf.spans.append(span)

    def mark_loop_done(self, loop_id: str) -> None:
        """标记某个 loop 已完成，触发该 loop 的整体决策。"""
        buf = self._buffers.pop(loop_id, None)
        if buf is None:
            return
        self._decide(buf)

    def drain_ready(self) -> tuple[list[AriadneSpan], int]:
        """取出已决策的 span。

        返回 (保留的 span 列表, 本次丢弃数量)。
        同时处理超时的 loop 缓冲区。
        """
        # 处理超时的 loop
        expired = [lid for lid, buf in self._buffers.items() if buf.is_expired]
        for lid in expired:
            buf = self._buffers.pop(lid)
            logger.debug(
                "tail sampling timeout, falling back to head decision",
                extra={"loop_id": lid, "span_count": len(buf.spans)},
            )
            self._decide(buf)

        kept = self._decided_keep
        dropped = self._dropped_count
        self._decided_keep = []
        self._dropped_count = 0
        return kept, dropped

    def _decide(self, buf: LoopSpanBuffer) -> None:
        """对一个 loop 的全部 span 做整体决策。"""
        if not buf.spans:
            return

        # 错误优先：有任何 error_type 非空的 span 就保留整个 loop
        if self.keep_errors and any(s.error_type for s in buf.spans):
            self._decided_keep.extend(buf.spans)
            return

        # 用第一条 span 的 trace_id 做确定性决策（整个 loop 一致）
        if self.head_sampler.should_sample(buf.spans[0]):
            self._decided_keep.extend(buf.spans)
        else:
            self._dropped_count += len(buf.spans)

    @property
    def buffered_loop_count(self) -> int:
        return len(self._buffers)

    @property
    def buffered_span_count(self) -> int:
        return sum(len(b.spans) for b in self._buffers.values())


__all__ = [
    "INTERNAL_PROJECT_ID",
    "TAIL_BUFFER_TIMEOUT_SEC",
    "HeadSampler",
    "LoopSpanBuffer",
    "Sampler",
    "TailSampler",
]
