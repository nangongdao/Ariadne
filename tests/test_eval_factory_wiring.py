"""评估器工厂的接线断言：注册表可达性 + 参数真的生效。

`tests/test_eval_worker.py::TestBuildDeterministicEvaluator` 的九个用例
全是 `assert ev.name == "..."` —— name 是工厂**唯一**总会传的参数，所以那
九条断言在旧 if-chain 静默丢弃 must_match / flags / case_sensitive /
allow_fenced 的情况下照样全绿。这正是记忆里"测试全绿但没真测到"的写法：
断言落在装配的副产物上，而不是落在被装配的行为上。

本文件因此只断言两件事：
  1. 注册表里的评估器**都能**从配置构造（旧 if-chain 短路了 4 个）；
  2. 配置里的每个参数**都影响判定结果**（旧 if-chain 丢弃了 7 个）。
"""

from __future__ import annotations

import pytest

from ariadne.eval_module import available_evaluators, evaluators_by_kind
from ariadne.eval_module.base import EvalContext, EvaluatorKind
from ariadne.eval_module.factory import (
    JudgeNeedsClientError,
    build_deterministic_evaluator,
    build_evaluator_from_config,
)

# 这些 type 需要注入 client 或额外依赖，不该出现在纯配置构造的覆盖里
_NEEDS_CLIENT = {"judge"}


def ctx(output: str, *, expected: str = "") -> EvalContext:
    return EvalContext(item_id="i1", input="q", output=output, expected=expected)


class TestRegistryReachability:
    """旧 if-chain 只有 10 个分支，注册表有 14 项 —— 4 个生产侧不可达。"""

    def test_every_registered_evaluator_is_constructible(self) -> None:
        """注册表新增项若忘了配参数，这条会立刻红。

        遍历 available_evaluators() 而不是写死列表：写死列表就是 if-chain
        的病根 —— 注册表长出第 15 项时没有任何东西会提醒我们。
        """
        unreachable: list[str] = []
        for etype in available_evaluators():
            if etype in _NEEDS_CLIENT:
                continue
            try:
                build_evaluator_from_config(_minimal_config(etype))
            except JudgeNeedsClientError:
                continue
            except Exception as exc:
                unreachable.append(f"{etype}: {type(exc).__name__}: {exc}")
        assert not unreachable, f"这些已注册评估器无法从配置构造: {unreachable}"

    @pytest.mark.parametrize("etype", ["rouge_l", "token_f1", "edit_distance"])
    def test_previously_unreachable_types_now_build(self, etype: str) -> None:
        """三个实现完整、单测全绿、但旧工厂没有分支的评估器。

        它们的 kind 是 statistical —— 旧 if-chain 的 10 个分支恰好是 10 个
        deterministic 类型，这不是巧合而是漏掉一整个 kind 的症状。
        """
        ev = build_deterministic_evaluator({"type": etype})
        assert ev.kind is EvaluatorKind.STATISTICAL
        assert ev.evaluate(ctx("hello world", expected="hello world")).passed

    def test_statistical_kind_is_config_buildable(self) -> None:
        """门槛是"零 API 成本"而非字面的 deterministic。"""
        for etype in evaluators_by_kind(EvaluatorKind.STATISTICAL):
            assert build_deterministic_evaluator({"type": etype}) is not None

    def test_judge_kind_is_rejected_by_deterministic_entry(self) -> None:
        """judge 每条样本打一次 LLM，混进来会让实验成本失控。"""
        # 交替是有意的：judge 需注入 client，这一步比 kind 检查更早触发
        with pytest.raises(ValueError, match=r"LLM 调用|需要注入 client"):
            build_deterministic_evaluator({"type": "judge"})

    def test_judge_reports_it_needs_a_client(self) -> None:
        """judge 在注册表里，但不能从纯配置构造 —— 报错要能区分这两种情况。"""
        with pytest.raises(JudgeNeedsClientError, match="需要注入 client"):
            build_evaluator_from_config({"type": "judge"})

    def test_unknown_type_lists_what_is_available(self) -> None:
        with pytest.raises(ValueError, match="不支持的评估器类型") as exc:
            build_evaluator_from_config({"type": "no_such_thing"})
        assert "regex" in str(exc.value)


