"""配额主动调度测试（2026-09-03）。

验证 quota_based_concurrency 预测性降并发逻辑：
- 剩余请求数低于阈值 → 降到 1（保守派发）
- 剩余 token 数低于阈值 → 降到 1
- 配额充足或 provider 不返回配额头 → 保持 max_concurrency
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from ariadne.loop_module.rate_limit import (
    QUOTA_LOW_REQUESTS_THRESHOLD,
    QUOTA_LOW_TOKENS_THRESHOLD,
    quota_based_concurrency,
)


@dataclass(frozen=True)
class MockQuota:
    """模拟 RateLimitQuota 结构（避免循环导入）。"""

    requests_remaining: int | None = None
    tokens_remaining: int | None = None
    requests_reset: str | None = None
    tokens_reset: str | None = None


class TestQuotaBasedConcurrency:
    def test_no_quota_returns_max(self) -> None:
        """provider 不返回配额头时保持原并发度（降级到被动 429 防线）。"""
        assert quota_based_concurrency(None, max_concurrency=10) == 10

    def test_sufficient_quota_returns_max(self) -> None:
        """配额充足时保持原并发度（无需限制）。"""
        quota = MockQuota(requests_remaining=100, tokens_remaining=50_000)
        assert quota_based_concurrency(quota, max_concurrency=10) == 10

    def test_low_requests_remaining_reduces_to_one(self) -> None:
        """剩余请求数低于阈值时降到保守派发（并发 1）。"""
        quota = MockQuota(
            requests_remaining=QUOTA_LOW_REQUESTS_THRESHOLD - 1,
            tokens_remaining=100_000,
        )
        assert quota_based_concurrency(quota, max_concurrency=10) == 1

    def test_low_tokens_remaining_reduces_to_one(self) -> None:
        """剩余 token 数低于阈值时降到保守派发（并发 1）。"""
        quota = MockQuota(
            requests_remaining=100, tokens_remaining=QUOTA_LOW_TOKENS_THRESHOLD - 1
        )
        assert quota_based_concurrency(quota, max_concurrency=10) == 1

    def test_both_low_reduces_to_one(self) -> None:
        """请求数和 token 数同时低时降到保守派发（并发 1）。"""
        quota = MockQuota(
            requests_remaining=QUOTA_LOW_REQUESTS_THRESHOLD - 1,
            tokens_remaining=QUOTA_LOW_TOKENS_THRESHOLD - 1,
        )
        assert quota_based_concurrency(quota, max_concurrency=10) == 1

    def test_boundary_requests_at_threshold(self) -> None:
        """剩余请求数恰好等于阈值时不触发降并发（>= 判断）。"""
        quota = MockQuota(
            requests_remaining=QUOTA_LOW_REQUESTS_THRESHOLD,
            tokens_remaining=100_000,
        )
        assert quota_based_concurrency(quota, max_concurrency=10) == 10

    def test_boundary_tokens_at_threshold(self) -> None:
        """剩余 token 数恰好等于阈值时不触发降并发（>= 判断）。"""
        quota = MockQuota(
            requests_remaining=100,
            tokens_remaining=QUOTA_LOW_TOKENS_THRESHOLD,
        )
        assert quota_based_concurrency(quota, max_concurrency=10) == 10

    def test_partial_quota_fields(self) -> None:
        """部分配额字段存在时仅根据存在字段判断。"""
        # 只有 requests_remaining，且低于阈值
        quota_req_only = MockQuota(requests_remaining=2, tokens_remaining=None)
        assert quota_based_concurrency(quota_req_only, max_concurrency=10) == 1

        # 只有 tokens_remaining，且低于阈值
        quota_tok_only = MockQuota(requests_remaining=None, tokens_remaining=5_000)
        assert quota_based_concurrency(quota_tok_only, max_concurrency=10) == 1

        # 只有 requests_remaining，且充足
        quota_req_sufficient = MockQuota(requests_remaining=100, tokens_remaining=None)
        assert quota_based_concurrency(quota_req_sufficient, max_concurrency=10) == 10

    def test_zero_remaining(self) -> None:
        """剩余配额为 0 时降到保守派发（边界情况：下次请求必然 429）。"""
        quota = MockQuota(requests_remaining=0, tokens_remaining=0)
        assert quota_based_concurrency(quota, max_concurrency=10) == 1

    def test_max_concurrency_one(self) -> None:
        """max_concurrency 本就是 1 时，即使配额低也返回 1（不会降到 0）。"""
        quota = MockQuota(requests_remaining=1, tokens_remaining=1_000)
        assert quota_based_concurrency(quota, max_concurrency=1) == 1

    def test_dynamic_object_with_getattr(self) -> None:
        """函数用 getattr 读取字段，支持鸭子类型（不仅限 MockQuota）。"""

        class DynamicQuota:
            def __init__(self, req: int | None, tok: int | None) -> None:
                self.requests_remaining = req
                self.tokens_remaining = tok

        quota = DynamicQuota(req=2, tok=50_000)
        assert quota_based_concurrency(quota, max_concurrency=10) == 1


@pytest.mark.parametrize(
    "requests_remaining,tokens_remaining,expected",
    [
        (100, 100_000, 10),  # 充足
        (4, 100_000, 1),  # 请求数低
        (100, 9_000, 1),  # token 数低
        (2, 5_000, 1),  # 双低
        (5, 10_000, 10),  # 临界（阈值恰好）
        (None, 100_000, 10),  # 只有 token 字段，充足
        (100, None, 10),  # 只有请求字段，充足
        (None, None, 10),  # 无字段
    ],
)
def test_quota_matrix(
    requests_remaining: int | None,
    tokens_remaining: int | None,
    expected: int,
) -> None:
    """参数化测试矩阵：覆盖配额组合。"""
    quota = MockQuota(
        requests_remaining=requests_remaining, tokens_remaining=tokens_remaining
    )
    assert quota_based_concurrency(quota, max_concurrency=10) == expected
