"""脱敏测试。核心是 trace 内占位符一致性 —— 不一致会让脱敏后的数据无法排障。"""

from ariadne.telemetry.redaction import RedactionContext, detect_pii, redact


def test_same_entity_maps_to_same_placeholder() -> None:
    """同一实体在一条 trace 内必须映射到同一结果。"""
    ctx = RedactionContext(trace_id="t1")
    first = redact("联系 alice@corp.com 确认", ctx)
    second = redact("已通知 alice@corp.com", ctx)

    assert "alice@corp.com" not in first
    # 同一邮箱两次脱敏结果一致
    assert first.split("联系 ")[1].split(" 确认")[0] == second.split("已通知 ")[1]
    assert ctx.hit_count == 1


def test_different_entities_get_different_placeholders() -> None:
    ctx = RedactionContext(trace_id="t1")
    redact("alice@corp.com 和 bob@corp.com", ctx)
    assert ctx.hit_count == 2


def test_separate_traces_have_independent_mappings() -> None:
    """跨 trace 不共享映射：避免一条 trace 的实体数泄漏到另一条。"""
    ctx_a = RedactionContext(trace_id="a")
    ctx_b = RedactionContext(trace_id="b")
    redact("alice@corp.com", ctx_a)
    redact("bob@corp.com", ctx_b)
    assert ctx_a.hit_count == 1
    assert ctx_b.hit_count == 1


def test_api_key_fully_replaced_not_masked() -> None:
    """凭证必须完全替换，掩码保留结构对 key 来说仍是泄漏风险。"""
    ctx = RedactionContext(trace_id="t1")
    out = redact("使用 sk-proj-abcd1234efgh5678ijkl 调用", ctx)
    assert "sk-proj-abcd1234efgh5678ijkl" not in out
    assert "[REDACTED:api_key]" in out


def test_email_keeps_domain_for_debuggability() -> None:
    """邮箱保留域名：排障时需要知道是哪个组织的用户。"""
    ctx = RedactionContext(trace_id="t1")
    out = redact("alice@example.com", ctx)
    assert "@example.com" in out
    assert "alice" not in out


def test_phone_and_card_keep_tail() -> None:
    ctx = RedactionContext(trace_id="t1")
    out = redact("手机 13800138000 卡号 6222021234567890", ctx)
    assert "13800138000" not in out
    assert "8000" in out
    assert "6222021234567890" not in out


def test_api_key_takes_priority_over_digits() -> None:
    """api_key 里的数字不应被当成手机号/卡号切走。"""
    ctx = RedactionContext(trace_id="t1")
    out = redact("ak-13800138000abcdefgh", ctx)
    assert "[REDACTED:api_key]" in out
    assert "api_key" in ctx.hit_kinds()


def test_empty_and_clean_text_unchanged() -> None:
    ctx = RedactionContext(trace_id="t1")
    assert redact("", ctx) == ""
    assert redact("这段文本没有敏感信息", ctx) == "这段文本没有敏感信息"
    assert ctx.hit_count == 0


def test_detect_pii_does_not_mutate() -> None:
    """detect_pii 只报告不改写（供 Harness 规则使用）。"""
    kinds = detect_pii("alice@corp.com 13800138000")
    assert "email" in kinds
    assert "phone_cn" in kinds


def test_id_card_masked() -> None:
    ctx = RedactionContext(trace_id="t1")
    out = redact("身份证 11010119900307123X", ctx)
    assert "11010119900307123X" not in out
    assert "id_card_cn" in ctx.hit_kinds()
