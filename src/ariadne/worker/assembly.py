"""Worker 装配层 —— 构造 LoopEngine 及其依赖。

从 loop_worker.py 提取装配职责，减少单文件行数（loop_worker.py 849 行超标 49 行）。
装配层负责：
1. 加载并编译 Harness 规则集
2. 构造审计池（PostgresAuditSink / NullAuditSink）
3. 选择命令执行器（SandboxRunner / RestrictedSubprocess）
4. 装配 LoopEngine（注入 LLM / checkpoint / harness / workspace）

调用方：LoopWorker._build_engine 委托给此模块的 build_loop_engine 函数。
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from ariadne.config import Settings
from ariadne.loop_module.budget import BudgetGuard
from ariadne.loop_module.engine import LLMClient, LoopConfig, LoopEngine
from ariadne.loop_module.goal import Goal
from ariadne.loop_module.idempotency import RedisIdempotencyStore
from ariadne.loop_module.modes import LoopModeFactory
from ariadne.storage.postgres.engine import PostgresStore

logger = logging.getLogger(__name__)


def load_harness_evaluator(settings: Settings) -> Any:
    """从 settings.harness.rules_dir 加载并编译规则集。

    返回 HarnessEvaluator 或 None（规则目录未配置或为空）。
    加载失败记录 warning 并返回 None（fail-open，不阻断 worker 启动）。
    """
    rules_dir = settings.harness.rules_dir
    if not rules_dir:
        return None

    rules_path = Path(rules_dir)
    if not rules_path.is_dir():
        logger.warning(
            "harness rules_dir 不存在，跳过规则加载",
            extra={"rules_dir": rules_dir},
        )
        return None

    try:
        from ariadne.harness_module.evaluator import HarnessEvaluator
        from ariadne.harness_module.loader import compile_rule_set, load_rule_set

        rules = load_rule_set(rules_path)
        if not rules:
            return None
        # 把 settings.harness 的求值策略注入求值器。不做这一步，
        # eval_timeout_ms 与 fail_closed 就是死配置：生产调用方
        # （engine._precheck / GuardedLLMAdapter）都不传超时参数。
        evaluator = HarnessEvaluator.from_settings(
            compile_rule_set(rules).rules, settings.harness
        )
        if not settings.harness.fail_closed:
            logger.warning(
                "harness fail_closed=False：规则求值故障将放行而非拒绝，"
                "这会让求值 bug 变成硬约束失效，仅供本地调试",
                extra={"rule_count": len(rules)},
            )
        return evaluator
    except Exception as exc:
        logger.warning(
            "harness 规则集加载失败，worker 将以无规则模式运行",
            extra={"rules_dir": rules_dir, "error": str(exc)},
        )
        return None


def build_audit_sink(settings: Settings, pg: PostgresStore) -> Any:
    """构造审计池。

    settings.harness.audit_enabled=True 时写 Postgres，否则用 NullAuditSink。
    """
    if not settings.harness.audit_enabled:
        from ariadne.harness_module.audit import NullAuditSink

        return NullAuditSink()

    from ariadne.harness_module.audit import PostgresAuditSink

    return PostgresAuditSink(pg)


async def build_loop_engine(
    *,
    loop_id: uuid.UUID,
    goal: Goal,
    pg: PostgresStore,
    project_id: uuid.UUID,
    settings: Settings,
    llm_client: LLMClient,
    counter_fn: Any,
    workspace: Path | None,
    workspace_base: Path,
) -> LoopEngine:
    """装配 LoopEngine。IO 全部接真实后端：Postgres 检查点 + Redis 预算。

    M4：从 settings.harness 加载规则集，编译 HarnessEvaluator，
    用 GuardedLLMAdapter 包装 LLM 客户端，并把 harness + audit_sink 注入 LoopConfig。
    harness=None 时 _precheck 行为同 M3（硬编码放行）。

    桌面端自定义模型：优先用项目默认模型配置装配 LLMClient（让用户在
    UI 配 provider/model/key/base_url），无配置回退 llm_client（环境变量）。
    """
    from ariadne.loop_module.tools import WorkspaceToolExecutor
    from ariadne.runtime_module.llm.resolver import resolve_project_llm
    from ariadne.worker.loop_worker import build_command_runner

    loop_id_str = str(loop_id)
    counter = counter_fn(settings.redis.url)

    # --- 按项目解析 LLM（自定义配置优先，回退环境变量）---
    base_llm, resolved_model = await resolve_project_llm(
        pg, project_id, env_settings=settings.llm, fallback_client=llm_client
    )

    # --- M4: 加载并编译 Harness 规则集 ---
    harness_evaluator = load_harness_evaluator(settings)

    # --- M4: 构造审计池 ---
    audit_sink = build_audit_sink(settings, pg)

    # --- M4: 选 COMMAND 断言的执行器（沙箱 or 降级受限子进程）---
    command_runner = build_command_runner(settings)

    # --- M4: 用 GuardedLLMAdapter 包装 LLM ---
    from ariadne.runtime_module.llm.guarded import GuardedLLMAdapter

    llm = base_llm
    if harness_evaluator is not None:
        llm = GuardedLLMAdapter(
            inner=base_llm,
            evaluator=harness_evaluator,
            audit_sink=audit_sink,
            project_id=project_id,
            loop_id=loop_id_str,
        )

    # --- 装配 LoopEngine ---
    from ariadne.worker.loop_worker import RedisEventSink, _PgCheckpointStore

    tool_executor: Any = WorkspaceToolExecutor(
        base_dir=workspace_base, loop_id=str(loop_id)
    )
    # 工具写入挂 pre_persist 卡点：与 GuardedArtifactWriter 同构，规则在
    # 内容落盘前求值（output-sensitive-high / output-huge）。loop_state
    # 源后绑（见下），与 GuardedLLMAdapter 同一模式。
    if harness_evaluator is not None:
        tool_executor.harness = harness_evaluator
        tool_executor.audit_sink = audit_sink
        tool_executor.project_id = project_id
        tool_executor.loop_id = loop_id_str

    engine = LoopEngine(
        LoopConfig(
            goal=goal,
            loop_id=loop_id_str,
            project_id=project_id,
            budget_guard=BudgetGuard(
                loop_id=loop_id_str, budget=goal.budget, counter=counter
            ),
            llm=llm,
            checkpoint_store=_PgCheckpointStore(pg),
            mode=LoopModeFactory(goal.mode),
            idempotency=RedisIdempotencyStore(settings.redis),
            event_sink=RedisEventSink(settings.redis.url, loop_id_str),
            model=resolved_model,
            degraded_model=settings.llm.degraded_model,
            harness=harness_evaluator,
            audit_sink=audit_sink,
            command_runner=command_runner,
            artifact_path=workspace,
            tool_executor=tool_executor,
        )
    )

    # 后绑 loop 状态源：适配器在引擎之前构造（引擎把 llm 当入参），
    # 只能等引擎建好再回填。不接线的后果不是"少了点上下文"，而是
    # post_model 的 resource-cost-cap 每次都读不到 ctx.loop.cost_limit
    # → CEL 缺键错误 → fail-closed 命中 → 每次 LLM 响应都被拦。
    if isinstance(llm, GuardedLLMAdapter):
        llm.loop_state_provider = engine.harness_loop_context

    # 工具执行器的 loop 状态源同样后绑（引擎构造后才能拿到）
    tool_executor.loop_state_provider = engine.harness_loop_context

    return engine


__all__ = [
    "build_audit_sink",
    "build_loop_engine",
    "load_harness_evaluator",
]
