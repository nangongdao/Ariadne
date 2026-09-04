"""API Key 前缀 -> project_id 的跨租户解析。

认证的鸡生蛋问题：api_keys 上的 RLS 策略要求 ariadne.project_id 已设置，
但认证时 project_id 恰恰还不知道 —— 它藏在待验证的这把 key 里。GUC 未设时
current_setting 返回 NULL，`project_id = NULL` 恒为 NULL，非 owner 角色
一行都看不见。所以认证必须先有一步不受 RLS 约束的前缀查找。

PG 侧走 SECURITY DEFINER 函数 ariadne_api_key_projects（迁移
d0e1f2a3b4c5 创建），它只返回 project_id，不返回 key_hash。哈希仍由
调用方拿 project_id 开 tenant_session 后经 RLS 正常取。这样：
- 提权面只有"某前缀属于哪个项目"这一条信息
- RLS 留在认证主路径上 —— 配错了当场 401，不会线上静默失效

SQLite（单元测试）没有 RLS，直接查表。

返回列表而非单值：uq_api_key_prefix 是 (project_id, key_prefix) 复合唯一，
前缀只在项目内唯一，跨项目可以重复。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.auth_models import ApiKey

_LOOKUP_FN = "public.ariadne_api_key_projects"


async def resolve_projects_by_prefix(
    session: AsyncSession, key_prefix: str
) -> list[uuid.UUID]:
    """按 key 前缀查出候选 project_id（不返回哈希）。

    Args:
        session: 普通会话即可 —— 本查询刻意不需要租户上下文。
        key_prefix: 明文 key 的前 16 字符（见 auth.keys.extract_prefix）。

    Returns:
        候选 project_id 列表。前缀不存在时为空列表。
    """
    dialect = session.bind.dialect.name if session.bind else "unknown"

    if dialect == "sqlite":
        result = await session.execute(
            select(ApiKey.project_id).where(
                ApiKey.key_prefix == key_prefix,
                ApiKey.is_active.is_(True),
            )
        )
    else:
        result = await session.execute(
            text(f"SELECT * FROM {_LOOKUP_FN}(:key_prefix)"),
            {"key_prefix": key_prefix},
        )

    # PG 侧 asyncpg 直接回 uuid.UUID；SQLite 侧 UuidType 已在
    # process_result_value 里转好。str() 兜底是为了裸 text() 查询绕过
    # 类型装饰器的情况，不是防御不可能的输入。
    return [
        value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        for value in result.scalars().all()
    ]


__all__ = ["resolve_projects_by_prefix"]
