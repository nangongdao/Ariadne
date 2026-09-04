"""ClickHouse 访问层：批量写入与查询。"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import clickhouse_connect

from ariadne.config import ClickHouseSettings
from ariadne.telemetry.models import AriadneSpan
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from clickhouse_connect.driver.client import Client

logger = get_logger(__name__)


def iter_ddl_statements(sql: str) -> Iterator[str]:
    """按分号切分 DDL，逐条剥掉行注释后产出非空语句。

    注释必须逐行剥，不能判「整段是否以 -- 开头」：按分号切分后每条语句都
    连着它上方的注释行，那种判法会把带注释的语句整条丢掉 —— 建库、建表、
    TTL 共 10 条曾因此静默跳过，只留下依赖它们的物化视图。
    """
    for chunk in sql.split(";"):
        body = "\n".join(
            line for line in chunk.splitlines() if not line.strip().startswith("--")
        ).strip()
        if body:
            yield body


# 列顺序必须与 insert 时的行元组顺序严格一致
SPAN_COLUMNS: Final[tuple[str, ...]] = (
    "project_id", "trace_id", "span_id", "parent_span_id",
    "name", "kind", "operation", "provider", "model_request", "model_response",
    "started_at", "duration_ms", "status", "error_type",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
    "reasoning_tokens", "cost_usd",
    "loop_id", "iteration", "failure_fp",
    "input_preview", "output_preview", "input_ref", "output_ref",
    "attributes", "tags",
)


def span_to_row(span: AriadneSpan) -> tuple[Any, ...]:
    return (
        span.project_id, span.trace_id, span.span_id, span.parent_span_id,
        span.name, span.kind.value, span.operation, span.provider,
        span.model_request, span.model_response,
        span.started_at, span.duration_ms, span.status.value, span.error_type,
        span.usage.input_tokens, span.usage.output_tokens,
        span.usage.cache_read_tokens, span.usage.cache_write_tokens,
        span.usage.reasoning_tokens, span.cost_usd,
        span.loop_id, span.iteration, span.failure_fp,
        span.input_preview, span.output_preview, span.input_ref, span.output_ref,
        span.attributes, span.tags,
    )


class ClickHouseStore:
    """连接管理 + 批量写 + 查询。

    clickhouse_connect 的 Client 不是线程安全的，用线程局部实例；
    Worker 是单线程消费循环，API 侧是短查询，都不需要连接池。
    """

    def __init__(self, settings: ClickHouseSettings) -> None:
        self._settings = settings
        self._local = threading.local()

    @property
    def client(self) -> Client:
        client = getattr(self._local, "client", None)
        if client is None:
            client = clickhouse_connect.get_client(
                host=self._settings.host,
                port=self._settings.port,
                username=self._settings.user,
                password=self._settings.password.get_secret_value(),
                database=self._settings.database,
                # 服务端不做 async insert：批量由 Worker 侧控制，语义更可控
                settings={"insert_deduplicate": 0},
            )
            self._local.client = client
        return client

    def ping(self) -> bool:
        try:
            self.client.command("SELECT 1")
            return True
        except Exception as exc:
            logger.warning("clickhouse ping failed", extra={"error": str(exc)})
            return False

    def migrate(self, ddl_dir: Path) -> list[str]:
        """按文件名顺序执行 DDL。语句以分号分隔，全部幂等。"""
        applied: list[str] = []
        # 建库语句需要用无 database 的连接执行
        bootstrap = clickhouse_connect.get_client(
            host=self._settings.host,
            port=self._settings.port,
            username=self._settings.user,
            password=self._settings.password.get_secret_value(),
        )
        try:
            for path in sorted(ddl_dir.glob("*.sql")):
                for stmt in iter_ddl_statements(path.read_text(encoding="utf-8")):
                    bootstrap.command(stmt)
                applied.append(path.name)
                logger.info("ddl applied", extra={"file": path.name})
        finally:
            bootstrap.close()
        return applied

    def insert_spans(self, spans: Sequence[AriadneSpan]) -> int:
        if not spans:
            return 0
        rows = [span_to_row(s) for s in spans]
        self.client.insert(
            "spans", rows, column_names=list(SPAN_COLUMNS), database=self._settings.database
        )
        return len(rows)

    def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        *,
        project_id: Any = None,
    ) -> list[dict[str, Any]]:
        """查询 ClickHouse。

        project_id 非 None 时，先 SET ariadne.project_id（row policy 依赖）。
        事务/会话级：clickhouse_connect 每次请求复用线程局部 client，
        SET 在后续 query 同一 client 上生效。重置在 query 后执行。

        注入防护：project_id 在拼进 SET 前强制校验 UUID 格式 —— 字符串拼接
        是历史遗留，一旦有调用方传入任意字符串即构成 ClickHouse 注入。
        """
        if project_id is not None:
            # 校验而非直接 str()：SET 语句是字符串拼接，非 UUID 输入
            # （SQL 片段、引号、注释符）会直接注入进 ClickHouse 会话设置
            try:
                UUID(str(project_id))
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(
                    f"project_id 必须是 UUID，收到 {project_id!r}"
                ) from exc
            self.client.command(
                f"SET ariadne.project_id = '{UUID(str(project_id))}'"
            )
        try:
            result = self.client.query(sql, parameters=params or {})
            columns = result.column_names
            return [dict(zip(columns, row, strict=True)) for row in result.result_rows]
        finally:
            if project_id is not None:
                with contextlib.suppress(Exception):
                    self.client.command("RESET SETTING ariadne.project_id")

    def count_spans(self, project_id: Any, trace_id: str | None = None) -> int:
        sql = "SELECT count() AS c FROM spans WHERE project_id = {pid:UUID}"
        params: dict[str, Any] = {"pid": project_id}
        if trace_id:
            sql += " AND trace_id = {tid:String}"
            params["tid"] = trace_id
        rows = self.query(sql, params, project_id=project_id)
        return int(rows[0]["c"]) if rows else 0

    def close(self) -> None:
        client = getattr(self._local, "client", None)
        if client is not None:
            client.close()
            self._local.client = None


def utc_naive(value: datetime) -> datetime:
    """ClickHouse DateTime64 不带时区信息，写入前转为 naive UTC。"""
    if value.tzinfo is None:
        return value
    from datetime import UTC

    return value.astimezone(UTC).replace(tzinfo=None)
