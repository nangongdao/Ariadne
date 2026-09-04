"""SDK 自动埋点测试 —— 四条主链路的版本兼容矩阵测试。

R8 风险应对：验证 wrap_openai / wrap_anthropic 在不同 SDK 版本响应格式下
都能正确提取 usage、生成 span、不拖垮业务。

不依赖真实 openai/anthropic SDK：用 FakeClient + FakeResponse 模拟
SDK 对象结构（属性访问 + 字典访问两种形式），覆盖版本兼容性。
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ariadne_sdk.instrument.anthropic import wrap_anthropic
from ariadne_sdk.instrument.common import (
    extract_anthropic_usage,
    extract_openai_usage,
    preview_messages,
    preview_output,
)
from ariadne_sdk.instrument.openai import wrap_openai
from ariadne_sdk.span import Span

# ---------- 伪造客户端与响应对象 ----------


class FakeSpan(Span):
    """记录所有 set_* 调用的 Span，用于断言。"""

    def __init__(self, name: str = "", **kwargs: Any) -> None:
        super().__init__(name=name, **kwargs)
        self.ended = False

    def __enter__(self) -> FakeSpan:
        super().__enter__()
        return self

    def __exit__(self, *args: Any) -> None:  # type: ignore[override]
        super().__exit__(*args)  # type: ignore[arg-type]
        self.ended = True


class FakeClient:
    """伪造 Ariadne 客户端，span() 返回可断言的 FakeSpan。

    enabled=False 确保 Span 不尝试上报（无 exporter）。
    """

    def __init__(self) -> None:
        self.spans: list[FakeSpan] = []

    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        operation: str = "",
        provider: str = "",
        model: str = "",
        attributes: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> FakeSpan:
        s = FakeSpan(
            name=name,
            kind=kind,
            operation=operation,
            provider=provider,
            model=model,
            tags=list(tags or []),
        )
        if attributes:
            s.set_attributes(attributes)
        self.spans.append(s)
        return s

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def shutdown(self) -> None:
        pass


class FakeResponse(dict):
    """同时支持字典访问和属性访问的响应对象（模拟 SDK 返回值）。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class FakeMessage(dict):
    """同 FakeResponse，用于 message 对象。"""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


class _Completions:
    """模拟 openai.chat.completions 子对象。"""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.create: Any = lambda *a, **kw: response


class _Chat:
    def __init__(self, response: Any) -> None:
        self.completions = _Completions(response)


class _Embeddings:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.create: Any = lambda *a, **kw: response


class FakeOpenAIClient:
    """模拟 openai.OpenAI 客户端的属性结构（用普通类，非 MagicMock）。"""

    def __init__(self, response: Any) -> None:
        self.chat = _Chat(response)
        self.embeddings = _Embeddings(response)


class _Messages:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.create: Any = lambda *a, **kw: response


class FakeAnthropicClient:
    """模拟 anthropic.Anthropic 客户端的属性结构。"""

    def __init__(self, response: Any) -> None:
        self.messages = _Messages(response)


class FakeObj:
    """用 __slots__ 属性模拟真实 SDK 返回的对象（不是 dict）。"""

    __slots__ = ("_attrs",)

    def __init__(self, **kwargs: Any) -> None:
        object.__setattr__(self, "_attrs", kwargs)

    def __getattr__(self, name: str) -> Any:
        attrs = object.__getattribute__(self, "_attrs")
        if name in attrs:
            return attrs[name]
        raise AttributeError(name)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def patched_client(fake_client: FakeClient):
    """patch get_client 返回 FakeClient。"""
    with patch("ariadne_sdk.instrument.openai.get_client", return_value=fake_client), \
         patch("ariadne_sdk.instrument.anthropic.get_client", return_value=fake_client):
        yield fake_client


# ======================================================================
# extract_openai_usage
# ======================================================================