def _minimal_config(etype: str) -> dict[str, object]:
    """各类型的最小可构造配置。"""
    required: dict[str, dict[str, object]] = {
        "regex": {"pattern": "x"},
        "required_fields": {"fields": ["a"]},
        "forbidden_terms": {"terms": ["bad"]},
        "json_schema": {"schema": {"type": "object"}},
    }
    return {"type": etype, **required.get(etype, {})}


class TestParametersActuallyApply:
    """旧 if-chain 把这些参数**静默丢弃** —— 配了不生效，且不报错。"""

    def test_regex_must_match_false_inverts_the_assertion(self) -> None:
        """最危险的一个：本意"不许出现 TODO"，旧代码变成"必须出现 TODO"。

        断言反向意味着门禁的判定和作者的意图**恰好相反**，而且两侧都不报错。
        """
        ev = build_deterministic_evaluator(
            {"type": "regex", "pattern": "TODO", "must_match": False}
        )
        assert ev.evaluate(ctx("all done")).passed is True
        assert ev.evaluate(ctx("TODO: fix later")).passed is False

    def test_regex_flags_apply(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "regex", "pattern": "hello", "flags": ["IGNORECASE"]}
        )
        assert ev.evaluate(ctx("HELLO")).passed is True

    def test_exact_match_case_sensitive_applies(self) -> None:
        strict = build_deterministic_evaluator(
            {"type": "exact_match", "case_sensitive": True}
        )
        assert strict.evaluate(ctx("Yes", expected="yes")).passed is False
        loose = build_deterministic_evaluator(
            {"type": "exact_match", "case_sensitive": False}
        )
        assert loose.evaluate(ctx("Yes", expected="yes")).passed is True

    def test_json_schema_allow_fenced_applies(self) -> None:
        fenced = '```json\n{"a": 1}\n```'
        config: dict[str, object] = {
            "type": "json_schema",
            "schema": {"type": "object", "required": ["a"]},
        }
        assert build_deterministic_evaluator(
            {**config, "allow_fenced": True}
        ).evaluate(ctx(fenced)).passed is True
        assert build_deterministic_evaluator(
            {**config, "allow_fenced": False}
        ).evaluate(ctx(fenced)).passed is False

    def test_markdown_structure_params_apply(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "markdown_structure", "min_headings": 3}
        )
        assert ev.evaluate(ctx("# only one")).passed is False
        assert ev.evaluate(ctx("# a\n## b\n### c")).passed is True

    def test_word_count_bounds_apply(self) -> None:
        ev = build_deterministic_evaluator(
            {"type": "word_count", "min_words": 2, "max_words": 3}
        )
        assert ev.evaluate(ctx("one")).passed is False
        assert ev.evaluate(ctx("one two")).passed is True
        assert ev.evaluate(ctx("one two three four")).passed is False

    def test_citation_count_legacy_alias_still_works(self) -> None:
        """旧配置用 min_citations，构造器参数叫 min_count。"""
        ev = build_deterministic_evaluator(
            {"type": "citation_count", "min_citations": 2}
        )
        assert ev.evaluate(ctx("see [1]")).passed is False
        assert ev.evaluate(ctx("see [1] and [2]")).passed is True

    def test_numeric_string_from_json_is_coerced(self) -> None:
        """配置来自 JSON，数字可能是字符串。"""
        ev = build_deterministic_evaluator({"type": "word_count", "min_words": "2"})
        assert ev.evaluate(ctx("one")).passed is False
        assert ev.evaluate(ctx("one two")).passed is True


class TestUnknownKeysFailFast:
    """拼错的参数名必须报错，不能忽略 —— 忽略就是"配了不生效"的成因。"""

    def test_typo_in_parameter_name_raises(self) -> None:
        with pytest.raises(ValueError, match="不接受参数 'must_matches'"):
            build_deterministic_evaluator(
                {"type": "regex", "pattern": "x", "must_matches": False}
            )

    def test_error_lists_accepted_parameters(self) -> None:
        with pytest.raises(ValueError, match="min_words") as exc:
            build_deterministic_evaluator({"type": "word_count", "min_word": 3})
        assert "可用" in str(exc.value)

    def test_score_spec_keys_are_not_passed_to_constructor(self) -> None:
        """weight / normalize_max 属于 ScoreSpec，评估器构造器不认识它们。"""
        ev = build_deterministic_evaluator(
            {"type": "json_parsable", "weight": 2.0, "normalize_max": 10.0}
        )
        assert ev.evaluate(ctx('{"a": 1}')).passed is True
