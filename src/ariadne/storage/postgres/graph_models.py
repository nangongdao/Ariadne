"""Graph 相关 Postgres 表：workflow_graphs, graph_runs。

项目级 DAG 图定义，JSONB 存储 WorkflowGraph 序列化结果，版本化。
与 harness_models.RuleSetRow 同构：id + project_id + name + version + JSONB + is_active。

graph_runs 表记录图执行的 Job 状态和结果。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from ariadne.storage.postgres.models import Base, TimestampMixin, UuidType

JsonType = JSON().with_variant(JSONB(), "postgresql")


class GraphRow(Base, TimestampMixin):
    """项目级工作流图 —— JSONB 存储，版本化。

    graph 字段存储 graph_to_spec() 的序列化结果（含 nodes/edges/version）。
    项目可以有多个图，每个图有唯一名称，只有一个 active 版本。
    """

    __tablename__ = "workflow_graphs"
    __table_args__ = (
        Index("ix_workflow_graphs_project", "project_id"),
        Index("ix_workflow_graphs_project_name", "project_id", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    graph: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    # 校验报告（最近一次 validate 的结果，便于前端展示）
    validation_errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class GraphRun(Base):
    """Graph 执行 Job 记录（阶段 2：支持租约机制，阶段 3：支持取消）。

    记录图执行的状态、输入、输出和节点状态，用于异步执行和结果查询。
    阶段 2 增加 worker_id 和 lease_expires_at 字段以支持多 Worker 并发。
    阶段 3 增加 cancelled_at 字段以支持取消 API，并用 checkpoint 保存
    节点级恢复所需的中间输出。
    """

    __tablename__ = "graph_runs"
    __table_args__ = (
        Index("ix_graph_runs_project_created", "project_id", text("created_at DESC")),
        Index(
            "ix_graph_runs_state",
            "state",
            postgresql_where=text("state IN ('PENDING', 'RUNNING')"),
        ),
        Index(
            "ix_graph_runs_lease",
            "lease_expires_at",
            postgresql_where=text("state IN ('PENDING', 'RUNNING')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    graph_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("workflow_graphs.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    outputs: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    node_states: Mapped[dict[str, str] | None] = mapped_column(JsonType, nullable=True)
    # 节点级恢复载荷：
    # {"completed_nodes": [...], "node_outputs": {node_id: {port: value}}}
    # node_states 保留为扁平状态索引，兼容现有状态 API 与旧数据。
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType, nullable=True
    )
    errors: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)

    # 阶段 2：租约机制字段
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    # 阶段 3：取消机制字段
    cancelled_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC)
    )


__all__ = ["GraphRow", "GraphRun"]