class TestExtractOpenAIUsage:
    """OpenAI usage 提取 —— 兼容字典和对象两种响应格式。"""

    def test_basic_usage(self) -> None:
        resp = FakeResponse(usage=FakeResponse(prompt_tokens=100, completion_tokens=50))
        usage = extract_openai_usage(resp)
        assert usage["input_tokens"] == 100
        assert usage["output_tokens"] == 50
        assert usage["cache_read_tokens"] == 0
        assert usage["reasoning_tokens"] == 0

    def test_dict_response(self) -> None:
        """SDK 返回纯 dict 也要兼容。"""
        resp = {"usage": {"prompt_tokens": 200, "completion_tokens": 80}}
        usage = extract_openai_usage(resp)
        assert usage["input_tokens"] == 200
        assert usage["output_tokens"] == 80

    def test_cached_tokens_subtracted(self) -> None:
        """cached_tokens 已包含在 prompt_tokens 中，必须减掉。"""
        resp = FakeResponse(
            usage=FakeResponse(
                prompt_tokens=1000,
                completion_tokens=200,
                prompt_tokens_details=FakeResponse(cached_tokens=300),
            )
        )
        usage = extract_openai_usage(resp)
        assert usage["input_tokens"] == 700  # 1000 - 300
        assert usage["cache_read_tokens"] == 300

    def test_reasoning_tokens(self) -> None:
        """o1/o3 模型的 reasoning_tokens。"""
        resp = FakeResponse(
            usage=FakeResponse(
                prompt_tokens=100,
                completion_tokens=50,
                completion_tokens_details=FakeResponse(reasoning_tokens=500),
            )
        )
        usage = extract_openai_usage(resp)
        assert usage["reasoning_tokens"] == 500

    def test_no_usage_returns_empty(self) -> None:
        resp = FakeResponse()
        assert extract_openai_usage(resp) == {}

    def test_none_response(self) -> None:
        assert extract_openai_usage(None) == {}

    def test_input_tokens_alias(self) -> None:
        """某些版本用 input_tokens 而非 prompt_tokens。"""
        resp = {"usage": {"input_tokens": 300, "output_tokens": 100}}
        usage = extract_openai_usage(resp)
        assert usage["input_tokens"] == 300


# ======================================================================
# extract_anthropic_usage
# ======================================================================


class TestExtractAnthropicUsage:
    """Anthropic usage 提取。"""

    def test_basic_usage(self) -> None:
        resp = FakeResponse(
            usage=FakeResponse(input_tokens=500, output_tokens=200)
        )
        usage = extract_anthropic_usage(resp)
        assert usage["input_tokens"] == 500
        assert usage["output_tokens"] == 200
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_write_tokens"] == 0

    def test_cache_tokens(self) -> None:
        """Anthropic cache_read_input_tokens 独立于 input_tokens（不减）。"""
        resp = FakeResponse(
            usage=FakeResponse(
                input_tokens=1000,
                output_tokens=300,
                cache_read_input_tokens=500,
                cache_creation_input_tokens=200,
            )
        )
        usage = extract_anthropic_usage(resp)
        assert usage["input_tokens"] == 1000  # 不减
        assert usage["cache_read_tokens"] == 500
        assert usage["cache_write_tokens"] == 200

    def test_dict_response(self) -> None:
        resp = {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 30,
            }
        }
        usage = extract_anthropic_usage(resp)
        assert usage["input_tokens"] == 100
        assert usage["cache_read_tokens"] == 30

    def test_no_usage_returns_empty(self) -> None:
        resp = FakeResponse()
        assert extract_anthropic_usage(resp) == {}

    def test_none_response(self) -> None:
        assert extract_anthropic_usage(None) == {}


# ======================================================================
# preview_messages
# ======================================================================


