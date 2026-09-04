"""副作用幂等：防止 Worker 接管后重复执行（docs/03 第 242 行、M3-spec §6.3）。

要防的场景：Worker A 跑完第 N 轮的 COMMAND 断言（命令可能是 `git push`、
可能写文件），但在 JUDGING 落检查点之前死了。Worker B 从第 N-1 轮的检查点
恢复，`_planning` 把轮次号加回 N —— 于是同一条命令再跑一遍。

轮次号是确定性重复的（`_restore` 设 iteration=checkpoint.iteration，
`_planning` 再 +1），所以 `{loop_id}:{iteration}:{step}` 能正确撞上。

## 为什么不能只用 SET NX

规格写的是"key 已存在则跳过执行并复用上次结果"。裸 SET NX 只回答
"有人做过吗"，拿不回上次结果。而跳过执行后引擎**必须**报一个结果：
报 passed=False 会让 Loop 以为断言失败去改代码，报 passed=True 是伪造
通过。所以存结果不是镀金，是正确性必需。

## 三个状态

一个 key 有三种含义，混淆任意两个都会出错：

| 状态 | Redis 值 | 含义 | 引擎动作 |
|------|---------|------|---------|
| 不存在 | — | 没人做过 | 抢租约后执行 |
| 租约 | `{"v":1}` | 有人在做，还没做完 | fail-closed 记 errored |
| 结果 | `{"v":1,"outcome":{…}}` | 做完了 | 复用，不重跑 |

`recall()` 只在第三种状态返回载荷 —— 第二种返回 None 是刻意的，
让调用方走 `try_acquire` 分支去发现"租约被别人持有"。

## Redis 不可用时 fail-open

`try_acquire` 在 Redis 故障时返回 True（放行执行），不是 False。
理由：被守卫的是"接管时重复执行"，这是罕见路径；而 fail-closed 会让
一次 Redis 抖动杀掉所有正在跑的 Loop。代价是故障期间没有去重 —— 这是
刻意的可用性取舍，不是遗漏。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Final

from ariadne.loop_module.goal import AssertionKind
from ariadne.loop_module.verifier.base import AssertionOutcome
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.config import RedisSettings

logger = get_logger(__name__)

# 24h（M3-spec §6.3）。远长于 Loop 墙钟上限，又能自动清理崩溃残留。
IDEMPOTENCY_TTL_SECONDS: Final = 86_400

# 信封版本。将来改载荷结构时用它区分，避免把旧格式解成新格式。
_ENVELOPE_VERSION: Final = 1
_LEASE_ENVELOPE: Final = '{"v":1}'


def build_key(loop_id: str, iteration: int, step: str) -> str:
    """幂等键。格式见 docs/03:242 与 M3-spec §6.3。

    step 用断言 id 而非序号：序号会因 spec 中断言顺序调整而错位，
    断言 id 在一个 Loop 生命周期内稳定。
    """
    return f"{loop_id}:{iteration}:{step}"


def encode_outcome(outcome: AssertionOutcome) -> str:
    """把 AssertionOutcome 序列化进信封。

    手写字段而非 asdict：AssertionOutcome 将来加字段时，解码端的
    默认值决定兼容行为，显式列出让这个决定可见。
    """
    return json.dumps(
        {
            "v": _ENVELOPE_VERSION,
            "outcome": {
                "assertion_id": outcome.assertion_id,
                "kind": outcome.kind.value,
                "passed": outcome.passed,
                "value": outcome.value,
                "evidence": outcome.evidence,
                "pending_human": outcome.pending_human,
                "errored": outcome.errored,
                "duration_ms": outcome.duration_ms,
            },
        },
        ensure_ascii=False,
    )


def decode_outcome(payload: str) -> AssertionOutcome | None:
    """从信封还原 AssertionOutcome。无法还原时返回 None（调用方重跑）。

    宽容解析是刻意的：载荷损坏、版本不认、枚举值失效都返回 None，
    让调用方退回"执行一次"。抛异常会让一个坏 key 卡死整个 Loop。
    """
    try:
        envelope = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        logger.warning("幂等载荷不是合法 JSON，将重新执行")
        return None

    if not isinstance(envelope, dict) or envelope.get("v") != _ENVELOPE_VERSION:
        logger.warning("幂等载荷版本不认，将重新执行", extra={"payload_head": payload[:80]})
        return None

    raw = envelope.get("outcome")
    if not isinstance(raw, dict):
        # 租约信封（无 outcome 字段）走到这里是正常的，不告警
        return None

    try:
        kind = AssertionKind(raw["kind"])
    except (KeyError, ValueError):
        logger.warning("幂等载荷的断言类型失效，将重新执行")
        return None

    return AssertionOutcome(
        assertion_id=str(raw.get("assertion_id", "")),
        kind=kind,
        passed=bool(raw.get("passed", False)),
        value=float(raw.get("value", 0.0)),
        evidence=str(raw.get("evidence", "")),
        pending_human=bool(raw.get("pending_human", False)),
        errored=bool(raw.get("errored", False)),
        duration_ms=int(raw.get("duration_ms", 0)),
    )


class RedisIdempotencyStore:
    """Redis SET NX 幂等存储。

    异步客户端：调用点在引擎的 `_evaluating`（协程），且与 RedisEventSink
    共用同一套连接惯例。与 RedisCounter 刻意用同步客户端不同 —— 那个在
    预算判定的纯同步链上。
    """

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings
        self._redis: Any = None

    async def _client(self) -> Any:
        if self._redis is None:
            from redis import asyncio as aioredis

            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                self._settings.url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool:
        """抢租约。已存在（租约或结果）返回 False。

        Redis 故障时返回 True —— fail-open，理由见模块 docstring。
        """
        try:
            client = await self._client()
            acquired = await client.set(
                f"idem:{key}", _LEASE_ENVELOPE, nx=True, ex=ttl_seconds
            )
        except Exception as exc:
            logger.warning(
                "幂等租约获取失败，本次不去重（fail-open）",
                extra={"key": key, "error": str(exc)},
            )
            return True
        return bool(acquired)

    async def recall(self, key: str) -> str | None:
        """取已完成的结果载荷。只有租约（无结果）时返回 None。"""
        try:
            client = await self._client()
            raw = await client.get(f"idem:{key}")
        except Exception as exc:
            logger.warning(
                "幂等结果读取失败，本次不复用",
                extra={"key": key, "error": str(exc)},
            )
            return None
        if not raw or raw == _LEASE_ENVELOPE:
            return None
        return str(raw)

    async def remember(self, key: str, payload: str, ttl_seconds: int) -> None:
        """把结果写回（覆盖租约信封）。不用 NX —— 这里就是要覆盖。"""
        try:
            client = await self._client()
            await client.set(f"idem:{key}", payload, ex=ttl_seconds)
        except Exception as exc:
            # 写不回只意味着下次接管会重跑，不影响本次结果
            logger.warning(
                "幂等结果写入失败，接管后可能重复执行",
                extra={"key": key, "error": str(exc)},
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "RedisIdempotencyStore",
    "build_key",
    "decode_outcome",
    "encode_outcome",
]
