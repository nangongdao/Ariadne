"""Judge 可信度工程测试。

这是 M2 里最关键的测试文件。Judge 分数会被 M3 用作收敛断言的依据，
因此六条措施每一条都要有对应验证 —— 任一失效都会让 Loop 用不可靠
信号驱动迭代。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from ariadne.eval_module.base import EvalContext
from ariadne.eval_module.judge import (
    JudgeConfig,
    JudgeEvaluator,
    JudgeIsolationError,
    JudgeResponse,
    KappaGateError,
    KappaVerdict,
    PairwiseJudge,
    Winner,
    aggregate_votes,
    available_dimensions,
    bucketize,
    enforce_gate,
    enforce_isolation,
    parse_judge_output,
    system_prompt,
    weighted_kappa,
)


@dataclass
class StubClient:
    """按调用顺序返回预设响应，并记录收到的参数。"""

    responses: list[str]
    calls: list[dict[str, Any]] = field(default_factory=list)
    model_name: str = "claude-sonnet-5"

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        model: str,
        temperature: float,
        seed: int | None,
    ) -> JudgeResponse:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": schema,
                "model": model,
                "temperature": temperature,
                "seed": seed,
            }
        )
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return JudgeResponse(
            content=self.responses[index],
            model=self.model_name,
            cost_usd=Decimal("0.001"),
        )


def judge_json(score: int, *, violations: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "reasoning": "分析过程",
            "score": score,
            "violations": violations or [],
        }
    )


def ctx(output: str = "输出内容", *, task: str = "写一段说明") -> EvalContext:
    return EvalContext(item_id="i1", input=task, output=output)


class TestMeasure1Determinism:
    """措施 1：版本锁定 + temperature=0 + seed。"""

    def test_defaults_are_deterministic(self) -> None:
        config = JudgeConfig(model="claude-sonnet-5", dimension="factuality")
        assert config.temperature == 0.0
        assert config.seed == 42

    def test_config_passed_to_client(self) -> None:
        client = StubClient([judge_json(90)])
        evaluator = JudgeEvaluator(
            client=client,
            config=JudgeConfig(
                model="claude-sonnet-5", dimension="factuality", seed=7
            ),
        )
        evaluator.evaluate(ctx())
        call = client.calls[0]
        assert call["temperature"] == 0.0
        assert call["seed"] == 7
        assert call["model"] == "claude-sonnet-5"

    def test_judge_model_recorded(self) -> None:
        """版本切换视为破坏性变更，因此必须记录实际使用的模型。"""
        client = StubClient([judge_json(90)])
        client.model_name = "claude-sonnet-5-20260101"
        evaluator = JudgeEvaluator(
            client=client,
            config=JudgeConfig(model="claude-sonnet-5", dimension="factuality"),
        )
        result = evaluator.evaluate(ctx())
        assert result.judge_model == "claude-sonnet-5-20260101"


class TestMeasure2StructuredOutput:
    """措施 2：结构化输出，不解析自由文本。"""

    def test_schema_passed_to_client(self) -> None:
        client = StubClient([judge_json(90)])
        JudgeEvaluator(
            client=client,
            config=JudgeConfig(model="claude-sonnet-5", dimension="ifr"),
        ).evaluate(ctx())
        schema = client.calls[0]["schema"]
        assert schema["required"] == ["reasoning", "score", "violations"]

    def test_violations_carry_span(self) -> None:
        """span 定位是关键 —— 只给总分的 Judge 对 Loop 毫无帮助。"""
        payload = judge_json(
            70,
            violations=[
                {"span": "第3段第2句", "severity": "medium", "detail": "无来源断言"}
            ],
        )
        client = StubClient([payload])
        result = JudgeEvaluator(
            client=client,
            config=JudgeConfig(model="claude-sonnet-5", dimension="factuality"),
        ).evaluate(ctx())

        assert len(result.violations) == 1
        assert result.violations[0].span == "第3段第2句"
        # 维度由配置决定，不让模型自己编
        assert result.violations[0].dimension == "factuality"

    def test_fenced_json_tolerated(self) -> None:
        client = StubClient([f"```json\n{judge_json(88)}\n```"])
        result = JudgeEvaluator(
            client=client,
            config=JudgeConfig(model="claude-sonnet-5", dimension="ifr"),
        ).evaluate(ctx())
        assert result.value == 88.0

    def test_parse_failure_flags_errored_not_zero(self) -> None:
        """解析失败必须记 errored 而非当 0 分 ——
        "没测出来"与"很差"是不同的，后者会让 Loop 朝错方向修。"""
        client = StubClient(["这不是 JSON"])
        result = JudgeEvaluator(
            client=client,
            config=JudgeConfig(model="claude-sonnet-5", dimension="ifr"),
        ).evaluate(ctx())
        assert result.errored
        assert not result.passed

    @pytest.mark.parametrize(
        "bad",
        ['{"reasoning":"x"}', '{"score":"high"}', "[]", '{"score":null}'],
    )
    def test_malformed_payloads_report_error(self, bad: str) -> None:
        _, _, _, error = parse_judge_output(bad)
        assert error

    def test_score_clamped(self) -> None:
        score, _, _, error = parse_judge_output(json.dumps({
            "reasoning": "", "score": 150, "violations": []
        }))
        assert not error
        assert score == 100.0


class TestMeasure3PairwiseVoting:
    """措施 3：双向投票消除位置偏差。"""

    @staticmethod
    def vote(winner: str) -> str:
        return json.dumps({"reasoning": "r", "winner": winner})

    def test_consistent_votes_accepted(self) -> None:
        """forward 选 A、reverse（交换后）选 B → 翻译回来都是 A，一致。"""
        client = StubClient([self.vote("A"), self.vote("B")])
        verdict = PairwiseJudge(client=client, model="claude-sonnet-5").compare(
            task="t", output_a="a", output_b="b"
        )
        assert verdict.consistent
        assert verdict.winner is Winner.A

    def test_inconsistent_votes_become_tie(self) -> None:
        """两次都选"显示在前的"→ 位置偏差，结论不可信，强制 tie。"""
        client = StubClient([self.vote("A"), self.vote("A")])
        verdict = PairwiseJudge(client=client, model="claude-sonnet-5").compare(
            task="t", output_a="a", output_b="b"
        )
        assert not verdict.consistent
        assert verdict.winner is Winner.TIE
        assert verdict.position_bias_detected

    def test_two_calls_made(self) -> None:
        """双向投票必须真的调两次，且第二次顺序相反。"""
        client = StubClient([self.vote("A"), self.vote("B")])
        PairwiseJudge(client=client, model="claude-sonnet-5").compare(
            task="t", output_a="AAA", output_b="BBB"
        )
        assert len(client.calls) == 2
        first, second = client.calls[0]["user"], client.calls[1]["user"]
        assert first.index("AAA") < first.index("BBB")
        assert second.index("BBB") < second.index("AAA")

    def test_tie_on_both_sides(self) -> None:
        client = StubClient([self.vote("tie"), self.vote("tie")])
        verdict = PairwiseJudge(client=client, model="claude-sonnet-5").compare(
            task="t", output_a="a", output_b="b"
        )
        assert verdict.consistent
        assert verdict.winner is Winner.TIE

    def test_malformed_vote_yields_tie_with_error(self) -> None:
        client = StubClient(["not json", "not json"])
        verdict = PairwiseJudge(client=client, model="claude-sonnet-5").compare(
            task="t", output_a="a", output_b="b"
        )
        assert verdict.winner is Winner.TIE
        assert verdict.error

    def test_aggregate_reports_inconsistency_rate(self) -> None:
        """inconsistency_rate 偏高 → 该 Judge 在此任务上不可靠。"""
        client = StubClient([self.vote("A"), self.vote("A")])
        judge = PairwiseJudge(client=client, model="claude-sonnet-5")
        verdicts = [
            judge.compare(task="t", output_a="a", output_b="b") for _ in range(4)
        ]
        stats = aggregate_votes(verdicts)
        assert stats["inconsistency_rate"] == 1.0
        assert stats["ties"] == 4

    def test_aggregate_empty(self) -> None:
        assert aggregate_votes([])["total"] == 0


class TestMeasure4Kappa:
    """措施 4：与人工标注对齐。"""

    def test_perfect_agreement(self) -> None:
        labels = [0, 1, 2] * 40
        report = weighted_kappa(labels, labels)
        assert report.kappa == 1.0
        assert report.verdict is KappaVerdict.HIGH

    def test_weighted_penalizes_near_miss_less(self) -> None:
        """把"好"判成"中"应比判成"差"扣得少 —— 这是用加权 kappa 的理由。"""
        human = [2] * 50 + [0] * 50
        near_miss = [1] * 50 + [0] * 50
        far_miss = [0] * 50 + [0] * 50

        near = weighted_kappa(human, near_miss, weighting="linear")
        far = weighted_kappa(human, far_miss, weighting="linear")
        assert near.kappa > far.kappa

    def test_single_category_is_unusable_not_perfect(self) -> None:
        """全判同一档说明没有区分能力，不该视为完美一致。"""
        report = weighted_kappa([1] * 100, [1] * 100)
        assert report.kappa == 0.0
        assert report.verdict is KappaVerdict.UNUSABLE

    def test_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="长度不匹配"):
            weighted_kappa([1, 2], [1])

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="样本为空"):
            weighted_kappa([], [])

    def test_small_sample_flagged_underpowered(self) -> None:
        """κ 在小样本上极不稳定，必须标记。"""
        report = weighted_kappa([0, 1] * 15, [0, 1] * 15)
        assert report.underpowered
        assert not report.can_block

    def test_large_sample_can_block(self) -> None:
        labels = [0, 1, 2] * 40
        report = weighted_kappa(labels, labels)
        assert not report.underpowered
        assert report.can_block

    def test_bucketize_uses_quality_thresholds(self) -> None:
        assert bucketize([50.0, 70.0, 90.0]) == [0, 1, 2]

    def test_gate_blocks_low_kappa(self) -> None:
        human = [0, 1, 2] * 40
        judge = [2, 0, 1] * 40  # 系统性错位
        report = weighted_kappa(human, judge)
        with pytest.raises(KappaGateError, match="不达标"):
            enforce_gate(report, evaluator_name="factuality", blocking=True)

    def test_gate_allows_non_blocking(self) -> None:
        """κ 低时仍可作参考指标，只是不能用于收敛判定。"""
        report = weighted_kappa([0, 1] * 15, [1, 0] * 15)
        enforce_gate(report, evaluator_name="x", blocking=False)

    def test_gate_blocks_underpowered_even_with_high_kappa(self) -> None:
        """30 个样本上的 κ=1.0 不能说明什么。"""
        labels = [0, 1, 2] * 10
        report = weighted_kappa(labels, labels)
        assert report.kappa == 1.0
        with pytest.raises(KappaGateError, match="样本量"):
            enforce_gate(report, evaluator_name="x", blocking=True)


class TestMeasure6Isolation:
    """措施 6：生成模型与 Judge 模型强制隔离。"""

    def test_identical_model_rejected(self) -> None:
        with pytest.raises(JudgeIsolationError, match="相同"):
            enforce_isolation("gpt-4o", "gpt-4o")

    def test_same_family_rejected(self) -> None:
        """同族不同尺寸也算违规 —— 共享训练数据与偏好。"""
        with pytest.raises(JudgeIsolationError, match="同族"):
            enforce_isolation("gpt-4o-mini", "gpt-4o")

    def test_different_family_allowed(self) -> None:
        enforce_isolation("claude-sonnet-5", "gpt-4o")

    def test_no_generation_model_skips_check(self) -> None:
        enforce_isolation("gpt-4o", "")

    def test_enforced_at_construction(self) -> None:
        """构造时就失败，不等评测才发现。"""
        with pytest.raises(JudgeIsolationError):
            JudgeEvaluator(
                client=StubClient([judge_json(90)]),
                config=JudgeConfig(
                    model="gpt-4o", dimension="ifr", generation_model="gpt-4o"
                ),
            )


class TestPrompts:
    def test_unknown_dimension_rejected(self) -> None:
        """未知维度显式报错，不静默退化为通用评分。"""
        with pytest.raises(ValueError, match="未知的评测维度"):
            system_prompt("vibes")

    def test_all_dimensions_have_criteria(self) -> None:
        for dimension in available_dimensions():
            prompt = system_prompt(dimension)
            assert "评判维度" in prompt
            assert "90-100" in prompt  # 评分标准必须在

    def test_prompt_demands_reasoning_before_score(self) -> None:
        """反过来会让模型先拍分再编理由。"""
        assert "先在 reasoning 中分析" in system_prompt("factuality")

    def test_prompt_warns_against_verbosity_bias(self) -> None:
        assert "文采" in system_prompt("factuality")


class TestThreshold:
    def test_below_threshold_fails(self) -> None:
        client = StubClient([judge_json(80)])
        result = JudgeEvaluator(
            client=client,
            config=JudgeConfig(
                model="claude-sonnet-5", dimension="ifr", threshold=85.0
            ),
        ).evaluate(ctx())
        assert not result.passed
        assert "80" in result.evidence

    def test_at_threshold_passes(self) -> None:
        client = StubClient([judge_json(85)])
        result = JudgeEvaluator(
            client=client,
            config=JudgeConfig(
                model="claude-sonnet-5", dimension="ifr", threshold=85.0
            ),
        ).evaluate(ctx())
        assert result.passed

    def test_cost_recorded(self) -> None:
        client = StubClient([judge_json(90)])
        result = JudgeEvaluator(
            client=client,
            config=JudgeConfig(model="claude-sonnet-5", dimension="ifr"),
        ).evaluate(ctx())
        assert result.cost_usd == Decimal("0.001")