class TestPreviewMessages:
    """messages 数组预览生成。"""

    def test_string_content(self) -> None:
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = preview_messages(messages)
        assert "user: hello" in result
        assert "assistant: hi there" in result

    def test_multimodal_content(self) -> None:
        """content 是 block 数组时，提取 text block，其他标记 [type]。"""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        result = preview_messages(messages)
        assert "user: describe this [image_url]" in result

    def test_truncates_long_messages(self) -> None:
        """超过 _MAX_PREVIEW 的截断。"""
        long_msg = [{"role": "user", "content": "x" * 5000}]
        result = preview_messages(long_msg)
        assert len(result) <= 2048

    def test_limits_message_count(self) -> None:
        """超过 _MAX_MESSAGES 条只取前 10 条。"""
        messages = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
        result = preview_messages(messages)
        assert "msg 9" in result
        assert "msg 10" not in result

    def test_non_list_input(self) -> None:
        assert "some string" in preview_messages("some string")

    def test_object_attributes(self) -> None:
        """message 是对象（属性访问）也要兼容。"""
        msg = FakeMessage(role="system", content="instructions")
        result = preview_messages([msg])
        assert "system: instructions" in result


# ======================================================================
# preview_output
# ======================================================================


class TestPreviewOutput:
    """响应输出预览生成。"""

    def test_openai_chat_response(self) -> None:
        """choices[0].message.content 路径。"""
        resp = FakeResponse(
            choices=[
                FakeResponse(
                    message=FakeResponse(content="hello world"),
                    finish_reason="stop",
                )
            ]
        )
        result = preview_output(resp)
        assert "hello world" in result

    def test_openai_dict_response(self) -> None:
        resp = {"choices": [{"message": {"content": "dict output"}}]}
        assert "dict output" in preview_output(resp)

    def test_anthropic_response(self) -> None:
        """content blocks 路径。"""
        resp = FakeResponse(
            content=[
                FakeResponse(type="text", text="anthropic output"),
            ]
        )
        result = preview_output(resp)
        assert "anthropic output" in result

    def test_anthropic_multiple_blocks(self) -> None:
        resp = FakeResponse(
            content=[
                FakeResponse(type="text", text="part 1"),
                FakeResponse(type="text", text="part 2"),
            ]
        )
        result = preview_output(resp)
        assert "part 1" in result
        assert "part 2" in result

    def test_embedding_response(self) -> None:
        """embeddings 返回维度信息，不回显向量。"""
        resp = FakeResponse(data=[FakeResponse(embedding=[0.1] * 1536)])
        result = preview_output(resp)
        assert "embedding" in result
        assert "1536" in result

    def test_string_response(self) -> None:
        assert preview_output("plain string") == "plain string"

    def test_truncation(self) -> None:
        resp = FakeResponse(
            choices=[
                FakeResponse(message=FakeResponse(content="x" * 5000))
            ]
        )
        assert len(preview_output(resp)) <= 2048


# ======================================================================
# wrap_openai
# ======================================================================


