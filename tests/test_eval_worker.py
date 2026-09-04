"""Eval Worker 测试。

三类 Worker 独立伸缩（M6 §5）—— 本文件测试第三类 Worker（eval）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest

from ariadne.eval_module.base import EvalContext
from ariadne.eval_module.deterministic import build_deterministic_evaluator
from ariadne.experiment.dataset import DatasetItem
from ariadne.loop_module.engine import LLMResponse
from ariadne.worker.eval_worker import (
    LLMGenerator,
    NoScorersConfiguredError,
    _AlwaysPassEvaluator,
    build_scorer_from_config,
)


class TestAlwaysPassEvaluator:
    def test_evaluate_returns_passed(self) -> None:
        ev = _AlwaysPassEvaluator()
        ctx = EvalContext(item_id="i1", input="test", output="response")
        result = ev.evaluate(ctx)
        assert result.passed is True
        assert result.value == 1.0

    def test_default_name_says_no_scorers_configured(self) -> None:
        """名字会随 evaluator_metrics 落库，必须自证"这轮没有质量评分"。

        叫 always_pass 时，metrics 里的 evaluators 键长得像一个正常评估器；
        读结果的人没有任何线索知道这个 100 分是兜底造出来的。
        """
        assert _AlwaysPassEvaluator().name == "no_scorers_configured"

    def test_evidence_disclaims_the_score(self) -> None:
        ev = _AlwaysPassEvaluator()
        ctx = EvalContext(item_id="i1", input="test", output="response")
        assert "不代表输出质量" in (ev.evaluate(ctx).evidence or "")

    def test_kind_is_deterministic(self) -> None:
        ev = _AlwaysPassEvaluator()
        assert ev.kind.value == "deterministic"


class TestBuildScorerFromConfig:
    def test_empty_config_raises(self) -> None:
        """没配评分器不能兜底成满分 —— 那会让门禁无条件放行。"""
        with pytest.raises(NoScorersConfiguredError, match="缺少 scorers"):
            build_scorer_from_config({})

    def test_empty_scorers_list_raises(self) -> None:
        with pytest.raises(NoScorersConfiguredError):
            build_scorer_from_config({"scorers": []})

    def test_allow_no_scorers_opts_in_explicitly(self) -> None:
        scorer = build_scorer_from_config({"allow_no_scorers": True})
        ctx = EvalContext(item_id="i1", input="test", output="response")
        result = scorer.evaluate(ctx)
        assert result.passed is True
        assert result.results[0].name == "no_scorers_configured"

    def test_with_regex_scorer(self) -> None:
        config = {
            "scorers": [
                {"type": "regex", "pattern": "hello", "weight": 1.0, "name": "has_hello"},
            ],
            "threshold": 50.0,
        }
        scorer = build_scorer_from_config(config)
        ctx = EvalContext(item_id="i1", input="test", output="hello world")
        result = scorer.evaluate(ctx)
        assert result.passed is True

    def test_with_exact_match(self) -> None:
        config = {
            "scorers": [
                {"type": "exact_match", "name": "match"},
            ],
        }
        scorer = build_scorer_from_config(config)
        ctx = EvalContext(item_id="i1", input="test", output="expected", expected="expected")
        result = scorer.evaluate(ctx)
        assert result.passed is True

    def test_unsupported_type_raises(self) -> None:
        config = {"scorers": [{"type": "nonexistent"}]}
        with pytest.raises(ValueError, match="不支持"):
            build_scorer_from_config(config)


class TestBuildDeterministicEvaluator:
    def test_exact_match(self) -> None:
        ev = build_deterministic_evaluator({"type": "exact_match", "name": "em"})
        assert ev.name == "em"

    def test_regex(self) -> None:
        ev = build_deterministic_evaluator({"type": "regex", "pattern": "\\d+", "name": "r"})
        assert ev.name == "r"

    def test_json_parsable(self) -> None:
        ev = build_deterministic_evaluator({"type": "json_parsable", "name": "jp"})
        assert ev.name == "jp"

    def test_required_fields(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "required_fields", "name": "rf", "fields": ["a", "b"]}
        )
        assert ev.name == "rf"

    def test_word_count(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "word_count", "name": "wc", "min_words": 10}
        )
        assert ev.name == "wc"

    def test_forbidden_terms(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "forbidden_terms", "name": "ft", "terms": ["bad"]}
        )
        assert ev.name == "ft"

    def test_citation_count(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "citation_count", "name": "cc", "min_count": 5}
        )
        assert ev.name == "cc"

    def test_markdown_structure(self) -> None:
        ev = build_deterministic_evaluator({"type": "markdown_structure", "name": "md"})
        assert ev.name == "md"

    def test_numeric_range(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "numeric_range", "name": "nr", "threshold": 0.5}
        )
        assert ev.name == "nr"


class TestLLMGenerator:
    def test_generate_success(self) -> None:
        from ariadne.loop_module.engine import LLMResponse

        mock_response = LLMResponse(
            output="hello",
            input_tokens=10,
            output_tokens=5,
            model="test-model",
            cost_usd=Decimal("0.01"),
        )
        mock_llm = MagicMock()
        mock_llm.complete = MagicMock(return_value=mock_response)
        # asyncio.run needs a coroutine

        async def _complete(*_args: Any, **_kw: Any) -> Any:
            return mock_response

        mock_llm.complete = _complete

        gen = LLMGenerator(mock_llm, "test-model")
        item = DatasetItem(item_id="i1", input="test input", expected="hello")
        result = gen.generate(item)
        assert result.text == "hello"
        assert result.cost_usd == Decimal("0.01")
        assert result.failed is False

    def test_generate_error(self) -> None:
        mock_llm = MagicMock()

        async def _fail(*_args: Any, **_kw: Any) -> Any:
            raise RuntimeError("LLM error")

        mock_llm.complete = _fail

        gen = LLMGenerator(mock_llm, "test-model")
        item = DatasetItem(item_id="i1", input="test input", expected="hello")
        result = gen.generate(item)
        assert result.text == ""
        assert result.failed is True
        assert "RuntimeError" in result.error

    async def test_generate_inside_running_loop(self) -> None:
        """在已有事件循环内调用 generate() 不得抛 asyncio.run 嵌套错误。

        EvalWorker._process 是 async 协程（通过 asyncio.gather 并发跑多个
        实验），而 ExperimentRunner / Generator 是同步接口 —— 旧实现直接
        asyncio.run 在已运行循环内必抛
        "asyncio.run() cannot be called from a running event loop"，
        生产路径上每条样本都生成失败、实验全挂。回归测试用真实 async 调用。
        """
        mock_response = LLMResponse(
            output="hello",
            input_tokens=1,
            output_tokens=1,
            model="test-model",
            cost_usd=Decimal("0.01"),
        )
        mock_llm = MagicMock()

        async def _complete(*_args: Any, **_kw: Any) -> Any:
            return mock_response

        mock_llm.complete = _complete

        gen = LLMGenerator(mock_llm, "test-model")
        item = DatasetItem(item_id="i1", input="test input", expected="hello")

        result = gen.generate(item)
        assert result.failed is False
        assert result.text == "hello"


class TestEvalQueueIntegration:
    """EvalQueue 与 LoopQueue 同构，只测关键差异。"""

    def test_stream_key_default(self) -> None:
        from ariadne.config import RedisSettings

        settings = RedisSettings(url="redis://localhost:6379/0")
        from ariadne.worker.eval_queue import EvalQueue

        q = EvalQueue(settings)
        assert q._stream_key == "q:eval"
        assert q._consumer_group == "eval-workers"

    def test_payload_field(self) -> None:
        from ariadne.worker.eval_queue import RECLAIM_MIN_IDLE_MS

        # 验证 reclaim 超时比 Loop 更长（评测任务更耗时）
        assert RECLAIM_MIN_IDLE_MS >= 120_000


class TestScorerConfigErrorIsTerminal:
    """配置错误必须转 failed + ACK，不能变成毒消息。

    不直接调 build_scorer_from_config —— 那只证明"会抛"。这里要证明 worker
    **捕获了它**：抛出去的话消息不 ACK，XAUTOCLAIM 反复回收，experiment 行
    永远停在 running，而队列里堆着一条永远失败的消息。
    """

    def test_worker_catches_all_three_config_error_types(self) -> None:
        """三类配置错误都是 ValueError 子类，被同一支 except 覆盖。"""
        import inspect

        from ariadne.eval_module.factory import JudgeNeedsClientError
        from ariadne.worker.eval_worker import EvalWorker, NoScorersConfiguredError

        assert issubclass(NoScorersConfiguredError, ValueError)
        assert issubclass(JudgeNeedsClientError, ValueError)

        source = inspect.getsource(EvalWorker._process)
        build_at = source.index("build_scorer_from_config(config)")
        # 构建评分器这一句必须在 try 里，且紧随其后的 except 捕 ValueError
        assert "try:" in source[:build_at]
        assert "except ValueError" in source[build_at:]
        # 捕获后要转 failed 并 return（return 才会让 _poll 走到 ACK）
        tail = source[build_at:]
        assert "self._fail(" in tail
        assert tail.index("self._fail(") < tail.index("return")

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"scorers": []},
            {"scorers": [{"type": "no_such_evaluator"}]},
            {"scorers": [{"type": "regex", "pattern": "x", "typo_key": 1}]},
            {"scorers": [{"type": "judge"}]},
        ],
    )
    def test_bad_configs_all_raise_value_error(self, config: dict[str, Any]) -> None:
        """worker 只捕 ValueError —— 任何漏网的异常类型都会变成毒消息。"""
        with pytest.raises(ValueError):
            build_scorer_from_config(config)


class _StubEvalQueue:
    """只喂固定几条消息的 EvalQueue 桩。"""

    def __init__(self, experiment_ids: list[str]) -> None:
        self._ids = experiment_ids
        self.acked: list[str] = []

    async def connect(self) -> None: ...
    async def ensure_group(self) -> None: ...
    async def close(self) -> None: ...

    async def claim(self, consumer: str) -> list[tuple[str, str, str]]:
        out = [(f"msg-{i}", i, "11111111-1111-1111-1111-111111111111") for i in self._ids]
        self._ids = []
        return out

    async def ack(self, message_id: str) -> None:
        self.acked.append(message_id)


class TestEvalPollConcurrency:
    """一轮 claim 拿到多个实验时必须并发处理。

    eval_queue 一次认领 _MAX_CLAIM=5 个，回收阈值 120s，而跑完一个数据集
    远超这个时间。串行时队尾实验纯等待回收期限到达；接管者读到
    status="running" **不会**跳过（_process 只跳 completed/failed），于是
    同一实验被两个 Worker 各跑一遍：双倍 LLM 成本，两份结果互相覆盖。

    断言执行是否重叠，而不是 Semaphore 是否存在 —— 后者在 gather 被改回
    串行 for 时照样通过。
    """

    async def test_claimed_experiments_run_concurrently(self) -> None:
        import asyncio

        from ariadne.worker.eval_worker import EvalWorker

        ids = ["e1", "e2", "e3"]
        queue = _StubEvalQueue(ids)
        worker = EvalWorker(
            settings=_eval_settings(eval_concurrency=3),
            queue=queue,  # type: ignore[arg-type]
        )

        in_flight = 0
        peak = 0

        async def fake_process(experiment_id: str, project_id_str: str) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.05)
            finally:
                in_flight -= 1

        worker._process = fake_process  # type: ignore[method-assign]
        await worker._poll()

        assert peak > 1, f"三个实验仍在串行执行（峰值并发 {peak}）"
        assert sorted(queue.acked) == [f"msg-{i}" for i in ids]

    async def test_respects_configured_limit(self) -> None:
        import asyncio

        from ariadne.worker.eval_worker import EvalWorker

        queue = _StubEvalQueue(["e1", "e2", "e3", "e4"])
        worker = EvalWorker(
            settings=_eval_settings(eval_concurrency=2),
            queue=queue,  # type: ignore[arg-type]
        )

        in_flight = 0
        peak = 0

        async def fake_process(experiment_id: str, project_id_str: str) -> None:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await asyncio.sleep(0.05)
            finally:
                in_flight -= 1

        worker._process = fake_process  # type: ignore[method-assign]
        await worker._poll()
        assert peak <= 2, f"并发超过配置上限 2（实测 {peak}）"

    async def test_one_failure_does_not_block_siblings(self) -> None:
        """单项抛异常时同批其他项仍要跑完并 ACK。"""
        from ariadne.worker.eval_worker import EvalWorker

        queue = _StubEvalQueue(["bad", "good"])
        worker = EvalWorker(
            settings=_eval_settings(eval_concurrency=2),
            queue=queue,  # type: ignore[arg-type]
        )

        async def fake_process(experiment_id: str, project_id_str: str) -> None:
            if experiment_id == "bad":
                raise RuntimeError("boom")

        worker._process = fake_process  # type: ignore[method-assign]
        await worker._poll()

        # 失败项不 ACK（留给回收重试），成功项必须 ACK
        assert queue.acked == ["msg-good"]
        assert worker._stats["failed"] == 1


def _eval_settings(*, eval_concurrency: int) -> Any:
    from pydantic import SecretStr

    from ariadne.config import (
        ApiSettings,
        ClickHouseSettings,
        RedisSettings,
        Settings,
        WorkerSettings,
    )

    return Settings(
        env="test",
        log_level="WARNING",
        clickhouse=ClickHouseSettings(host="localhost", database="ariadne_test"),
        redis=RedisSettings(url="redis://localhost:6379/15"),
        api=ApiSettings(static_api_key=SecretStr("ak_test_key")),
        worker=WorkerSettings(eval_concurrency=eval_concurrency),
    )
