"""M1 演示：模拟一个 RAG 应用，产生完整调用树。

不需要真实的 LLM API key —— 用假响应模拟，重点验证采集链路。

运行：
    docker compose up -d
    uv run python examples/demo_rag.py
    curl -H "X-Ariadne-Key: ak_local_dev_key" http://localhost:8000/v1/traces
"""

from __future__ import annotations

import os
import random
import time
from typing import Any

from ariadne_sdk import init, trace
from ariadne_sdk.span import current_trace_id

API_KEY = os.getenv("ARIADNE_API_KEY", "ak_local_dev_key")
ENDPOINT = os.getenv("ARIADNE_ENDPOINT", "http://localhost:8000/v1/ingest/spans")

client = init(api_key=API_KEY, endpoint=ENDPOINT, project="demo-rag", flush_interval=0.5)


class FakeUsage:
    def __init__(self, prompt: int, completion: int, cached: int = 0) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.prompt_tokens_details = type("D", (), {"cached_tokens": cached})()


@trace(kind="rag", capture_io=False)
def retrieve(query: str, top_k: int = 5) -> list[str]:
    """模拟向量检索。"""
    time.sleep(random.uniform(0.05, 0.15))
    return [f"doc-{i}: 关于 {query} 的内容片段" for i in range(top_k)]


def call_llm(prompt: str, docs: list[str], *, cached: bool = False) -> str:
    """模拟 LLM 调用。手动埋点以演示完整的用量与模型版本记录。"""
    with client.span("chat gpt-4o", kind="llm", operation="chat",
                     provider="openai", model="gpt-4o") as span:
        span.set_input(f"system: 你是助手\nuser: {prompt}\n引用 {len(docs)} 篇文档")
        span.set_attributes({"temperature": 0.7, "max_tokens": 1024, "top_k": len(docs)})
        time.sleep(random.uniform(0.3, 0.8))

        answer = f"根据检索到的 {len(docs)} 篇文档，{prompt} 的答案是……"
        # 缓存命中时 input 少、cache_read 多，用于演示成本折扣
        if cached:
            span.set_usage(input_tokens=200, output_tokens=180, cache_read_tokens=1800)
        else:
            span.set_usage(input_tokens=2000, output_tokens=180)

        span.set_model("gpt-4o", response_model="gpt-4o-2024-11-20", provider="openai")
        span.set_output(answer)
        return answer


@trace(kind="tool")
def lookup_order(order_id: str) -> dict[str, Any]:
    """模拟工具调用。"""
    time.sleep(0.05)
    return {"order_id": order_id, "status": "shipped", "items": 3}


def answer_question(question: str, *, cached: bool = False) -> str:
    """一次完整请求，产生 4 层调用树。"""
    with client.span("answer_question", kind="internal") as root:
        root.set_input(question)
        root.set_attributes({"user_tier": "pro"})

        docs = retrieve(question)
        lookup_order("ORD-20260825-001")
        answer = call_llm(question, docs, cached=cached)

        root.set_output(answer)
        return answer


def failing_request() -> None:
    """演示错误 span：异常会被记录但仍向上传播。"""
    with client.span("answer_question", kind="internal") as root:
        root.set_input("这个会失败")
        retrieve("失败场景")
        raise RuntimeError("模拟 provider 超时")


def pii_request() -> str:
    """演示脱敏：这些 PII 不会原样落库。"""
    with client.span("answer_question", kind="internal") as root:
        root.set_input("我的邮箱 alice@example.com，手机 13800138000，帮我查订单")
        root.set_output("已发送到 alice@example.com")
        return "done"


def main() -> None:
    print(f"上报到 {ENDPOINT}")

    for i in range(3):
        trace_id = ""
        with client.span("_probe"):
            trace_id = current_trace_id()
        answer = answer_question(f"如何优化第 {i + 1} 个查询", cached=(i == 2))
        print(f"  [{i + 1}] {answer[:40]}…  trace={trace_id[:12]}")

    try:
        failing_request()
    except RuntimeError as exc:
        print(f"  [错误 span] 已记录: {exc}")

    pii_request()
    print("  [脱敏 span] 已记录（PII 不会原样落库）")

    if client.flush(timeout=10):
        print(f"\n上报完成: {client.stats}")
    else:
        print(f"\n上报未在超时内完成: {client.stats}")
    client.shutdown()

    print("\n查看结果：")
    print('  curl -H "X-Ariadne-Key: ak_local_dev_key" http://localhost:8000/v1/traces')
    print('  curl -H "X-Ariadne-Key: ak_local_dev_key" '
          '"http://localhost:8000/v1/costs?group_by=model_request"')


if __name__ == "__main__":
    main()