class TestWrapOpenAI:
    """OpenAI 客户端包装 —— 验证 span 生成、usage 提取、幂等。"""

    def test_chat_completion_generates_span(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=100, completion_tokens=50),
            model="gpt-4",
            choices=[
                FakeResponse(
                    message=FakeResponse(content="answer"),
                    finish_reason="stop",
                )
            ],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "hi"}],
        )

        assert len(patched_client.spans) == 1
        span = patched_client.spans[0]
        assert span.ended is True
        assert span.kind == "llm"
        assert span.provider == "openai"
        assert span.model == "gpt-4"
        assert span.input_tokens == 100
        assert span.output_tokens == 50
        assert "answer" in span.output_preview
        assert "hi" in span.input_preview

    def test_finish_reason_recorded(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=10, completion_tokens=5),
            model="gpt-4o",
            choices=[
                FakeResponse(
                    message=FakeResponse(content="ok"),
                    finish_reason="length",
                )
            ],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4o", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert "length" in span.attributes.get("finish_reason", "")

    def test_cached_tokens_subtracted_in_span(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(
                prompt_tokens=1000,
                completion_tokens=200,
                prompt_tokens_details=FakeResponse(cached_tokens=300),
            ),
            model="gpt-4",
            choices=[FakeResponse(message=FakeResponse(content="ok"))],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert span.input_tokens == 700  # 1000 - 300
        assert span.cache_read_tokens == 300

    def test_stream_skips_usage(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(model="gpt-4")
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "x"}],
            stream=True,
        )
        span = patched_client.spans[0]
        assert "usage_unavailable" in span.attributes

    def test_idempotent(self, patched_client: FakeClient) -> None:
        """重复包装同一客户端不产生嵌套 span。"""
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=10, completion_tokens=5),
            model="gpt-4",
            choices=[FakeResponse(message=FakeResponse(content="ok"))],
        )
        fake = FakeOpenAIClient(resp)
        wrap_openai(fake)
        wrap_openai(fake)  # 第二次不应嵌套
        fake.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": "x"}]
        )
        assert len(patched_client.spans) == 1

    def test_no_client_passthrough(self) -> None:
        """get_client() 返回 None 时不包装，直接返回原始结果。"""
        with patch("ariadne_sdk.instrument.openai.get_client", return_value=None):
            resp = FakeResponse(model="gpt-4")
            client = wrap_openai(FakeOpenAIClient(resp))
            result = client.chat.completions.create(
                model="gpt-4", messages=[{"role": "user", "content": "x"}]
            )
            assert result is resp

    def test_embedding_generates_span(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=20, completion_tokens=0),
            model="text-embedding-3-small",
            data=[FakeResponse(embedding=[0.1] * 1536)],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.embeddings.create(
            model="text-embedding-3-small", input="embed this"
        )
        assert len(patched_client.spans) == 1
        span = patched_client.spans[0]
        assert span.input_tokens == 20
        assert "embedding" in span.output_preview

    def test_response_model_recorded(
        self, patched_client: FakeClient
    ) -> None:
        """response.model 可能与请求 model 不同（如 gpt-4 -> gpt-4-0613）。"""
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=10, completion_tokens=5),
            model="gpt-4-0613",
            choices=[FakeResponse(message=FakeResponse(content="ok"))],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert span.model == "gpt-4"
        assert span.model_response == "gpt-4-0613"


# ======================================================================
# wrap_anthropic
# ======================================================================


class TestWrapAnthropic:
    """Anthropic 客户端包装。"""

    def test_messages_generates_span(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(input_tokens=100, output_tokens=50),
            model="claude-sonnet-5",
            content=[FakeResponse(type="text", text="anthropic answer")],
            stop_reason="end_turn",
        )
        client = wrap_anthropic(FakeAnthropicClient(resp))
        client.messages.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "hi"}],
        )

        assert len(patched_client.spans) == 1
        span = patched_client.spans[0]
        assert span.ended is True
        assert span.kind == "llm"
        assert span.provider == "anthropic"
        assert span.model == "claude-sonnet-5"
        assert span.input_tokens == 100
        assert span.output_tokens == 50
        assert "anthropic answer" in span.output_preview
        assert "hi" in span.input_preview

    def test_cache_tokens_in_span(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(
                input_tokens=500,
                output_tokens=200,
                cache_read_input_tokens=300,
                cache_creation_input_tokens=100,
            ),
            model="claude-sonnet-5",
            content=[FakeResponse(type="text", text="ok")],
        )
        client = wrap_anthropic(FakeAnthropicClient(resp))
        client.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert span.cache_read_tokens == 300
        assert span.cache_write_tokens == 100

    def test_stop_reason_recorded(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(input_tokens=10, output_tokens=5),
            model="claude-sonnet-5",
            content=[FakeResponse(type="text", text="ok")],
            stop_reason="max_tokens",
        )
        client = wrap_anthropic(FakeAnthropicClient(resp))
        client.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert "max_tokens" in span.attributes.get("finish_reason", "")

    def test_system_prompt_length_recorded(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(
            usage=FakeResponse(input_tokens=10, output_tokens=5),
            model="claude-sonnet-5",
            content=[FakeResponse(type="text", text="ok")],
        )
        client = wrap_anthropic(FakeAnthropicClient(resp))
        client.messages.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "x"}],
            system="You are a helpful assistant.",
        )
        span = patched_client.spans[0]
        assert "system_len" in span.attributes

    def test_stream_skips_usage(
        self, patched_client: FakeClient
    ) -> None:
        resp = FakeResponse(model="claude-sonnet-5")
        client = wrap_anthropic(FakeAnthropicClient(resp))
        client.messages.create(
            model="claude-sonnet-5",
            messages=[{"role": "user", "content": "x"}],
            stream=True,
        )
        span = patched_client.spans[0]
        assert "usage_unavailable" in span.attributes

    def test_idempotent(self, patched_client: FakeClient) -> None:
        resp = FakeResponse(
            usage=FakeResponse(input_tokens=10, output_tokens=5),
            model="claude-sonnet-5",
            content=[FakeResponse(type="text", text="ok")],
        )
        fake = FakeAnthropicClient(resp)
        wrap_anthropic(fake)
        wrap_anthropic(fake)
        fake.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
        )
        assert len(patched_client.spans) == 1

    def test_no_client_passthrough(self) -> None:
        with patch("ariadne_sdk.instrument.anthropic.get_client", return_value=None):
            resp = FakeResponse(model="claude-sonnet-5")
            client = wrap_anthropic(FakeAnthropicClient(resp))
            result = client.messages.create(
                model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
            )
            assert result is resp

    def test_no_messages_attribute(self) -> None:
        """客户端无 messages 属性时不崩溃。"""
        fake = object()  # 无 messages 属性
        result = wrap_anthropic(fake)
        assert result is fake


