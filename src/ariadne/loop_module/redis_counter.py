"""Redis 预算计数器。

生产环境用它而非 InMemoryCounter：并行 Loop 共享预算池时，
进程内计数必然超支（各 Worker 各自计数，总和无人管）。

同步接口而非 async：预算检查在 LLM 调用的关键路径上，且是单次
原子操作（INCRBY），同步 Redis 客户端的开销可忽略，但能让 BudgetGuard
保持纯同步 —— 否则整条判定链都要染上 async，测试也更麻烦。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from ariadne.loop_module.budget import BudgetUsage
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    import redis

logger = get_logger(__name__)

# 24 小时：足够覆盖最长的 Loop（墙钟上限远小于此），
# 又能自动清理崩溃 Loop 的残留 key
KEY_TTL_SECONDS: Final = 86_400

def _sync(value: Any) -> Any:
    """redis-py 的类型存根对同步/异步客户端共用一套签名，返回
    `Awaitable[T] | T`。本模块刻意用同步客户端（见模块 docstring），
    因此在边界处统一收窄，避免每个调用点都写 cast。
    """
    return cast("Any", value)


_TOKENS = "tokens"
_COST = "cost"
_ITERATIONS = "iterations"


class RedisCounter:
    """基于 Redis Hash 的原子计数器。

    用 Hash 而非三个独立 key：一个 Loop 的三项用量放一起，
    读取只需一次 HGETALL，且 TTL 只用管一个 key。
    """

    def __init__(self, client: redis.Redis, *, key_prefix: str = "budget") -> None:
        self._client = client
        self._prefix = key_prefix

    def _key(self, loop_id: str) -> str:
        return f"{self._prefix}:{loop_id}"

    def _touch_ttl(self, key: str) -> None:
        # 每次写入都续 TTL：长 Loop 不会因为超过 TTL 而丢计数
        self._client.expire(key, KEY_TTL_SECONDS)

    def incr_tokens(self, loop_id: str, delta: int) -> int:
        key = self._key(loop_id)
        value = int(_sync(self._client.hincrby(key, _TOKENS, delta)))
        self._touch_ttl(key)
        return value

    def incr_cost(self, loop_id: str, delta_micro: int) -> int:
        """成本以微美分整数递增。

        刻意不用 HINCRBYFLOAT：浮点累加有精度漂移，
        预算判定必须精确（见 budget.to_micro_usd 的取整策略）。
        """
        key = self._key(loop_id)
        value = int(_sync(self._client.hincrby(key, _COST, delta_micro)))
        self._touch_ttl(key)
        return value

    def incr_iterations(self, loop_id: str, delta: int = 1) -> int:
        key = self._key(loop_id)
        value = int(_sync(self._client.hincrby(key, _ITERATIONS, delta)))
        self._touch_ttl(key)
        return value

    def get(self, loop_id: str) -> BudgetUsage:
        raw: dict[Any, Any] = _sync(self._client.hgetall(self._key(loop_id)))
        if not raw:
            return BudgetUsage()

        def field(name: str) -> int:
            # decode_responses 可能为 True 或 False，两种键形式都要认
            value = raw.get(name) or raw.get(name.encode())
            return int(value) if value is not None else 0

        return BudgetUsage(
            total_tokens=field(_TOKENS),
            cost_micro_usd=field(_COST),
            iterations=field(_ITERATIONS),
        )

    def set_usage(self, loop_id: str, usage: BudgetUsage) -> None:
        """从检查点恢复。

        用 HSET 覆盖而非递增：恢复语义是"把计数设为快照值"，
        递增会在重复恢复时翻倍（Worker 反复接管的场景真实存在）。
        """
        key = self._key(loop_id)
        self._client.hset(
            key,
            mapping={
                _TOKENS: usage.total_tokens,
                _COST: usage.cost_micro_usd,
                _ITERATIONS: usage.iterations,
            },
        )
        self._touch_ttl(key)
        logger.info(
            "budget restored from checkpoint",
            extra={
                "loop_id": loop_id,
                "tokens": usage.total_tokens,
                "cost_usd": str(usage.cost_usd),
            },
        )

    def reset(self, loop_id: str) -> None:
        self._client.delete(self._key(loop_id))


class ProjectPoolCounter:
    """项目级共享预算池。

    并行 Loop 场景（M5）需要它：单个 Loop 的预算之外，还要有
    项目级总量上限，防止"每个 Loop 都在预算内但一起跑爆账单"。
    """

    def __init__(self, client: redis.Redis, *, key_prefix: str = "budget:pool") -> None:
        self._client = client
        self._prefix = key_prefix

    def _key(self, project_id: str) -> str:
        return f"{self._prefix}:{project_id}"

    def try_consume_cost(self, project_id: str, delta_micro: int, limit_micro: int) -> bool:
        """原子预扣项目池。超限时回滚并返回 False。

        这个"扣了再查、超了回滚"的模式是并发安全的关键 ——
        "先查再扣"在并发下会让多个调用都通过检查。
        """
        key = self._key(project_id)
        new_total = int(_sync(self._client.incrby(key, delta_micro)))
        self._client.expire(key, KEY_TTL_SECONDS)
        if new_total > limit_micro:
            self._client.incrby(key, -delta_micro)
            return False
        return True

    def current(self, project_id: str) -> int:
        raw = _sync(self._client.get(self._key(project_id)))
        return int(raw) if raw else 0

    def reset(self, project_id: str) -> None:
        self._client.delete(self._key(project_id))
