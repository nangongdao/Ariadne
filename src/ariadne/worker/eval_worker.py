"""Eval Worker —— 消费实验任务并批量评测。

M6 §5：三类 Worker 独立伸缩。
  - collector-worker：高吞吐，CPU 轻量
  - loop-worker：长任务，单任务分钟级
  - eval-worker：CPU + LLM 密集，批量评测（本文件）

生命周期：API 创建 experiment 行（status=pending）→ 入 eval 队列 →
Worker 认领 → 转 running → 加载数据集 + 构建评测器 → 批量评测 →
回传结果 + 转 completed → ACK。

崩溃安全：未 ACK 消息由 XAUTOCLAIM 回收，experiment 行 status=running
会被其他 Worker 重新认领（running→running 不报错，幂等重跑）。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time
import uuid
from decimal import Decimal
from typing import Any, ClassVar

from ariadne.auth.tenant import tenant_session
from ariadne.config import Settings, get_settings
from ariadne.eval_module.base import BaseEvaluator, EvalContext, EvalResult, EvaluatorKind
from ariadne.eval_module.composite import CompositeScorer, ScoreSpec
from ariadne.eval_module.deterministic import build_deterministic_evaluator
from ariadne.experiment.dataset import Dataset, DatasetItem
from ariadne.experiment.runner import (
    ExperimentRunner,
    GenerationOutput,
)
from ariadne.observability.metrics import eval_score, llm_errors_total, llm_request_duration_seconds
from ariadne.runtime_module.llm import build_llm_client
from ariadne.storage.postgres.engine import PostgresStore
from ariadne.storage.postgres.repositories.datasets import (
    DatasetNotFoundError,
    DatasetRepository,
)
from ariadne.storage.postgres.repositories.experiments import (
    ExperimentNotFoundError,
    ExperimentRepository,
)
from ariadne.utils.logging import configure_logging, get_logger
from ariadne.worker.eval_queue import EvalQueue

logger = get_logger(__name__)

_POLL_INTERVAL = 2.0


class LLMGenerator:
    """LLM 驱动的生成器。

    实现 Generator Protocol：每条样本调一次 LLM，返回输出 + 成本。
    生成侧失败记为 generation_error，不中断整个实验。

    generate() 保持同步（Generator 协议、ExperimentRunner 同步），但
    async 事件循环内调用时不能直接 asyncio.run —— 那会在**已有运行循环**
    时抛 RuntimeError（eval worker 的 async _process 正是这种情况）。
    复用 sandbox_module/base.py 的"新线程跑独立循环"桥接模式。
    """

    def __init__(self, llm: Any, model: str) -> None:
        self._llm = llm
        self._model = model

    def generate(self, item: DatasetItem) -> GenerationOutput:
        start = time.monotonic()
        try:
            response = self._call_llm(item.input)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            llm_request_duration_seconds.labels(
                provider="", model=self._model
            ).observe(elapsed_ms / 1000)
            return GenerationOutput(
                text=response.output,
                cost_usd=response.cost_usd,
                duration_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            llm_errors_total.labels(
                provider="", model=self._model, error_code=type(exc).__name__
            ).inc()
            return GenerationOutput(
                text="",
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=elapsed_ms,
            )

    def _call_llm(self, prompt: str) -> Any:
        """同步调用 async LLM client：无运行循环直接 asyncio.run，
        已有运行循环（eval worker async 上下文）则新线程跑独立循环。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._llm.complete(prompt, model=self._model))

        result_holder: list[Any] = [None]
        error_holder: list[Exception | None] = [None]

        def _in_thread() -> None:
            try:
                result_holder[0] = asyncio.run(
                    self._llm.complete(prompt, model=self._model)
                )
            except Exception as exc:
                error_holder[0] = exc

        import threading

        t = threading.Thread(target=_in_thread)
        t.start()
        t.join()
        if error_holder[0]:
            raise error_holder[0]
        return result_holder[0]


class NoScorersConfiguredError(ValueError):
    """实验配置里没有任何评分器。

    单独成类是为了让 Worker 能把它翻译成 experiment failed + 明确原因，
    而不是与"评估器类型写错"混在一起。
    """


