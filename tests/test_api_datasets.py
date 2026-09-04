"""数据集与实验 API 测试。

关键验证：
1. 版本不可变（没有 PUT/PATCH，改内容只能建新版本）
2. JSONL 往返后 content_hash 不变
3. 跨数据集对比被拒
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def items(n: int = 3) -> list[dict[str, Any]]:
    return [
        {"item_id": f"i{k}", "input": f"问题{k}", "expected": f"答案{k}"}
        for k in range(n)
    ]


class TestDatasetCrud:
    def test_create_returns_hash_and_ref(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/datasets", headers=auth, json={"name": "core", "items": items(3)}
        )
        assert response.status_code == 201
        body = response.json()
        assert body["version"] == 1
        assert body["item_count"] == 3
        assert len(body["content_hash"]) == 16
        assert body["ref"].startswith("core@v1#")

    def test_version_auto_increments(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        client.post("/v1/datasets", headers=auth, json={"name": "core", "items": items(2)})
        second = client.post(
            "/v1/datasets", headers=auth, json={"name": "core", "items": items(5)}
        )
        assert second.json()["version"] == 2

    def test_explicit_duplicate_version_conflicts(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """版本不可变：重复版本号返回 409 而非静默递增。"""
        payload = {"name": "core", "items": items(2), "version": 1}
        assert client.post("/v1/datasets", headers=auth, json=payload).status_code == 201
        conflict = client.post("/v1/datasets", headers=auth, json=payload)
        assert conflict.status_code == 409
        assert conflict.json()["type"].endswith("/conflict")

    def test_no_update_endpoint_exists(self, client: TestClient) -> None:
        """刻意没有 PUT/PATCH —— 允许原地改会让历史实验的 hash 失效。"""
        spec = client.app.openapi()  # type: ignore[attr-defined]
        dataset_paths = {
            p: set(ops) for p, ops in spec["paths"].items() if "/datasets" in p
        }
        for path, methods in dataset_paths.items():
            assert "put" not in methods, f"{path} 不应有 PUT"
            assert "patch" not in methods, f"{path} 不应有 PATCH"

    def test_duplicate_item_ids_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/datasets",
            headers=auth,
            json={
                "name": "bad",
                "items": [
                    {"item_id": "same", "input": "a"},
                    {"item_id": "same", "input": "b"},
                ],
            },
        )
        assert response.status_code == 400
        assert "重复" in response.json()["detail"]

    def test_get_specific_version(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        client.post("/v1/datasets", headers=auth, json={"name": "core", "items": items(2)})
        client.post("/v1/datasets", headers=auth, json={"name": "core", "items": items(5)})

        v1 = client.get("/v1/datasets/core", headers=auth, params={"version": 1})
        assert v1.json()["item_count"] == 2
        latest = client.get("/v1/datasets/core", headers=auth)
        assert latest.json()["item_count"] == 5

    def test_missing_returns_404(self, client: TestClient, auth: dict[str, str]) -> None:
        response = client.get("/v1/datasets/nope", headers=auth)
        assert response.status_code == 404

    def test_list_and_versions(self, client: TestClient, auth: dict[str, str]) -> None:
        client.post("/v1/datasets", headers=auth, json={"name": "core", "items": items(2)})
        client.post("/v1/datasets", headers=auth, json={"name": "core", "items": items(3)})
        client.post("/v1/datasets", headers=auth, json={"name": "edge", "items": items(1)})

        listing = client.get("/v1/datasets", headers=auth).json()
        assert {e["name"]: e["latest_version"] for e in listing} == {
            "core": 2,
            "edge": 1,
        }

        versions = client.get("/v1/datasets/core/versions", headers=auth).json()
        assert [v["version"] for v in versions] == [2, 1]

    def test_auth_required(self, client: TestClient) -> None:
        assert client.get("/v1/datasets").status_code == 401


class TestJsonlRoundtrip:
    def test_export_import_preserves_hash(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """往返后 hash 必须一致，否则复现校验形同虚设。"""
        created = client.post(
            "/v1/datasets", headers=auth, json={"name": "core", "items": items(10)}
        ).json()

        exported = client.get("/v1/datasets/core/export", headers=auth)
        assert exported.status_code == 200
        assert "attachment" in exported.headers["content-disposition"]

        reimported = client.post(
            "/v1/datasets/import",
            headers=auth,
            json={"name": "core-copy", "jsonl": exported.text},
        )
        assert reimported.status_code == 201
        assert reimported.json()["content_hash"] == created["content_hash"]

    def test_bad_jsonl_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """解析不容错：静默跳过坏行会导致两次实验用的不是同一数据集。"""
        response = client.post(
            "/v1/datasets/import",
            headers=auth,
            json={"name": "bad", "jsonl": '{"input":"ok"}\n{broken'},
        )
        assert response.status_code == 400
        assert "第 2 行" in response.json()["detail"]

    def test_missing_input_field_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        response = client.post(
            "/v1/datasets/import",
            headers=auth,
            json={"name": "bad", "jsonl": '{"item_id":"a"}'},
        )
        assert response.status_code == 400


def make_result(
    *, dataset_ref: str, label: str, score: float, passed: bool, cost: str = "0.01"
) -> dict[str, Any]:
    """构造 persist 格式的实验结果。"""
    return {
        "schema_version": 1,
        "experiment_id": label,
        "dataset_ref": dataset_ref,
        "config_label": label,
        "judge_models": [],
        "note": "",
        "outcomes": [
            {
                "item_id": f"i{k:02d}",
                "output": "out",
                "results": [
                    {
                        "name": "quality",
                        "value": score,
                        "passed": passed,
                        "evidence": "",
                        "violations": [],
                        "judge_model": "",
                        "duration_ms": 1,
                        "cost_usd": "0",
                        "errored": False,
                    }
                ],
                "composite_score": score,
                "passed": passed,
                "cost_usd": cost,
                "duration_ms": 1,
                "generation_error": "",
                "metadata": {},
            }
            for k in range(20)
        ],
    }


class TestExperiments:
    def _create(self, client: TestClient, auth: dict[str, str], **kw: Any) -> str:
        body = {
            "dataset_ref": kw.get("dataset_ref", "core@v1#abc"),
            "config_label": kw.get("label", "v1"),
            "config": {},
        }
        response = client.post("/v1/experiments", headers=auth, json=body)
        assert response.status_code == 201
        return str(response.json()["id"])

    def test_create_and_get(self, client: TestClient, auth: dict[str, str]) -> None:
        exp_id = self._create(client, auth)
        detail = client.get(f"/v1/experiments/{exp_id}", headers=auth).json()
        assert detail["status"] == "pending"
        assert detail["dataset_ref"] == "core@v1#abc"

    def test_submit_result_persists_metrics(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        exp_id = self._create(client, auth)
        response = client.post(
            f"/v1/experiments/{exp_id}/result",
            headers=auth,
            json={
                "result": make_result(
                    dataset_ref="core@v1#abc", label="v1", score=90.0, passed=True
                )
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "completed"
        assert body["item_count"] == 20
        assert body["metrics"]["composite_quality"] == 90.0

    def test_submit_twice_conflicts(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        exp_id = self._create(client, auth)
        payload = {
            "result": make_result(
                dataset_ref="core@v1#abc", label="v1", score=90.0, passed=True
            )
        }
        client.post(f"/v1/experiments/{exp_id}/result", headers=auth, json=payload)
        again = client.post(
            f"/v1/experiments/{exp_id}/result", headers=auth, json=payload
        )
        assert again.status_code == 409

    def test_bad_schema_version_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        exp_id = self._create(client, auth)
        bad = make_result(dataset_ref="d", label="v1", score=1.0, passed=True)
        bad["schema_version"] = 999
        response = client.post(
            f"/v1/experiments/{exp_id}/result", headers=auth, json={"result": bad}
        )
        assert response.status_code == 400
        assert "重跑 baseline" in response.json()["detail"]

    def test_missing_experiment_404(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        fake = "00000000-0000-0000-0000-0000000000ff"
        assert client.get(f"/v1/experiments/{fake}", headers=auth).status_code == 404

    def test_list_filters_by_dataset(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        self._create(client, auth, dataset_ref="core@v1#abc")
        self._create(client, auth, dataset_ref="other@v1#xyz")
        filtered = client.get(
            "/v1/experiments", headers=auth, params={"dataset_ref": "core@v1#abc"}
        ).json()
        assert len(filtered) == 1


class TestCompare:
    def _prepare(
        self, client: TestClient, auth: dict[str, str], *, score: float,
        passed: bool, label: str, dataset_ref: str = "core@v1#abc", cost: str = "0.01",
    ) -> str:
        exp_id = str(
            client.post(
                "/v1/experiments",
                headers=auth,
                json={
                    "dataset_ref": dataset_ref,
                    "config_label": label,
                    "config": {},
                },
            ).json()["id"]
        )
        result = make_result(
            dataset_ref=dataset_ref, label=label, score=score, passed=passed, cost=cost
        )
        client.put(
            f"/v1/experiments/{exp_id}/snapshot", headers=auth, json={"result": result}
        )
        client.post(
            f"/v1/experiments/{exp_id}/result", headers=auth, json={"result": result}
        )
        return exp_id

    def test_improvement_passes(self, client: TestClient, auth: dict[str, str]) -> None:
        base = self._prepare(client, auth, score=50.0, passed=False, label="v1")
        curr = self._prepare(client, auth, score=90.0, passed=True, label="v2")
        response = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": base, "current_id": curr},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["passed"] is True
        assert body["exit_code"] == 0

    def test_regression_blocks(self, client: TestClient, auth: dict[str, str]) -> None:
        base = self._prepare(client, auth, score=90.0, passed=True, label="v1")
        curr = self._prepare(client, auth, score=50.0, passed=False, label="v2")
        body = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": base, "current_id": curr},
        ).json()
        assert body["passed"] is False
        assert body["exit_code"] == 1
        assert body["violations"]
        assert len(body["flipped_to_fail"]) == 20

    def test_dataset_mismatch_rejected(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """在不同数据集上比均值无意义。"""
        base = self._prepare(
            client, auth, score=90.0, passed=True, label="v1",
            dataset_ref="core@v1#abc",
        )
        curr = self._prepare(
            client, auth, score=90.0, passed=True, label="v2",
            dataset_ref="other@v1#xyz",
        )
        response = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": base, "current_id": curr},
        )
        assert response.status_code == 400
        assert "数据集不一致" in response.json()["detail"]

    def test_allow_mismatch_flag(self, client: TestClient, auth: dict[str, str]) -> None:
        base = self._prepare(
            client, auth, score=90.0, passed=True, label="v1", dataset_ref="a@v1#1"
        )
        curr = self._prepare(
            client, auth, score=90.0, passed=True, label="v2", dataset_ref="b@v1#2"
        )
        response = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={
                "baseline_id": base,
                "current_id": curr,
                "allow_dataset_mismatch": True,
            },
        )
        assert response.status_code == 200

    def test_custom_gate_rules(self, client: TestClient, auth: dict[str, str]) -> None:
        """成本上涨门禁：质量持平但成本翻倍应被阻断。"""
        base = self._prepare(
            client, auth, score=90.0, passed=True, label="v1", cost="0.01"
        )
        curr = self._prepare(
            client, auth, score=90.0, passed=True, label="v2", cost="0.03"
        )
        body = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={
                "baseline_id": base,
                "current_id": curr,
                "fail_if": [{"metric": "cost_per_item", "increase_pct": 10}],
            },
        ).json()
        assert body["passed"] is False

    def test_missing_snapshot_gives_actionable_error(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        exp_id = str(
            client.post(
                "/v1/experiments",
                headers=auth,
                json={"dataset_ref": "d", "config_label": "v1", "config": {}},
            ).json()["id"]
        )
        response = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": exp_id, "current_id": exp_id},
        )
        assert response.status_code == 400
        detail = response.json()["detail"]
        assert "补传完整结果" in detail
        # 报错必须指向真正会写快照的端点
        assert "/snapshot" in detail

    def test_report_text_included(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        base = self._prepare(client, auth, score=50.0, passed=False, label="v1")
        curr = self._prepare(client, auth, score=90.0, passed=True, label="v2")
        body = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": base, "current_id": curr},
        ).json()
        assert "指标变化" in body["report_text"]
        assert "门禁" in body["report_text"]


class TestCompareStandardPath:
    """只走 POST /result 的标准路径也要能对比。

    此前 save_result 只写聚合指标，逐样本快照全靠手动 PUT /snapshot 补，
    正常完成的实验一律 400。
    """

    def _complete(
        self,
        client: TestClient,
        auth: dict[str, str],
        *,
        score: float,
        passed: bool,
        label: str,
    ) -> str:
        exp_id = str(
            client.post(
                "/v1/experiments",
                headers=auth,
                json={
                    "dataset_ref": "core@v1#abc",
                    "config_label": label,
                    "config": {},
                },
            ).json()["id"]
        )
        result = make_result(
            dataset_ref="core@v1#abc", label=label, score=score, passed=passed
        )
        # 注意：不调 PUT /snapshot
        response = client.post(
            f"/v1/experiments/{exp_id}/result", headers=auth, json={"result": result}
        )
        assert response.status_code == 200
        return exp_id

    def test_compare_works_without_manual_snapshot(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        base = self._complete(client, auth, score=50.0, passed=False, label="v1")
        curr = self._complete(client, auth, score=90.0, passed=True, label="v2")
        response = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": base, "current_id": curr},
        )
        assert response.status_code == 200
        assert response.json()["passed"] is True

    def test_snapshot_not_exposed_in_list(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """快照不能出现在列表响应里 —— 一次拉 200 条，带全文会拖慢每次请求。"""
        self._complete(client, auth, score=90.0, passed=True, label="v1")
        body = client.get("/v1/experiments", headers=auth).json()
        assert body
        for row in body:
            assert "result_snapshot" not in row
            assert "_result_snapshot" not in str(row.get("metrics", {}))

    def test_per_sample_stats_computed(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """置信区间来自逐样本，聚合指标算不出 —— 有 CI 才证明快照真的落库了。"""
        base = self._complete(client, auth, score=90.0, passed=True, label="v1")
        curr = self._complete(client, auth, score=50.0, passed=False, label="v2")
        body = client.post(
            "/v1/experiments/compare",
            headers=auth,
            json={"baseline_id": base, "current_id": curr},
        ).json()
        assert body["flipped_to_fail"]
        stats = body["stats"]
        assert stats
        assert any(s["sample_count"] > 0 for s in stats)


class TestHealthIncludesPostgres:
    def test_health_reports_pg(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert "postgres" in body
        assert body["postgres"] is True
