"""GDPR 级联删除的三个真实执行器。

存在的理由：`RetentionManager` 与 `PostgresDeleter` / `ClickHouseDeleter` /
`ObjectStoreDeleter` 三个 Protocol 建好后，只有测试里的 Fake 实现过它们。
生产端点 `DELETE /v1/projects/{id}/data` 因此只写一行 `status="pending"`
就返 202 —— 一个**假成功的合规端点**：调用方拿到 202 以为删除已受理，
实际上没有任何代码会去删数据。这是 R12 在合规面上的实例，比其他死代码
更严重，因为它对外做出了法律承诺。

三处存储的删除语义刻意不同，不要"统一"：
- Postgres：FK ON DELETE CASCADE，同步，删完即数
- ClickHouse：ALTER TABLE DELETE 是**异步 mutation**，提交后只能轮询
- 对象存储：前缀批量删，同步

subject 级删除（GDPR 的"被遗忘权"通常针对单个自然人）与 project 级删除
走同一套代码，区别只在 WHERE 条件 —— 见各 deleter 的 subject_id 分支。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.storage.clickhouse import ClickHouseStore
    from ariadne.storage.objectstore import ObjectStore
    from ariadne.storage.postgres.engine import PostgresStore

logger = get_logger(__name__)


def _rowcount(result: object) -> int:
    """取受影响行数。

    `session.execute()` 的静态类型是 `Result`，只有运行时的 `CursorResult`
    才有 rowcount。DML 语句拿到的必然是后者，但 mypy 看不出来 —— 用
    getattr 而不是 cast，因为断言错了会静默返回 0（少报删除行数），
    getattr 至少行为明确。
    """
    return int(getattr(result, "rowcount", 0) or 0)


# project 删除时按 FK 依赖倒序删（子表在前，否则 FK 约束会挡住父表删除）。
# projects 本行不删 —— 删掉会连带 api_keys 让租户彻底失联，而 GDPR 要的是
# "数据删除"不是"注销账号"。api_keys 同理保留：凭据不是用户数据。
#
# 这 11 张表都直接带 project_id 列。dataset_items 刻意不在表里：它只有
# dataset_id，靠 datasets 的 ON DELETE CASCADE 连带删除 —— 我最初把它写进
# 来，真 SQL 一跑就报 "no such column: project_id"（Fake deleter 永远发现
# 不了这个，见 tests/test_retention_worker.py 的模块注释）。
_PROJECT_TABLES: tuple[str, ...] = (
    "loop_checkpoints",
    "approvals",
    "loop_runs",
    "audit_log",
    "rule_sets",
    "workflow_graphs",
    "llm_model_configs",
    "judge_calibrations",
    "prompt_versions",
    "experiments",
    "datasets",
)

# ClickHouse 侧需要删的表。rollup 是物化视图的目标表，必须一并删 ——
# 只删 spans 会让聚合表里留着已删项目的成本与延迟数据。
CLICKHOUSE_TABLES: tuple[str, ...] = ("spans", "trace_rollup", "cost_rollup")

# 带 subject_id 的行级删除只在 spans 上做。rollup 是聚合结果，
# 没有 subject 维度，subject 级删除后需要靠 TTL 或重算收敛。
_SUBJECT_ATTR = "ariadne.subject.id"


class PostgresCascadeDeleter:
    """Postgres 侧删除。带租户上下文执行（RLS 要求）。

    生产连接是 app 角色（非 owner），所有表都有 RLS：不带租户变量的
    会话看不到任何行 —— 旧实现的 DELETE 全部静默删 0 行，任务却报
    completed（真机验收实证）。本删除器只删**单个项目**的数据，按该
    项目建租户会话即可，也不需要 owner 的绕过权。
    """

    def __init__(self, store: PostgresStore) -> None:
        self._store = store

    async def delete_project_data(self, project_id: UUID, subject_id: str = "") -> int:
        """删除项目（或 subject）在 Postgres 的数据，返回删除行数。

        subject_id 非空时只删该 subject 的 loop_runs（subject 声明存在
        goal JSON 里）—— 其余表没有 subject 维度，按 subject 删会误伤
        同项目其他人的数据，只能等 project 级删除或 TTL 收敛。
        """
        from sqlalchemy import delete, text

        from ariadne.auth.tenant import tenant_session
        from ariadne.storage.postgres.loop_models import LoopRun

        total = 0
        async with tenant_session(self._store, project_id) as session:
            if subject_id:
                # JSON 字段精确匹配，避免 LIKE 的 %/_ 通配符和子串误删。
                result = await session.execute(
                    delete(LoopRun).where(
                        LoopRun.project_id == project_id,
                        LoopRun.goal["subject_id"].as_string() == subject_id,
                    )
                )
                return _rowcount(result)

            for table in _PROJECT_TABLES:
                # 表名来自模块内常量白名单，不是外部输入
                result = await session.execute(
                    text(f"DELETE FROM {table} WHERE project_id = :pid"),
                    {"pid": str(project_id)},
                )
                deleted = _rowcount(result)
                total += deleted
                if deleted:
                    logger.debug(
                        "postgres 删除完成",
                        extra={"table": table, "rows": deleted},
                    )
            # deletion_jobs 自身不删：删了就没有审计痕迹证明删除发生过
        return total


class ClickHouseMutationDeleter:
    """ClickHouse 侧删除。ALTER TABLE DELETE 是异步 mutation。

    提交后立刻返回 mutation_id，真正的删除在后台按 part 重写完成。
    因此 `RetentionManager` 不能在提交后就报 completed —— 那正是
    M6 §4.3 要求记录任务状态的原因。
    """

    def __init__(self, store: ClickHouseStore, database: str = "") -> None:
        self._store = store
        self._database = database or store._settings.database

    def submit_delete_mutation(
        self, table: str, project_id: UUID, subject_id: str = ""
    ) -> str:
        """提交删除 mutation，返回 mutation_id。

        表名校验白名单：拼进 SQL 的标识符不能来自未校验输入。
        """
        if table not in CLICKHOUSE_TABLES:
            raise ValueError(f"不允许删除的表: {table!r}")

        where = "project_id = {pid:UUID}"
        params: dict[str, object] = {"pid": str(project_id)}
        if subject_id:
            if table != "spans":
                # rollup 无 subject 维度，提交这条 mutation 会删掉整个项目的聚合
                return ""
            where += f" AND attributes['{_SUBJECT_ATTR}'] = {{sid:String}}"
            params["sid"] = subject_id

        # table 已过 CLICKHOUSE_TABLES 白名单，subject 值走 {sid:String} 绑定
        self._store.client.command(
            f"ALTER TABLE {self._database}.{table} DELETE WHERE {where}",
            parameters=params,
        )
        rows = self._store.query(
            "SELECT mutation_id FROM system.mutations "
            "WHERE database = {db:String} AND table = {tbl:String} "
            "ORDER BY create_time DESC LIMIT 1",
            {"db": self._database, "tbl": table},
        )
        mutation_id = str(rows[0]["mutation_id"]) if rows else ""
        logger.info(
            "clickhouse 删除 mutation 已提交",
            extra={"table": table, "mutation_id": mutation_id},
        )
        return mutation_id

    def is_mutation_done(self, mutation_id: str) -> bool:
        """查询 mutation 是否完成。

        空 mutation_id 视为完成（跳过的 rollup 表走这条路），
        查不到记录也视为完成 —— system.mutations 有 TTL，
        老记录消失说明早就跑完了。
        """
        if not mutation_id:
            return True
        rows = self._store.query(
            "SELECT is_done FROM system.mutations WHERE mutation_id = {mid:String}",
            {"mid": mutation_id},
        )
        if not rows:
            return True
        return bool(int(rows[0]["is_done"]))


class ObjectStorePrefixDeleter:
    """对象存储侧删除。直接复用 ObjectStore.delete_prefix。"""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def delete_prefix(self, prefix: str) -> int:
        deleted = self._store.delete_prefix(prefix)
        logger.info("对象存储删除完成", extra={"prefix": prefix, "count": deleted})
        return deleted


__all__ = [
    "CLICKHOUSE_TABLES",
    "ClickHouseMutationDeleter",
    "ObjectStorePrefixDeleter",
    "PostgresCascadeDeleter",
]