def build_scorer_from_config(config: dict[str, Any]) -> CompositeScorer:
    """从实验配置构建复合评分器。

    config 结构：
    {
        "scorers": [
            {"type": "regex", "pattern": "...", "weight": 1.0, "threshold": 0.8},
            {"type": "json_schema", "schema": {...}, "weight": 2.0},
            ...
        ],
        "threshold": 85.0
    }

    **没有 scorers 时抛 NoScorersConfiguredError，不再兜底成 always-pass。**
    旧行为是造一个恒通过的评估器，于是复合分恒为 100、pass_rate 恒为 1.0，
    然后这些数字照常写进 experiments.metrics、照常参与 compare 的门禁判定。
    一个"没配评分器"的实验因此长得和"完美通过"的实验一模一样 —— 门禁会
    放行任何变更。这与记忆里 `_AlwaysPassEvaluator` 永远给 100 分是同一类
    失效模式：假成功比失败危险，因为没人会去查一个满分结果。

    确实要跑无评分实验（只看生成成本/延迟）时显式配 `"allow_no_scorers": true`
    —— 此时评估器名叫 no_scorers_configured 而非 always_pass，metrics 的
    evaluators 字典里会带着这个名字落库，看结果的人一眼知道分数不代表质量。
    """
    scorer_configs = config.get("scorers", [])
    threshold = config.get("threshold", 85.0)

    if not scorer_configs:
        if not config.get("allow_no_scorers"):
            raise NoScorersConfiguredError(
                "实验配置缺少 scorers：没有评分器的实验会得到恒为 100 的复合分，"
                '门禁将无条件放行。请配置至少一个评分器，或显式设 "allow_no_scorers": true '
                "表示只测生成成本与延迟。"
            )
        always = _AlwaysPassEvaluator(name="no_scorers_configured")
        return CompositeScorer(
            (ScoreSpec(evaluator=always, weight=1.0),),
            threshold=threshold,
        )

    specs: list[ScoreSpec] = []
    for sc in scorer_configs:
        evaluator = build_deterministic_evaluator(sc)
        specs.append(
            ScoreSpec(
                evaluator=evaluator,
                weight=float(sc.get("weight", 1.0)),
                threshold=sc.get("threshold"),
                normalize_max=sc.get("normalize_max"),
            )
        )
    return CompositeScorer(tuple(specs), threshold=threshold)


