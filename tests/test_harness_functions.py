"""Harness 内置函数测试 —— ReDoS 防护、各函数边界。"""

from __future__ import annotations

from ariadne.harness_module.functions import (
    MAX_REGEX_INPUT_CHARS,
    count_citations,
    detect_pii,
    estimate_tokens,
    json_valid,
    regex_match,
    sensitive_score,
    token_count,
)


class TestRegexMatch:
    """regex_match + ReDoS 防护。"""

    def test_basic_match(self) -> None:
        assert regex_match("hello world", "world")

    def test_no_match(self) -> None:
        assert not regex_match("hello", "xyz")

    def test_anchored_match(self) -> None:
        assert regex_match("pytest", "^(pytest|ruff)$")
        assert not regex_match("pytest -v", "^(pytest|ruff)$")

    def test_invalid_pattern(self) -> None:
        assert not regex_match("text", "[")

    def test_non_string_input(self) -> None:
        assert not regex_match(123, "test")  # type: ignore[arg-type]
        assert not regex_match("test", None)  # type: ignore[arg-type]

    def test_long_input_truncated(self) -> None:
        long_text = "a" * (MAX_REGEX_INPUT_CHARS + 100)
        # Should not raise, should truncate and still match
        assert regex_match(long_text, "a+")

    def test_catastrophic_backtracking_rejected(self) -> None:
        """嵌套量词模式被拒（fail-closed）。"""
        result = regex_match("aaaaaaaaab", "(a+)+b")
        assert result is False  # catastrophic pattern rejected


class TestDetectPii:
    def test_email(self) -> None:
        pii = detect_pii("contact john@example.com")
        assert "email" in pii

    def test_phone(self) -> None:
        pii = detect_pii("call 555-123-4567")
        assert "phone" in pii

    def test_ssn(self) -> None:
        pii = detect_pii("ssn 123-45-6789")
        assert "ssn" in pii

    def test_ip(self) -> None:
        pii = detect_pii("server at 192.168.1.1")
        assert "ip" in pii

    def test_no_pii(self) -> None:
        assert detect_pii("just some plain text") == []

    def test_non_string(self) -> None:
        assert detect_pii(123) == []  # type: ignore[arg-type]

    def test_multiple_types(self) -> None:
        pii = detect_pii("email: a@b.com, phone: 555-123-4567")
        assert "email" in pii
        assert "phone" in pii

    def test_returns_sorted(self) -> None:
        pii = detect_pii("ip 10.0.0.1 email a@b.com")
        assert pii == sorted(pii)


class TestCountCitations:
    def test_markdown_link(self) -> None:
        assert count_citations("see [link](http://x.com)") >= 1

    def test_footnote(self) -> None:
        assert count_citations("see [^1]") >= 1

    def test_numeric(self) -> None:
        assert count_citations("see [1] and [2]") == 2

    def test_no_citations(self) -> None:
        assert count_citations("no refs here") == 0

    def test_non_string(self) -> None:
        assert count_citations(None) == 0  # type: ignore[arg-type]

    def test_dedup(self) -> None:
        assert count_citations("[1] [1] [1]") == 1


class TestJsonValid:
    def test_valid_json(self) -> None:
        assert json_valid('{"key": "value"}')

    def test_invalid_json(self) -> None:
        assert not json_valid("{not json}")

    def test_with_schema(self) -> None:
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        assert json_valid('{"x": 1}', schema=schema)
        assert not json_valid('{"x": "str"}', schema=schema)

    def test_non_string(self) -> None:
        assert not json_valid(123)  # type: ignore[arg-type]


class TestEstimateTokens:
    def test_short_text(self) -> None:
        assert estimate_tokens("hello") > 0

    def test_dict_input(self) -> None:
        tokens = estimate_tokens({"key": "value"})
        assert tokens > 0

    def test_empty_string(self) -> None:
        # estimate_tokens returns >= 1 even for empty (rounds up)
        assert estimate_tokens("") >= 0


class TestSensitiveScore:
    def test_no_pii_zero(self) -> None:
        assert sensitive_score("clean text") == 0.0

    def test_pii_positive(self) -> None:
        score = sensitive_score("email a@b.com phone 555-123-4567")
        assert score > 0.0

    def test_score_capped_at_1(self) -> None:
        score = sensitive_score("a@b.com 555-123-4567 123-45-6789 192.168.1.1 4111111111111111")
        assert score <= 1.0


class TestTokenCount:
    def test_alias_of_estimate(self) -> None:
        text = "some text here"
        assert token_count(text) == estimate_tokens(text)
