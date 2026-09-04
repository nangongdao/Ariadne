"""节点执行器装配层 —— 把 8 种 executor 接到 GraphExecutor 上。

R12 第六实例的修复。此前 8 种 NodeExecutor 全部实现完整、单测全绿，却在
src 里没有任何构造点：`GraphExecutor.run()` 要求传入 node_executors dict，
而唯一传它的地方是 nodes/subgraph.py:98 的递归自调 —— 子图把父图给它的
dict 再传给下一层，整条链没有源头。API 只有 CRUD，没有 execute 端点。

三类依赖注入方式，刻意区分：
- 无依赖（branch / eval）：直接实例化
- 外部 IO（llm / loop / rag / tool / code）：依赖不可用时**不注册**该 kind，
  让 GraphExecutor 报"没有注册 executor"并 fail-fast，而不是注册一个假的
  执行器返回空串。后者会让图"执行成功"但结果全空 —— 与 _AlwaysPassEvaluator
  永远给 100 分是同一类失效模式。
- 自引用（subgraph）：需要拿到 dict 本身，所以最后就地回填。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ariadne.graph_module.executor import NodeExecutor
from ariadne.graph_module.models import NodeKind
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.config import Settings
    from ariadne.graph_module.nodes.code import CodeRunner
    from ariadne.graph_module.nodes.rag import Retriever
    from ariadne.loop_module.engine import LLMClient

logger = get_logger(__name__)

ToolExecutor = Any  # Callable[[str, dict[str, Any]], Awaitable[str]]


def build_node_executors(
    *,
    llm: LLMClient | None = None,
    default_model: str | None = None,
    code_runner: CodeRunner | None = None,
    retriever: Retriever | None = None,
    tool_executor: ToolExecutor | None = None,
) -> dict[NodeKind, NodeExecutor]:
    """按可用依赖装配 executor 表。

    缺依赖的 kind 不进表 —— 图里真用到它时 GraphExecutor 会把该节点标 failed
    并给出"没有注册 executor"，这比静默返回空输出可诊断得多。

    subgraph 的 executor 需要引用最终的表本身（递归执行子图），所以在表建好
    后回填。它拿到的是同一个 dict 对象，因此子图与父图的能力集始终一致。
    """
    from ariadne.graph_module.nodes.branch import BranchNodeExecutor
    from ariadne.graph_module.nodes.eval import EvalNodeExecutor

    executors: dict[NodeKind, NodeExecutor] = {
        NodeKind.BRANCH: BranchNodeExecutor(),
        NodeKind.EVAL: EvalNodeExecutor(),
    }

    if llm is not None:
        from ariadne.graph_module.nodes.llm import LLMNodeExecutor
        from ariadne.graph_module.nodes.loop import LoopNodeExecutor

        executors[NodeKind.LLM] = LLMNodeExecutor(llm, default_model=default_model)
        executors[NodeKind.LOOP] = LoopNodeExecutor(llm, default_model=default_model)

    if code_runner is not None:
        from ariadne.graph_module.nodes.code import CodeNodeExecutor

        executors[NodeKind.CODE] = CodeNodeExecutor(code_runner)

    if retriever is not None:
        from ariadne.graph_module.nodes.rag import RAGNodeExecutor

        executors[NodeKind.RAG] = RAGNodeExecutor(retriever)

    if tool_executor is not None:
        from ariadne.graph_module.nodes.tool import ToolNodeExecutor

        executors[NodeKind.TOOL] = ToolNodeExecutor(tool_executor)

    # 自引用：subgraph 递归执行时复用同一张表
    from ariadne.graph_module.nodes.subgraph import SubgraphNodeExecutor

    executors[NodeKind.SUBGRAPH] = SubgraphNodeExecutor(executors)

    missing = sorted(k.value for k in NodeKind if k not in executors)
    if missing:
        logger.info(
            "部分节点类型无可用 executor，用到它们的图会 fail-fast",
            extra={"unavailable_kinds": missing},
        )
    return executors


def build_node_executors_from_settings(
    settings: Settings,
    *,
    llm: LLMClient | None = None,
    default_model: str | None = None,
    retriever: Retriever | None = None,
    tool_executor: ToolExecutor | None = None,
) -> dict[NodeKind, NodeExecutor]:
    """从 Settings 装配 —— 生产入口用这个。

    llm 为 None 时按 settings.llm 构造。api_key 未配置则不注册 llm/loop
    两种节点：构造一个注定 401 的 client 会把配置缺失变成运行时图执行失败。

    code_runner 走与 COMMAND 断言相同的选择逻辑（沙箱优先、按
    fallback_to_restricted 决定是否降级），因此 Code 节点与 COMMAND 断言的
    隔离强度天然一致，不会出现"断言跑在沙箱里、Code 节点跑在裸子进程里"。

    retriever 没有内置实现（M5 未定检索后端），不注入则 RAG 节点不可用。
    """
    resolved_llm = llm
    if resolved_llm is None and settings.llm.api_key.get_secret_value():
        from ariadne.runtime_module.llm import build_llm_client

        resolved_llm = build_llm_client(settings.llm)
    elif resolved_llm is None:
        logger.warning(
            "ARIADNE_LLM_API_KEY 未配置：llm / loop 节点不可用",
        )

    return build_node_executors(
        llm=resolved_llm,
        default_model=default_model,
        code_runner=_build_code_runner(settings),
        retriever=retriever,
        tool_executor=tool_executor,
    )


def _build_code_runner(settings: Settings) -> CodeRunner:
    """复用 COMMAND 断言的执行器选择逻辑。

    `build_command_runner` 已经实现了完整的沙箱选择 + 降级 + 不可信代码组合
    校验。复用它而不是重写一份：两处逻辑分叉的表现是改了一处安全检查、
    另一处仍是旧行为，且没有任何测试会发现。

    它返回 None 表示"沙箱不可用但允许降级"，这里补上 RestrictedRunner ——
    与 CommandVerifier 的默认值一致，因此 Code 节点与 COMMAND 断言的隔离
    强度天然相同。CodeRunner 与 CommandRunner 结构一致，无需适配层。
    """
    from ariadne.worker.loop_worker import build_command_runner

    runner = build_command_runner(settings)
    if runner is not None:
        return runner  # type: ignore[no-any-return]

    from ariadne.loop_module.verifier.command import RestrictedRunner

    return RestrictedRunner()


__all__ = [
    "build_node_executors",
    "build_node_executors_from_settings",
]
