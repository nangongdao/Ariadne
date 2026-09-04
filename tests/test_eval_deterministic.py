"""确定性评估器测试。

这类评估器会被 M3 的 Loop 每轮调用，因此重点验证两件事：
1. 结果二值且无歧义（可信度 ★★★★★ 的前提）
2. evaluate() 绝不外抛（契约要求）
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from ariadne.eval_module import EvaluatorFactory, available_evaluators
from ariadne.eval_module.base import EvalContext, EvalResult, EvaluatorKind
from ariadne.eval_module.deterministic import count_citations, count_words


def ctx(output: str = "", *, expected: str | None = None, item_id: str = "i1") -> EvalContext:
    return EvalContext(item_id=item_id, output=output, expected=expected)


class TestRegistry:
    def test_all_registered(self) -> None:
        names = available_evaluators()
        assert "regex" in names
        assert "json_schema" in names
        assert "word_count" in names
        assert "rouge_l" in names

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="未知的评估器"):
            EvaluatorFactory("nope")

    def test_direct_import_does_not_break_registry(self) -> None:
        """M1 踩过的坑：直接 import 子模块让"字典非空即已加载"提前返回。"""
        from ariadne.eval_module import available_evaluators as fresh
        from ariadne.eval_module.deterministic import regex  # noqa: F401

        assert "rouge_l" in fresh(), "统计类评估器漏注册"


class TestRegex:
    def test_must_match(self) -> None:
        ev = EvaluatorFactory("regex", pattern=r"^#\s", must_match=True)
        assert ev.evaluate(ctx("# 标题\n正文")).passed
        assert not ev.evaluate(ctx("正文没标题")).passed

    def test_must_not_match(self) -> None:
        ev = EvaluatorFactory("regex", pattern=r"TODO", must_match=False)
        assert ev.evaluate(ctx("完整内容")).passed
        result = ev.evaluate(ctx("这里有 TODO 待办"))
        assert not result.passed
        assert "TODO" in result.evidence


class TestCitations:
    @pytest.mark.parametrize(
        ("text", "expected_total"),
        [
            ("[链接](https://a.com) [另一个](https://b.com)", 2),
            ("参见[^1] 和[^2]", 2),
            ("引用 [1] 与 [2] 及 [3]", 3),
            ("裸链接 https://a.com 和 https://b.com", 2),
            ("没有任何引用", 0),
        ],
    )
    def test_counts_all_forms(self, text: str, expected_total: int) -> None:
        assert sum(count_citations(text).values()) == expected_total

    def test_same_source_counted_once(self) -> None:
        """"至少 3 个来源"的语义是 3 个不同来源，同一来源引用多次不算。"""
        text = "[a](https://x.com) 又见 [a](https://x.com) 再见 [a](https://x.com)"
        assert sum(count_citations(text).values()) == 1

    def test_markdown_link_not_double_counted_as_bare_url(self) -> None:
        """Markdown 链接里的 URL 不应同时被算作裸链接。"""
        counts = count_citations("[标题](https://a.com)")
        assert counts["markdown_link"] == 1
        assert counts["bare_url"] == 0

    def test_threshold(self) -> None:
        ev = EvaluatorFactory("citation_count", min_count=3)
        assert not ev.evaluate(ctx("[a](https://a.com)")).passed
        text = "[a](https://a.com) [b](https://b.com) [c](https://c.com)"
        result = ev.evaluate(ctx(text))
        assert result.passed
        assert result.value == 3.0


class TestMarkdownStructure:
    def test_requires_h1(self) -> None:
        ev = EvaluatorFactory("markdown_structure", require_h1=True)
        assert ev.evaluate(ctx("# 标题\n内容")).passed
        result = ev.evaluate(ctx("## 二级标题\n内容"))
        assert not result.passed
        assert "一级标题" in result.evidence

    def test_detects_unclosed_fence(self) -> None:
        ev = EvaluatorFactory("markdown_structure", require_h1=False)
        result = ev.evaluate(ctx("# T\n```python\ncode here"))
        assert not result.passed
        assert "未闭合" in result.evidence

    def test_detects_missing_fence_language(self) -> None:
        ev = EvaluatorFactory("markdown_structure", require_h1=False)
        result = ev.evaluate(ctx("# T\n```\ncode\n```"))
        assert not result.passed
        assert "语言标注" in result.evidence

    def test_valid_document_passes(self) -> None:
        ev = EvaluatorFactory("markdown_structure")
        doc = "# 标题\n\n正文\n\n```python\nprint(1)\n```\n"
        assert ev.evaluate(ctx(doc)).passed


class TestJsonSchema:
    SCHEMA: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
        "required": ["name", "age"],
    }

    def test_valid(self) -> None:
        ev = EvaluatorFactory("json_schema", schema=self.SCHEMA)
        assert ev.evaluate(ctx('{"name":"a","age":3}')).passed

    def test_fenced_json_tolerated(self) -> None:
        """模型常把 JSON 包在围栏里 —— 格式没完全对但意图明确，应容忍。"""
        ev = EvaluatorFactory("json_schema", schema=self.SCHEMA)
        fenced = '```json\n{"name":"a","age":3}\n```'
        assert ev.evaluate(ctx(fenced)).passed

    def test_missing_field_reported_with_path(self) -> None:
        ev = EvaluatorFactory("json_schema", schema=self.SCHEMA)
        result = ev.evaluate(ctx('{"name":"a"}'))
        assert not result.passed
        assert "age" in result.evidence

    def test_parse_error_distinguished(self) -> None:
        ev = EvaluatorFactory("json_schema", schema=self.SCHEMA)
        result = ev.evaluate(ctx("{not json"))
        assert not result.passed
        assert "解析失败" in result.evidence

    def test_bad_schema_rejected_at_construction(self) -> None:
        """配置写错应在构造时暴露，而非每次评测才发现。"""
        with pytest.raises(Exception, match=r"(?i)schema"):
            EvaluatorFactory("json_schema", schema={"type": "not-a-type"})

    def test_required_fields_dotted_path(self) -> None:
        ev = EvaluatorFactory("required_fields", fields=("user.name", "items.0.id"))
        good = '{"user":{"name":"a"},"items":[{"id":1}]}'
        assert ev.evaluate(ctx(good)).passed
        result = ev.evaluate(ctx('{"user":{},"items":[]}'))
        assert not result.passed
        assert "user.name" in result.evidence


class TestWordCount:
    @pytest.mark.parametrize(
        ("text", "count"),
        [
            ("hello world", 2),
            ("中文五个字啊", 6),
            ("混排 hello 世界", 5),  # 3 CJK + 1 word... 实际: 混排(2)+hello(1)+世界(2)=5
            ("", 0),
        ],
    )
    def test_mixed_script_counting(self, text: str, count: int) -> None:
        assert count_words(text) == count

    def test_range_check(self) -> None:
        ev = EvaluatorFactory("word_count", min_words=3, max_words=5)
        assert ev.evaluate(ctx("a b c d")).passed
        assert not ev.evaluate(ctx("a b")).passed
        assert not ev.evaluate(ctx("a b c d e f")).passed

    def test_evidence_says_which_side(self) -> None:
        ev = EvaluatorFactory("word_count", min_words=10)
        assert "下限" in ev.evaluate(ctx("short")).evidence
        ev2 = EvaluatorFactory("word_count", max_words=2)
        assert "上限" in ev2.evaluate(ctx("a b c")).evidence


class TestForbiddenTerms:
    def test_whole_word_matching(self) -> None:
        """"AI" 不应命中 "SAID"。"""
        ev = EvaluatorFactory("forbidden_terms", terms=("AI",))
        assert ev.evaluate(ctx("he SAID something")).passed
        assert not ev.evaluate(ctx("using AI here")).passed

    def test_cjk_substring_matching(self) -> None:
        """中文无词边界，退化为子串匹配。"""
        ev = EvaluatorFactory("forbidden_terms", terms=("保证收益",))
        assert not ev.evaluate(ctx("我们保证收益翻倍")).passed

    def test_case_insensitive_by_default(self) -> None:
        ev = EvaluatorFactory("forbidden_terms", terms=("Todo",))
        assert not ev.evaluate(ctx("TODO left here")).passed


class TestExactMatch:
    def test_normalizes_whitespace_and_case(self) -> None:
        ev = EvaluatorFactory("exact_match")
        assert ev.evaluate(ctx("  Hello   World ", expected="hello world")).passed

    def test_missing_expected_flags_errored(self) -> None:
        """缺 expected 是配置问题，要与"输出不合格"区分开。"""
        ev = EvaluatorFactory("exact_match")
        result = ev.evaluate(ctx("anything"))
        assert not result.passed
        assert result.errored


class TestContract:
    def test_evaluate_never_raises(self) -> None:
        """契约：单个评估器失败不能中断实验。"""

        from ariadne.eval_module.base import BaseEvaluator

        class Exploding(BaseEvaluator):
            kind = EvaluatorKind.DETERMINISTIC

            def _evaluate(self, ctx: EvalContext) -> EvalResult:
                raise RuntimeError("boom")

        result = Exploding("boom").evaluate(ctx("x"))
        assert not result.passed
        assert result.errored
        assert "RuntimeError" in result.evidence

    def test_duration_always_recorded(self) -> None:
        ev = EvaluatorFactory("word_count", min_words=1)
        assert ev.evaluate(ctx("hello")).duration_ms >= 0
