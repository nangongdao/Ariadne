"""数据集仓储。

核心约束：**版本不可变**。新增样本或修改内容必须创建新版本，
绝不原地改 —— 否则历史实验的 content_hash 会失效，复现校验形同虚设。
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ariadne.experiment.dataset import Dataset as DomainDataset
from ariadne.experiment.dataset import DatasetItem
from ariadne.storage.postgres.models import Dataset as DatasetRow
from ariadne.storage.postgres.models import DatasetItemRow


class DatasetNotFoundError(LookupError):
    pass


class DatasetVersionConflictError(ValueError):
    """尝试创建已存在的版本号。

    显式报错而非自动递增：调用方以为在建 v2 实际建了 v3，
    会让实验记录里的版本引用指向意料之外的数据。
    """


class DatasetRepository:
    """数据集读写。所有方法都强制带 project_id（多租户隔离的应用层防线）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def next_version(self, *, project_id: uuid.UUID, name: str) -> int:
        result = await self._session.execute(
            select(func.max(DatasetRow.version)).where(
                DatasetRow.project_id == project_id, DatasetRow.name == name
            )
        )
        current = result.scalar_one_or_none()
        return 1 if current is None else int(current) + 1

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        items: list[DatasetItem],
        version: int | None = None,
        description: str = "",
    ) -> DomainDataset:
        """创建新版本。version 缺省时自动取下一个。"""
        resolved_version = (
            version
            if version is not None
            else await self.next_version(project_id=project_id, name=name)
        )

        exists = await self._session.execute(
            select(DatasetRow.id).where(
                DatasetRow.project_id == project_id,
                DatasetRow.name == name,
                DatasetRow.version == resolved_version,
            )
        )
        if exists.scalar_one_or_none() is not None:
            raise DatasetVersionConflictError(
                f"数据集 {name!r} 的版本 {resolved_version} 已存在。"
                "版本不可变，请用新版本号或不指定 version 让系统自动递增。"
            )

        dataset_id = uuid.uuid4()
        # 先构造领域对象：它会校验 item_id 唯一并算出 content_hash
        domain = DomainDataset.create(
            dataset_id=str(dataset_id),
            name=name,
            version=resolved_version,
            items=items,
            description=description,
        )

        row = DatasetRow(
            id=dataset_id,
            project_id=project_id,
            name=name,
            version=resolved_version,
            content_hash=domain.content_hash,
            item_count=len(domain),
            description=description,
            items=[
                DatasetItemRow(
                    item_id=item.item_id,
                    input=item.input,
                    expected=item.expected,
                    item_metadata=dict(item.metadata),
                )
                for item in domain.items
            ],
        )
        self._session.add(row)
        await self._session.flush()
        return domain

    async def get(
        self, *, project_id: uuid.UUID, name: str, version: int | None = None
    ) -> DomainDataset:
        """取指定版本；version 缺省取最新。"""
        stmt = (
            select(DatasetRow)
            .options(selectinload(DatasetRow.items))
            .where(DatasetRow.project_id == project_id, DatasetRow.name == name)
        )
        stmt = (
            stmt.where(DatasetRow.version == version)
            if version is not None
            else stmt.order_by(DatasetRow.version.desc()).limit(1)
        )

        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            suffix = f"@v{version}" if version is not None else ""
            raise DatasetNotFoundError(f"数据集不存在: {name}{suffix}")

        return self._to_domain(row)

    async def get_by_id(
        self, *, project_id: uuid.UUID, dataset_id: uuid.UUID
    ) -> DomainDataset:
        row = (
            await self._session.execute(
                select(DatasetRow)
                .options(selectinload(DatasetRow.items))
                .where(
                    DatasetRow.id == dataset_id, DatasetRow.project_id == project_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise DatasetNotFoundError(f"数据集不存在: {dataset_id}")
        return self._to_domain(row)

    async def list_names(self, *, project_id: uuid.UUID) -> list[tuple[str, int]]:
        """列出 (名称, 最新版本)。"""
        result = await self._session.execute(
            select(DatasetRow.name, func.max(DatasetRow.version))
            .where(DatasetRow.project_id == project_id)
            .group_by(DatasetRow.name)
            .order_by(DatasetRow.name)
        )
        return [(str(name), int(version)) for name, version in result.all()]

    async def list_versions(
        self, *, project_id: uuid.UUID, name: str
    ) -> list[tuple[int, str, int]]:
        """列出某数据集的全部版本 (version, content_hash, item_count)。"""
        result = await self._session.execute(
            select(DatasetRow.version, DatasetRow.content_hash, DatasetRow.item_count)
            .where(DatasetRow.project_id == project_id, DatasetRow.name == name)
            .order_by(DatasetRow.version.desc())
        )
        return [(int(v), str(h), int(c)) for v, h, c in result.all()]

    async def verify_hash(
        self, *, project_id: uuid.UUID, name: str, version: int, expected_hash: str
    ) -> bool:
        """校验存储的 hash 与期望一致。

        用于对比实验前确认"两次实验用的确实是同一数据集"——
        docs/M2 里 compare() 的数据集一致性检查依赖它。
        """
        row = (
            await self._session.execute(
                select(DatasetRow.content_hash).where(
                    DatasetRow.project_id == project_id,
                    DatasetRow.name == name,
                    DatasetRow.version == version,
                )
            )
        ).scalar_one_or_none()
        return row == expected_hash

    @staticmethod
    def _to_domain(row: DatasetRow) -> DomainDataset:
        """从行构造领域对象。

        刻意用 Dataset(...) 直接构造而非 Dataset.create(...)：后者会重算
        hash，而我们要保留库里存的值 —— 这样 verify() 才能检测出数据被
        篡改（若重算就永远一致，检测能力为零）。
        """
        return DomainDataset(
            dataset_id=str(row.id),
            name=row.name,
            version=row.version,
            items=tuple(
                DatasetItem(
                    item_id=item.item_id,
                    input=item.input,
                    expected=item.expected,
                    metadata=dict(item.item_metadata),
                )
                for item in sorted(row.items, key=lambda i: i.item_id)
            ),
            content_hash=row.content_hash,
            description=row.description,
        )
