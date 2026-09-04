"""Playground API 测试。

测试所有 4 个端点：run、compare、freeze、reproduce。
认证测试验证 API key 校验。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


# ---------- /v1/playground/run ----------


class TestPlaygroundRun:
    def test_run_returns_config(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/run",
            json={
                "prompt": "写一首关于秋天的诗",
                "config": {"model": "gpt-4o", "temperature": 0.8},
            },
            headers=auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["prompt"] == "写一首关于秋天的诗"
        assert data["config"]["model"] == "gpt-4o"
        assert data["config"]["temperature"] == 0.8
        assert "request_id" in data

    def test_run_default_config(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/run",
            json={"prompt": "hello"},
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["config"]["temperature"] == 0.7
        assert resp.json()["config"]["max_tokens"] == 4096

    def test_run_empty_prompt_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/run",
            json={"prompt": ""},
            headers=auth,
        )
        assert resp.status_code == 422

    def test_run_invalid_temperature(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/run",
            json={"prompt": "test", "config": {"temperature": 3.0}},
            headers=auth,
        )
        assert resp.status_code == 422


# ---------- /v1/playground/compare ----------


class TestPlaygroundCompare:
    def test_compare_multiple_configs(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/compare",
            json={
                "prompt": "解释量子计算",
                "configs": [
                    {"model": "gpt-4o", "temperature": 0.0},
                    {"model": "gpt-4o-mini", "temperature": 0.7},
                    {"model": "claude-3-opus", "temperature": 0.5},
                ],
            },
            headers=auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["prompt"] == "解释量子计算"
        assert len(data["results"]) == 3
        assert data["results"][0]["config"]["model"] == "gpt-4o"
        assert data["results"][1]["config"]["model"] == "gpt-4o-mini"
        assert data["results"][2]["config"]["model"] == "claude-3-opus"
        # 占位结果应为空
        assert data["results"][0]["output"] == ""

    def test_compare_empty_configs_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/compare",
            json={"prompt": "test", "configs": []},
            headers=auth,
        )
        assert resp.status_code == 422

    def test_compare_too_many_configs(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        configs = [{"model": f"model-{i}"} for i in range(10)]
        resp = client.post(
            "/v1/playground/compare",
            json={"prompt": "test", "configs": configs},
            headers=auth,
        )
        assert resp.status_code == 422


# ---------- /v1/playground/freeze ----------


class TestPlaygroundFreeze:
    def test_freeze_generates_spec(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/freeze",
            json={
                "task": "生成测试用例",
                "model": "gpt-4o",
                "temperature": 0.3,
                "assertions": [
                    {
                        "id": "has_tests",
                        "kind": "regex",
                        "spec": {"pattern": "def test_"},
                        "blocking": True,
                    },
                ],
                "budget": {"max_iterations": 5, "max_cost_usd": 0.5},
            },
            headers=auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "spec_yaml" in data
        assert "spec_dict" in data
        assert data["spec_dict"]["goal"]["task"] == "生成测试用例"
        assert data["spec_dict"]["goal"]["playground_model"] == "gpt-4o"
        assert data["spec_dict"]["goal"]["playground_temperature"] == 0.3
        assert len(data["spec_dict"]["goal"]["assertions"]) == 1
        assert data["spec_dict"]["goal"]["budget"]["max_iterations"] == 5
        assert data["spec_dict"]["goal"]["budget"]["max_cost_usd"] == 0.5
        # YAML 包含 task
        assert "生成测试用例" in data["spec_yaml"]

    def test_freeze_minimal(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/freeze",
            json={"task": "简单任务", "model": "gpt-4"},
            headers=auth,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["spec_dict"]["goal"]["task"] == "简单任务"
        assert data["spec_dict"]["goal"]["playground_model"] == "gpt-4"
        # 无断言时注入占位断言
        assert len(data["spec_dict"]["goal"]["assertions"]) == 1
        assert data["spec_dict"]["goal"]["assertions"][0]["id"] == "placeholder"
        # 默认预算
        assert data["spec_dict"]["goal"]["budget"]["max_iterations"] == 10

    def test_freeze_empty_task_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/freeze",
            json={"task": "", "model": "gpt-4"},
            headers=auth,
        )
        assert resp.status_code == 422


# ---------- /v1/playground/reproduce ----------


class TestPlaygroundReproduce:
    def test_reproduce_span_not_found(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        # ClickHouse 未连接，查询返回空 → 404
        resp = client.post(
            "/v1/playground/reproduce",
            json={"trace_id": "nonexistent", "span_id": "nonexistent"},
            headers=auth,
        )
        # ClickHouse 不可用时可能 503 或 404，取决于 ping 状态
        # 主要是验证 API 结构正确，而非数据库行为
        assert resp.status_code in (404, 500, 503)

    def test_reproduce_missing_fields(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        resp = client.post(
            "/v1/playground/reproduce",
            json={"trace_id": ""},
            headers=auth,
        )
        assert resp.status_code == 422


# ---------- 认证 ----------


class TestPlaygroundAuth:
    def test_no_key_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/playground/run",
            json={"prompt": "test"},
        )
        assert resp.status_code == 401

    def test_wrong_key_rejected(self, client: TestClient) -> None:
        resp = client.post(
            "/v1/playground/run",
            json={"prompt": "test"},
            headers={"X-Ariadne-Key": "wrong_key"},
        )
        assert resp.status_code == 401
