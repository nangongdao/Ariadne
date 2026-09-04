"""API 层测试：认证、入队、错误格式、查询。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from tests.conftest import FakeQueue, FakeStore

SPAN = {
    "trace_id": "a" * 32,
    "span_id": "b" * 16,
    "name": "chat",
    "kind": "llm",
    "provider": "openai",
    "model": "gpt-4o",
    "started_at": "2026-08-25T10:00:00Z",
    "duration_ms": 1200,
    "usage": {"input_tokens": 100, "output_tokens": 50},
}


class TestAuth:
    def test_missing_key_rejected(self, client: TestClient) -> None:
        response = client.post("/v1/ingest/spans", json={"spans": [SPAN]})
        assert response.status_code == 401
        problem = response.json()
        assert problem["type"].endswith("/unauthorized")
        assert response.headers["content-type"].startswith("application/problem+json")

    def test_wrong_key_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/v1/ingest/spans", json={"spans": [SPAN]},
            headers={"X-Ariadne-Key": "wrong"},
        )
        assert response.status_code == 401

    def test_bearer_token_accepted(self, client: TestClient) -> None:
        response = client.post(
            "/v1/ingest/spans", json={"spans": [SPAN]},
            headers={"Authorization": "Bearer ak_test_key"},
        )
        assert response.status_code == 202


class TestIngest:
    def test_native_batch_queued(
        self, client: TestClient, auth: dict[str, str], fake_queue: FakeQueue
    ) -> None:
        response = client.post("/v1/ingest/spans", json={"spans": [SPAN]}, headers=auth)
        assert response.status_code == 202
        assert response.json()["accepted"] == 1

        # 同步路径只入队，不加工
        assert len(fake_queue.published) == 1
        batch = fake_queue.published[0]
        assert batch["format"] == "native"
        assert len(batch["payload"]) == 1

    def test_otlp_structure_flattened(
        self, client: TestClient, auth: dict[str, str], fake_queue: FakeQueue
    ) -> None:
        """resourceSpans/scopeSpans/spans 三层要展平，resource 属性要下传。"""
        response = client.post(
            "/v1/traces",
            headers=auth,
            json={
                "resourceSpans": [{
                    "resource": {"attributes": [
                        {"key": "service.name", "value": {"stringValue": "my-app"}}
                    ]},
                    "scopeSpans": [{
                        "scope": {"name": "openai-instr"},
                        "spans": [
                            {"traceId": "a" * 32, "spanId": "b" * 16, "name": "s1",
                             "startTimeUnixNano": 1787652000000000000, "attributes": []},
                            {"traceId": "a" * 32, "spanId": "c" * 16, "name": "s2",
                             "startTimeUnixNano": 1787652000000000000, "attributes": []},
                        ],
                    }],
                }]
            },
        )
        assert response.status_code == 202
        assert response.json()["accepted"] == 2

        payload = fake_queue.published[0]["payload"]
        assert payload[0]["_resource"]["service.name"] == "my-app"
        assert payload[0]["_scope"] == "openai-instr"

    def test_otlp_protobuf_accepted(
        self, client: TestClient, auth: dict[str, str], fake_queue: FakeQueue
    ) -> None:
        """M6 支持 OTLP protobuf 编码，解码为与 JSON 相同的结构。"""
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
            ExportTraceServiceRequest,
        )
        from opentelemetry.proto.common.v1.common_pb2 import AnyValue

        req = ExportTraceServiceRequest()
        rs = req.resource_spans.add()
        rs.resource.attributes.add(
            key="service.name", value=AnyValue(string_value="my-app")
        )
        ss = rs.scope_spans.add()
        span = ss.spans.add()
        span.trace_id = b"\x61" * 16
        span.span_id = b"\x62" * 8
        span.name = "chat"

        response = client.post(
            "/v1/traces",
            headers={**auth, "Content-Type": "application/x-protobuf"},
            content=req.SerializeToString(),
        )
        assert response.status_code == 202
        assert response.json()["accepted"] == 1
        assert len(fake_queue.published) == 1
        payload = fake_queue.published[0]["payload"]
        assert payload[0]["traceId"] == "61" * 16
        assert payload[0]["name"] == "chat"

    def test_otlp_invalid_protobuf_rejected_with_guidance(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """无效 protobuf 体应返回 400 + 描述性错误。"""
        response = client.post(
            "/v1/traces", headers={**auth, "Content-Type": "application/x-protobuf"},
            content=b"not a valid protobuf",
        )
        assert response.status_code == 400
        assert "protobuf" in response.json()["detail"].lower()

    def test_empty_otlp_accepted_as_noop(
        self, client: TestClient, auth: dict[str, str], fake_queue: FakeQueue
    ) -> None:
        response = client.post("/v1/traces", json={"resourceSpans": []}, headers=auth)
        assert response.status_code == 202
        assert response.json()["accepted"] == 0
        assert not fake_queue.published

    def test_batch_size_limit_enforced(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/ingest/spans", json={"spans": [SPAN] * 1001}, headers=auth
        )
        assert response.status_code == 413
        assert response.json()["limit"] == 1000

    def test_empty_spans_rejected_by_schema(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.post("/v1/ingest/spans", json={"spans": []}, headers=auth)
        assert response.status_code == 422
        assert response.json()["type"].endswith("/validation")

    def test_formats_listed(self, client: TestClient) -> None:
        response = client.get("/v1/ingest/formats")
        assert response.status_code == 200
        assert set(response.json()["formats"]) == {"native", "otlp", "openinference"}


class TestTraces:
    def test_trace_detail_builds_tree(
        self, client: TestClient, auth: dict[str, str], fake_store: FakeStore
    ) -> None:
        base = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)
        fake_store.rows["spans"] = [
            {"span_id": "a" * 16, "parent_span_id": "", "name": "root",
             "kind": "internal", "operation": "", "status": "ok", "error_type": "",
             "provider": "", "model_request": "", "model_response": "",
             "started_at": base, "duration_ms": 300,
             "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0, "cost_usd": "0",
             "input_preview": "", "output_preview": "", "input_ref": "",
             "output_ref": "", "attributes": {}, "tags": [], "loop_id": "",
             "iteration": 0},
            {"span_id": "b" * 16, "parent_span_id": "a" * 16, "name": "llm",
             "kind": "llm", "operation": "chat", "status": "ok", "error_type": "",
             "provider": "openai", "model_request": "gpt-4o",
             "model_response": "gpt-4o-2024-11-20",
             "started_at": base, "duration_ms": 200,
             "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0,
             "cache_write_tokens": 0, "reasoning_tokens": 0, "cost_usd": "0.0075",
             "input_preview": "q", "output_preview": "a", "input_ref": "",
             "output_ref": "", "attributes": {}, "tags": [], "loop_id": "",
             "iteration": 0},
        ]
        response = client.get(f"/v1/traces/{'a' * 32}", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert body["span_count"] == 2
        assert body["total_tokens"] == 150
        assert len(body["roots"]) == 1
        assert body["roots"][0]["children"][0]["model_response"] == "gpt-4o-2024-11-20"
        # self_ms 排除子节点耗时
        assert body["roots"][0]["self_ms"] == 100

    def test_missing_trace_returns_404(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.get(f"/v1/traces/{'f' * 32}", headers=auth)
        assert response.status_code == 404
        assert response.json()["type"].endswith("/not-found")

    def test_span_filters_are_parameterized(
        self, client: TestClient, auth: dict[str, str], fake_store: FakeStore
    ) -> None:
        """过滤值必须走参数化，不能拼进 SQL（注入防护）。"""
        response = client.get(
            "/v1/spans", headers=auth,
            params={"kind": "llm", "search": "'; DROP TABLE spans--"},
        )
        assert response.status_code == 200
        sql, params = fake_store.queries[-1]
        assert "DROP TABLE" not in sql
        assert params["q"] == "'; DROP TABLE spans--"


class TestCosts:
    def test_invalid_group_by_returns_400(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """分组维度直接进 SQL，非白名单值必须拒绝而非报 500。"""
        response = client.get(
            "/v1/costs", headers=auth, params={"group_by": "1; DROP TABLE spans"}
        )
        assert response.status_code == 400
        assert "allowed" in response.json()

    def test_cache_hit_ratio_computed(
        self, client: TestClient, auth: dict[str, str], fake_store: FakeStore
    ) -> None:
        fake_store.rows["cost_rollup"] = [{
            "model_request": "gpt-4o", "span_count": 10,
            "input_tokens": 1000, "output_tokens": 500,
            "cache_read_tokens": 3000, "cost_usd": "0.05",
        }]
        response = client.get("/v1/costs", headers=auth)
        assert response.status_code == 200
        body = response.json()
        # 3000 / (1000 + 3000) = 0.75
        assert body["cache_hit_ratio"] == 0.75
        assert body["total_tokens"] == 4500

    def test_cache_write_and_reasoning_reported(
        self, client: TestClient, auth: dict[str, str], fake_store: FakeStore
    ) -> None:
        """cache_write 计入总量（与 spans 的 total_tokens 表达式一致），
        reasoning 是 output 的子集，单独展示不并入 —— 并了就是双重计数。"""
        fake_store.rows["cost_rollup"] = [{
            "model_request": "o3", "span_count": 2,
            "input_tokens": 1000, "output_tokens": 500,
            "cache_read_tokens": 200, "cache_write_tokens": 300,
            "reasoning_tokens": 400, "cost_usd": "0.09",
        }]
        response = client.get("/v1/costs", headers=auth)
        assert response.status_code == 200
        body = response.json()
        assert body["total_tokens"] == 1000 + 500 + 200 + 300
        assert body["cache_write_tokens"] == 300
        assert body["reasoning_tokens"] == 400
        bucket = body["buckets"][0]
        assert bucket["cache_write_tokens"] == 300
        assert bucket["reasoning_tokens"] == 400
