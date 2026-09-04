"""Harness API 路由测试 —— rules、specs、approvals。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

TEST_KEY = "ak_test_key"


class TestRulesAPI:
    """规则管理 + 试跑 API。"""

    def test_list_empty(self, client: TestClient, auth: dict[str, str]) -> None:
        """无规则集时返回空列表。"""
        response = client.get("/v1/rules", headers=auth)
        assert response.status_code == 200
        assert response.json() == []

    def test_create_and_list_rule_set(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """创建规则集后能列出。"""
        rules = [
            {
                "id": "block-pii",
                "category": "input",
                "hook": "pre_model",
                "when": "detect_pii(input.text).size() > 0",
                "action": "block",
                "severity": "critical",
                "message": "PII detected",
            }
        ]
        response = client.put(
            "/v1/rules",
            json={"name": "test-rules", "rules": rules},
            headers=auth,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "test-rules"
        assert body["version"] == 1
        assert len(body["rules"]) == 1

        # 列表
        response = client.get("/v1/rules", headers=auth)
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_update_creates_new_version(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """同名称更新创建新版本。"""
        rules1 = [
            {
                "id": "rule-1",
                "category": "input",
                "hook": "pre_model",
                "when": "true",
                "action": "warn",
            }
        ]
        client.put(
            "/v1/rules",
            json={"name": "versioned-rules", "rules": rules1},
            headers=auth,
        )

        rules2 = [
            *rules1,
            {
                "id": "rule-2",
                "category": "output",
                "hook": "post_model",
                "when": "true",
                "action": "warn",
            }
        ]
        response = client.put(
            "/v1/rules",
            json={"name": "versioned-rules", "rules": rules2},
            headers=auth,
        )
        assert response.status_code == 201
        assert response.json()["version"] == 2

    def test_create_invalid_rule_returns_400(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """语法错误的规则返回 400。"""
        rules = [
            {
                "id": "bad",
                "category": "input",
                "hook": "pre_model",
                "when": "input..text",  # 语法错误
                "action": "warn",
            }
        ]
        response = client.put(
            "/v1/rules",
            json={"name": "bad-rules", "rules": rules},
            headers=auth,
        )
        assert response.status_code == 400

    def test_test_rules_no_ruleset(self, client: TestClient, auth: dict[str, str]) -> None:
        """无规则集时试跑返回 allow。"""
        response = client.post(
            "/v1/rules/test",
            json={"hook": "pre_model", "context": {"input": {"text": "hello"}}},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["action"] == "allow"

    def test_test_rules_with_block(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """有规则集时试跑返回正确裁决。"""
        rules = [
            {
                "id": "block-pii",
                "category": "input",
                "hook": "pre_model",
                "when": "detect_pii(input.text).size() > 0",
                "action": "block",
                "severity": "critical",
            }
        ]
        client.put(
            "/v1/rules",
            json={"name": "test-rules", "rules": rules},
            headers=auth,
        )

        # 有 PII → block
        response = client.post(
            "/v1/rules/test",
            json={
                "hook": "pre_model",
                "context": {"input": {"text": "email: test@example.com"}},
            },
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["action"] == "block"

        # 无 PII → allow
        response = client.post(
            "/v1/rules/test",
            json={"hook": "pre_model", "context": {"input": {"text": "clean"}}},
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json()["action"] == "allow"

    def test_test_rules_unknown_hook(self, client: TestClient, auth: dict[str, str]) -> None:
        """未知 hook 返回 400。"""
        response = client.post(
            "/v1/rules/test",
            json={"hook": "nonexistent", "context": {}},
            headers=auth,
        )
        assert response.status_code == 400

    def test_auth_required(self, client: TestClient) -> None:
        """无 API key 返回 401。"""
        response = client.get("/v1/rules")
        assert response.status_code == 401


class TestSpecsAPI:
    """Spec 管理 API。"""

    # ClassVar — 测试用的有效 spec 模板（不依赖实例状态）
    _VALID_SPEC: ClassVar[dict[str, Any]] = {
        "version": "1",
        "goal": {
            "task": "实现功能",
            "mode": "quality",
            "assertions": [
                {
                    "id": "test-pass",
                    "kind": "command",
                    "spec": {"cmd": "pytest -x"},
                    "blocking": True,
                }
            ],
            "budget": {"max_iterations": 3},
        },
        "rules": [],
        "sandbox": {"profile": "strict"},
    }

    def test_create_spec(self, client: TestClient, auth: dict[str, str]) -> None:
        """创建有效 spec 返回 201。"""
        response = client.post(
            "/v1/specs",
            json={"spec": self._VALID_SPEC},
            headers=auth,
        )
        assert response.status_code == 201
        body = response.json()
        assert body["version"] == 1
        assert "id" in body

    def test_create_invalid_spec(self, client: TestClient, auth: dict[str, str]) -> None:
        """无效 spec 返回 400。"""
        response = client.post(
            "/v1/specs",
            json={"spec": {"version": "1"}},  # 缺 goal
            headers=auth,
        )
        assert response.status_code == 400

    def test_create_unverifiable_spec(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """不可验证的 spec 返回 422。"""
        spec = {
            **self._VALID_SPEC,
            "goal": {
                "task": "测试",
                "mode": "quality",
                "assertions": [
                    {
                        "id": "soft",
                        "kind": "command",
                        "spec": {"cmd": "echo"},
                        "blocking": False,  # 全 non-blocking → 不可验证
                    }
                ],
            },
        }
        response = client.post(
            "/v1/specs",
            json={"spec": spec},
            headers=auth,
        )
        assert response.status_code == 422

    def test_list_specs(self, client: TestClient, auth: dict[str, str]) -> None:
        """列出 spec。"""
        client.post("/v1/specs", json={"spec": self._VALID_SPEC}, headers=auth)
        response = client.get("/v1/specs", headers=auth)
        assert response.status_code == 200
        assert len(response.json()) >= 1

    def test_get_spec_not_found(self, client: TestClient, auth: dict[str, str]) -> None:
        """不存在的 spec 返回 404。"""
        response = client.get(
            "/v1/specs/00000000-0000-0000-0000-000000000099",
            headers=auth,
        )
        assert response.status_code == 404


class TestApprovalsAPI:
    """审批 API。需要先创建 loop。"""

    def test_create_loop_first(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """创建审批需要先有 loop。此测试验证无 loop 时返回 404。"""
        response = client.post(
            "/v1/loops/00000000-0000-0000-0000-000000000099/approvals",
            json={"context": {}, "expires_in_seconds": 3600},
            headers=auth,
        )
        assert response.status_code == 404

    def test_list_approvals_empty_loop(
        self, client: TestClient, auth: dict[str, str]
    ) -> None:
        """不存在的 loop 查审批返回空列表（不报错）。"""
        # 由于 loop 不存在，列表查询不会验证 loop 存在性
        # 它只查 approvals 表
        response = client.get(
            "/v1/loops/00000000-0000-0000-0000-000000000099/approvals",
            headers=auth,
        )
        assert response.status_code == 200
        assert response.json() == []

    def test_decide_not_found(self, client: TestClient, auth: dict[str, str]) -> None:
        """不存在的审批 decide 返回 404。"""
        response = client.post(
            "/v1/approvals/00000000-0000-0000-0000-000000000099/decide",
            json={"decision": "approved", "reviewer": "admin"},
            headers=auth,
        )
        assert response.status_code == 404
