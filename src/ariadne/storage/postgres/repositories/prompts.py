"""Prompt 版本仓储。

范围刻意最小（见 docs/01 非目标）：只做到"可复现实验"所需。
不做模板市场、不做变量类型系统、不做可视化编辑。

label 语义：同一 label 在项目内唯一指向一个版本（如 production 只能
指一个）。切换 label 是显式操作，且要留痕 —— 生产 prompt 被改是
最常见的"昨天还好今天就坏了"的原因。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.models import PromptVersion


class PromptNotFoundError(LookupError):
    pass


def compute_prompt_hash(template: str, variables: dict[str, Any]) -> str:
    """模板 + 变量声明的哈希。

    变量也参与：同样的模板文本配不同的变量声明是不同的 prompt
    （渲染结果会不同）。
    """
    import json

    payload = json.dumps(
        {"template": template, "variables": variables},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class PromptSnapshot:
    """取出的 prompt 版本。"""

    id: uuid.UUID
    name: str
    version: int
    template: str
    variables: dict[str, Any]
    labels: tuple[str, ...]
    content_hash: str

    @property
    def ref(self) -> str:
        """实验记录里引用 prompt 的规范形式。"""
        return f"{self.name}@v{self.version}#{self.content_hash}"

    def render(self, values: dict[str, str]) -> str:
        """渲染模板。

        缺变量时显式报错而非留下 {placeholder} —— 后者会让模型收到
        字面的花括号文本，产生难以定位的质量问题。
        """
        missing = [
            name
            for name in self.variables
            if name not in values
        ]
        if missing:
            raise KeyError(
                f"prompt {self.ref} 渲染缺少变量: {', '.join(sorted(missing))}"
            )
        result = self.template
        for key, value in values.items():
            result = result.replace(f"{{{{{key}}}}}", value)
        return result


class PromptRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_version(self, *, project_id: uuid.UUID, name: str) -> int:
        current = (
            await self._session.execute(
                select(func.max(PromptVersion.version)).where(
                    PromptVersion.project_id == project_id,
                    PromptVersion.name == name,
                )
            )
        ).scalar_one_or_none()
        return 1 if current is None else int(current) + 1

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        template: str,
        variables: dict[str, Any] | None = None,
        labels: tuple[str, ...] = (),
    ) -> PromptSnapshot:
        resolved_vars = variables or {}
        version = await self.next_version(project_id=project_id, name=name)
        row = PromptVersion(
            id=uuid.uuid4(),
            project_id=project_id,
            name=name,
            version=version,
            template=template,
            variables=resolved_vars,
            labels={"labels": list(labels)},
            content_hash=compute_prompt_hash(template, resolved_vars),
        )
        self._session.add(row)
        await self._session.flush()

        if labels:
            # 新版本带 label 时要把其他版本的同名 label 摘掉
            for label in labels:
                await self._detach_label(
                    project_id=project_id, name=name, label=label, keep=row.id
                )
        return self._to_snapshot(row)

    async def get(
        self, *, project_id: uuid.UUID, name: str, version: int | None = None
    ) -> PromptSnapshot:
        stmt = select(PromptVersion).where(
            PromptVersion.project_id == project_id, PromptVersion.name == name
        )
        stmt = (
            stmt.where(PromptVersion.version == version)
            if version is not None
            else stmt.order_by(PromptVersion.version.desc()).limit(1)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            suffix = f"@v{version}" if version is not None else ""
            raise PromptNotFoundError(f"prompt 不存在: {name}{suffix}")
        return self._to_snapshot(row)

    async def get_by_label(
        self, *, project_id: uuid.UUID, name: str, label: str
    ) -> PromptSnapshot:
        """按 label 取版本。生产代码应用这个而非硬编码版本号。"""
        rows = (
            await self._session.execute(
                select(PromptVersion).where(
                    PromptVersion.project_id == project_id,
                    PromptVersion.name == name,
                )
            )
        ).scalars()
        for row in rows:
            if label in self._labels_of(row):
                return self._to_snapshot(row)
        raise PromptNotFoundError(f"prompt {name!r} 没有标签 {label!r} 的版本")

    async def set_label(
        self, *, project_id: uuid.UUID, name: str, version: int, label: str
    ) -> PromptSnapshot:
        """把 label 指向指定版本，并从其他版本摘掉。

        同一 label 在项目内唯一 —— production 指向两个版本是无意义状态。
        """
        row = (
            await self._session.execute(
                select(PromptVersion).where(
                    PromptVersion.project_id == project_id,
                    PromptVersion.name == name,
                    PromptVersion.version == version,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise PromptNotFoundError(f"prompt 不存在: {name}@v{version}")

        await self._detach_label(
            project_id=project_id, name=name, label=label, keep=row.id
        )
        labels = set(self._labels_of(row))
        labels.add(label)
        row.labels = {"labels": sorted(labels)}
        await self._session.flush()
        return self._to_snapshot(row)

    async def list_versions(
        self, *, project_id: uuid.UUID, name: str
    ) -> list[PromptSnapshot]:
        rows = (
            await self._session.execute(
                select(PromptVersion)
                .where(
                    PromptVersion.project_id == project_id,
                    PromptVersion.name == name,
                )
                .order_by(PromptVersion.version.desc())
            )
        ).scalars()
        return [self._to_snapshot(row) for row in rows]

    async def _detach_label(
        self, *, project_id: uuid.UUID, name: str, label: str, keep: uuid.UUID
    ) -> None:
        rows = (
            await self._session.execute(
                select(PromptVersion).where(
                    PromptVersion.project_id == project_id,
                    PromptVersion.name == name,
                )
            )
        ).scalars()
        for row in rows:
            if row.id == keep:
                continue
            labels = self._labels_of(row)
            if label in labels:
                row.labels = {"labels": [x for x in labels if x != label]}
        await self._session.flush()

    @staticmethod
    def _labels_of(row: PromptVersion) -> tuple[str, ...]:
        raw = row.labels or {}
        values = raw.get("labels", []) if isinstance(raw, dict) else []
        return tuple(str(x) for x in values)

    def _to_snapshot(self, row: PromptVersion) -> PromptSnapshot:
        return PromptSnapshot(
            id=row.id,
            name=row.name,
            version=row.version,
            template=row.template,
            variables=dict(row.variables),
            labels=self._labels_of(row),
            content_hash=row.content_hash,
        )
