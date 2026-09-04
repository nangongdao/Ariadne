"""Alembic 环境。

DSN 从 Ariadne Settings 读取而非 alembic.ini —— 密码不该进版本控制。

生产环境**只能**通过 Alembic 改 schema，禁止 create_all：后者无版本记录、
无回滚路径，用它会让"schema 是怎么变成现在这样的"无从追溯。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# 副作用导入：这些模块把各自的表注册到 Base.metadata，autogenerate 才能看见。
# 只有最后一个需要 noqa —— 重复 `import ariadne.x.y` 都绑定同一个 `ariadne`，
# 前面的绑定被遮蔽后 F401 不会触发，多写 noqa 反而被 RUF100 判为冗余。
import ariadne.storage.postgres.graph_models
import ariadne.storage.postgres.harness_models
import ariadne.storage.postgres.loop_models
import ariadne.storage.postgres.model_config_models
import ariadne.storage.postgres.retention_models  # noqa: F401
from ariadne.config import get_settings
from ariadne.storage.postgres.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# asyncpg 的 DSN 直接用于 Alembic 需要 async 迁移路径（下方 run_async_migrations）
config.set_main_option("sqlalchemy.url", get_settings().postgres.dsn())


def run_migrations_offline() -> None:
    """生成 SQL 脚本而不连库。用于需要 DBA 审核的场景。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # 开启类型与默认值比对：否则 autogenerate 会漏掉列类型变更
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
