"""入口命令：ariadne-api / ariadne-worker / ariadne-migrate。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from ariadne.config import get_settings, validate_production_secrets
from ariadne.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)

_DDL_RELATIVE = "deploy/clickhouse"
_ALEMBIC_INI = "alembic.ini"


def _locate(relative: str) -> Path | None:
    """定位随部署分发但不在 wheel 里的文件（ClickHouse DDL、alembic.ini）。

    hatch 只打包 src/ariadne，所以这些路径不能从 __file__ 推。非 editable
    安装时 __file__ 落在 site-packages/ariadne/ 下，parents[2] 指向
    python3.11/ 而不是仓库根 —— 原先的写法在容器里必然找不到，且因为
    migrate 入口从未被真正调用过，这个错一直没暴露。

    工作目录优先：镜像 WORKDIR=/app，COPY 把 deploy/ 与 alembic.ini 放在
    那里；parents[2] 作为 editable 安装（仓库内直接跑）的回退。
    """
    for base in (Path.cwd(), Path(__file__).resolve().parents[2]):
        candidate = base / relative
        if candidate.exists():
            return candidate
    return None


def run_api() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "ariadne.api.app:app",
        host=settings.api.host,
        port=settings.api.port,
        log_config=None,  # 交给我们自己的结构化日志
    )


def run_worker() -> None:
    """Collector / Loop / Eval Worker。

    子命令：
      - `ariadne-worker` 或 `ariadne-worker collector`：启动 Collector Worker
      - `ariadne-worker loop`：启动 Loop Worker（配 ARIADNE_LLM_* 装配真实 LLM）
      - `ariadne-worker eval`：启动 Eval Worker（CPU/LLM 密集，独立伸缩）
      - `ariadne-worker retention`：启动 Retention Worker（GDPR 级联删除）

    四类 Worker 独立伸缩（M6 §5）：
      - collector：高吞吐，按 span 速率伸缩
      - loop：长任务，按并发 Loop 数伸缩
      - eval：CPU/LLM 密集，按实验并发伸缩
      - retention：低频（人工触发），单副本足够 —— 跑多个只会互相抢同一批
        pending 行，删除幂等所以不会出错，但没有收益
    """
    # Worker 不建 FastAPI app，走不到 create_app 里那道校验，但它们同样读
    # api.jwt_secret / static_api_key 去调内部接口 —— 少了这行，"生产必须配
    # 真密钥"就只对 API 进程成立。
    validate_production_secrets(get_settings())

    subcommand = sys.argv[1] if len(sys.argv) > 1 else "collector"
    if subcommand == "loop":
        from ariadne.runtime_module.llm import build_llm_client
        from ariadne.worker.loop_worker import run_loop_worker

        settings = get_settings()
        llm = build_llm_client(settings.llm)
        try:
            asyncio.run(run_loop_worker(llm=llm))
        except KeyboardInterrupt:
            logger.info("loop worker interrupted")
        return

    if subcommand == "eval":
        from ariadne.runtime_module.llm import build_llm_client
        from ariadne.worker.eval_worker import run_eval_worker

        settings = get_settings()
        llm = build_llm_client(settings.llm)
        try:
            asyncio.run(run_eval_worker(llm=llm))
        except KeyboardInterrupt:
            logger.info("eval worker interrupted")
        return

    if subcommand == "retention":
        from ariadne.worker.retention_worker import run_retention_worker

        try:
            asyncio.run(run_retention_worker())
        except KeyboardInterrupt:
            logger.info("retention worker interrupted")
        return

    from ariadne.worker.collector import run_collector

    try:
        asyncio.run(run_collector())
    except KeyboardInterrupt:
        logger.info("worker interrupted")


def run_migrate() -> None:
    """迁移两个存储：ClickHouse DDL + Postgres Alembic。

    任一失败即退出非零 —— 半迁移状态比不迁移更危险（服务会以为
    schema 已就绪然后在运行时报缺列）。
    """
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)

    if len(sys.argv) > 1:
        ddl_dir = Path(sys.argv[1])
    else:
        located = _locate(_DDL_RELATIVE)
        if located is None:
            logger.error(
                "clickhouse ddl directory not found",
                extra={"searched": _DDL_RELATIVE, "cwd": str(Path.cwd())},
            )
            raise SystemExit(1)
        ddl_dir = located

    _migrate_clickhouse(ddl_dir)
    _migrate_postgres()
    logger.info("all migrations complete")


def _migrate_clickhouse(ddl_dir: Path) -> None:
    from ariadne.storage.clickhouse import ClickHouseStore

    if not ddl_dir.is_dir():
        logger.error("ddl directory not found", extra={"path": str(ddl_dir)})
        raise SystemExit(1)

    store = ClickHouseStore(get_settings().clickhouse)
    try:
        applied = store.migrate(ddl_dir)
    except Exception as exc:
        logger.error("clickhouse migration failed", extra={"error": str(exc)})
        raise SystemExit(1) from exc
    finally:
        store.close()

    logger.info("clickhouse migration complete", extra={"files": applied})


def _migrate_postgres() -> None:
    """执行 Alembic 迁移。

    刻意用 Alembic 而非 create_all：后者无版本记录、无回滚路径，
    线上用它会让"schema 是怎么变成现在这样的"无从追溯。
    """
    from alembic import command
    from alembic.config import Config

    ini_path = _locate(_ALEMBIC_INI)
    if ini_path is None:
        logger.error(
            "alembic.ini not found",
            extra={"searched": _ALEMBIC_INI, "cwd": str(Path.cwd())},
        )
        raise SystemExit(1)

    # script_location 在 ini 里是相对路径（deploy/alembic），Alembic 按进程
    # 工作目录解析，所以 ini 所在目录必须是工作目录。
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(ini_path.parent / "deploy" / "alembic"))

    try:
        command.upgrade(config, "head")
    except Exception as exc:
        logger.error("postgres migration failed", extra={"error": str(exc)})
        raise SystemExit(1) from exc

    logger.info("postgres migration complete")