class _AlwaysPassEvaluator(BaseEvaluator):
    """恒通过评估器，只在显式 allow_no_scorers 时使用。

    默认 name 是 no_scorers_configured 而不是 always_pass：这个名字会随
    evaluator_metrics 落到 experiments.metrics 里，读结果的人应当能从名字
    本身看出"这一轮没有质量评分"。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(self, name: str = "no_scorers_configured") -> None:
        super().__init__(name)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        return EvalResult(
            name=self.name,
            value=1.0,
            passed=True,
            evidence="未配置评分器，此分数不代表输出质量",
            cost_usd=Decimal("0"),
            duration_ms=0,
        )


class EvalWorker:
    """评测 Worker。

    与 LoopWorker 同构：队列认领 → 装配 → 执行 → 终态落库 → ACK。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: Any | None = None,
        queue: EvalQueue | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._llm = llm
        self._consumer = f"{socket.gethostname()}-{id(self)}"
        self._queue = queue or EvalQueue(self._settings.redis)
        self._running = False
        self._stats = {"claimed": 0, "completed": 0, "failed": 0, "skipped": 0}

    async def start(self) -> None:
        await self._queue.connect()
        await self._queue.ensure_group()
        self._running = True
        logger.info(
            "eval worker started",
            extra={"consumer": self._consumer, "has_llm": self._llm is not None},
        )
        while self._running:
            try:
                await self._poll()
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "eval worker poll error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
                await asyncio.sleep(1.0)
        logger.info("eval worker stopped", extra=dict(self._stats))

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """一轮轮询：认领任务 → 有界并发处理。

        与 LoopWorker._poll 同构，原因也同构：一次认领 _MAX_CLAIM=5 个，未
        ACK 消息 idle 超过 RECLAIM_MIN_IDLE_MS（120s）就被回收，而跑完一个
        数据集远超这个时间。串行时队尾的实验纯等待自己的回收期限到达，接管
        者读到 status="running" 也不会跳过（只有 completed/failed 才跳），
        于是同一个实验被两个 Worker 各跑一遍 —— 双倍 LLM 成本，且两份结果
        互相覆盖。
        """
        claimed = await self._queue.claim(self._consumer)
        if not claimed:
            return

        sem = asyncio.Semaphore(max(1, self._settings.worker.eval_concurrency))

        async def _one(message_id: str, experiment_id: str, project_id_str: str) -> None:
            async with sem:
                self._stats["claimed"] += 1
                try:
                    await self._process(experiment_id, project_id_str)
                    await self._queue.ack(message_id)
                except Exception as exc:
                    self._stats["failed"] += 1
                    logger.error(
                        "eval processing failed",
                        extra={
                            "experiment_id": experiment_id,
                            "error": str(exc),
                        },
                        exc_info=True,
                    )

        await asyncio.gather(
            *(_one(*item) for item in claimed), return_exceptions=True
        )

    async def _process(self, experiment_id: str, project_id_str: str) -> None:
        """处理单个实验：加载配置 → 加载数据集 → 评测 → 回传结果。"""
        pg = PostgresStore(self._settings.postgres)
        try:
            exp_uuid = uuid.UUID(experiment_id)
            project_uuid = uuid.UUID(project_id_str)
            async with tenant_session(pg, project_uuid) as session:
                repo = ExperimentRepository(session)
                try:
                    row = await repo.get(
                        project_id=project_uuid, experiment_id=exp_uuid
                    )
                except ExperimentNotFoundError:
                    self._stats["skipped"] += 1
                    return

                if row.status == "completed":
                    self._stats["skipped"] += 1
                    return
                if row.status == "failed":
                    self._stats["skipped"] += 1
                    return

                # 转 running
                if row.status == "pending":
                    await repo.transition(
                        project_id=project_uuid,
                        experiment_id=exp_uuid,
                        to="running",
                    )

                config = row.config or {}
                dataset_ref = row.dataset_ref
                dataset_id = row.dataset_id

            # 加载数据集
            dataset = await self._load_dataset(pg, project_uuid, dataset_ref, dataset_id)
            if dataset is None:
                await self._fail(pg, project_uuid, exp_uuid, f"数据集不存在: {dataset_ref}")
                self._stats["failed"] += 1
                return

            # 构建评测器。配置错误是永久性的：类型名写错、参数名写错、
            # 缺 scorers，重试一万次仍然是同一个错。所以翻成 experiment
            # failed + 原文报错并 return（让 _poll 走到 ACK），而不是抛出去
            # 变成毒消息 —— 后者会让这条消息被 XAUTOCLAIM 反复回收，
            # experiment 行永远停在 running。
            # ValueError 一支覆盖三类：NoScorersConfiguredError、未知评估器
            # 类型 / 未知参数键、JudgeNeedsClientError。
            try:
                scorer = build_scorer_from_config(config)
            except ValueError as exc:
                await self._fail(
                    pg, project_uuid, exp_uuid, f"评分器配置无效: {exc}"
                )
                self._stats["failed"] += 1
                logger.warning(
                    "experiment failed on scorer config",
                    extra={"experiment_id": experiment_id, "error": str(exc)},
                )
                return

            # 构建生成器
            llm = self._llm
            if llm is None:
                llm = build_llm_client(self._settings.llm)
            model = config.get("model", self._settings.llm.model)
            generator = LLMGenerator(llm, model)

            # 批量评测
            runner = ExperimentRunner(scorer=scorer)
            result = runner.run(
                experiment_id=experiment_id,
                dataset=dataset,
                generator=generator,
                config_label=config.get("label", "default"),
            )

            # 回传结果
            async with tenant_session(pg, project_uuid) as session:
                repo = ExperimentRepository(session)
                await repo.save_result(
                    project_id=project_uuid,
                    experiment_id=exp_uuid,
                    result=result,
                )

            # 记录评测分数指标
            metrics = result.metrics()
            composite_score = metrics.get("composite_quality", 0.0)
            eval_score.labels(
                project=project_id_str,
                evaluator="composite",
            ).observe(composite_score)

            self._stats["completed"] += 1
            logger.info(
                "experiment completed",
                extra={
                    "experiment_id": experiment_id,
                    "items": len(result.outcomes),
                    "failures": len(result.generation_failures),
                    "composite": composite_score,
                },
            )
        finally:
            await pg.close()

    async def _load_dataset(
        self,
        pg: PostgresStore,
        project_uuid: uuid.UUID,
        dataset_ref: str,
        dataset_id: uuid.UUID | None,
    ) -> Dataset | None:
        """从 Postgres 加载数据集。优先用 dataset_id，否则用 ref。"""
        try:
            async with tenant_session(pg, project_uuid) as session:
                repo = DatasetRepository(session)
                if dataset_id is not None:
                    return await repo.get_by_id(
                        project_id=project_uuid, dataset_id=dataset_id
                    )
                # dataset_ref 格式：name 或 name@vN
                if "@" in dataset_ref:
                    name, version_str = dataset_ref.rsplit("@v", 1)
                    return await repo.get(
                        project_id=project_uuid, name=name, version=int(version_str)
                    )
                return await repo.get(project_id=project_uuid, name=dataset_ref)
        except DatasetNotFoundError:
            return None

    async def _fail(
        self, pg: PostgresStore, project_uuid: uuid.UUID, exp_uuid: uuid.UUID, error: str
    ) -> None:
        try:
            async with tenant_session(pg, project_uuid) as session:
                repo = ExperimentRepository(session)
                await repo.transition(
                    project_id=project_uuid,
                    experiment_id=exp_uuid,
                    to="failed",
                    error=error,
                )
        except Exception as exc:
            logger.error(
                "failed to mark experiment as failed",
                extra={"error": str(exc)},
            )

    async def close(self) -> None:
        await self._queue.close()


async def run_eval_worker(llm: Any | None = None) -> None:
    """CLI 入口：ariadne-worker eval。"""
    settings = get_settings()
    configure_logging(settings.log_level)

    if llm is None:
        llm = build_llm_client(settings.llm)

    worker = EvalWorker(settings, llm=llm)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(worker.start())

    def _shutdown() -> None:
        logger.info("shutdown signal received")
        task_stop = asyncio.create_task(worker.stop())
        task_stop.add_done_callback(lambda _: None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown)

    try:
        await task
    finally:
        await worker.close()


__all__ = [
    "EvalWorker",
    "LLMGenerator",
    "NoScorersConfiguredError",
    "build_scorer_from_config",
    "run_eval_worker",
]