# ======================================================================
# 版本兼容矩阵
# ======================================================================


class TestVersionCompatibility:
    """模拟不同 SDK 版本的响应格式差异，验证提取逻辑兼容。"""

    def test_openai_legacy_text_field(self, patched_client: FakeClient) -> None:
        """旧版 OpenAI completion API 用 choices[0].text 而非 message.content。"""
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=50, completion_tokens=20),
            model="gpt-3.5-turbo-instruct",
            choices=[FakeResponse(text="legacy output")],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-3.5-turbo-instruct", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert "legacy output" in span.output_preview

    def test_openai_new_alias(self, patched_client: FakeClient) -> None:
        """新版 OpenAI 用 max_completion_tokens 替代 max_tokens。"""
        resp = FakeResponse(
            usage=FakeResponse(prompt_tokens=10, completion_tokens=5),
            model="gpt-4o",
            choices=[FakeResponse(message=FakeResponse(content="ok"))],
        )
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "x"}],
            max_completion_tokens=100,
        )
        span = patched_client.spans[0]
        assert span.attributes.get("max_tokens") == "100"

    def test_anthropic_object_response(self, patched_client: FakeClient) -> None:
        """真实 Anthropic SDK 返回对象（非 dict）—— stop_reason 是属性。"""
        resp = FakeObj(
            usage=FakeObj(input_tokens=100, output_tokens=50),
            model="claude-sonnet-5",
            content=[FakeObj(type="text", text="object response")],
            stop_reason="end_turn",
        )

        client = wrap_anthropic(FakeAnthropicClient(resp))
        client.messages.create(
            model="claude-sonnet-5", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert span.input_tokens == 100
        assert "object response" in span.output_preview

    def test_openai_none_usage(self, patched_client: FakeClient) -> None:
        """某些异常情况 usage 为 None —— 不应崩溃，token 记 0。"""
        resp = FakeResponse(
            model="gpt-4",
            choices=[FakeResponse(message=FakeResponse(content="ok"))],
        )
        # usage 字段不存在
        client = wrap_openai(FakeOpenAIClient(resp))
        client.chat.completions.create(
            model="gpt-4", messages=[{"role": "user", "content": "x"}]
        )
        span = patched_client.spans[0]
        assert span.input_tokens == 0
        assert span.output_tokens == 0
