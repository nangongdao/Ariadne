"""LLM 模型配置 API + 仓储 + 解析器测试。

覆盖：
- 仓储 CRUD（加密往返、唯一名约束、default 唯一性）
- API 端点（创建/列表/默认/更新/删除，api_key 仅创建时返回明文）
- resolve_project_llm（有配置覆盖 env、无配置回退 fallback_client）
- 加密降级（cryptography 不可用时明文存储仍可用）

用内存 SQLite（aiosqlite）跑真实 SQL，不 mock 数据库。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
from pydantic import SecretStr

from ariadne.config import LlmSettings

TEST_PROJECT = UUID("00000000-0000-0000-0000-000000000001")
TEST_ORG = UUID("00000000-0000-0000-0000-0000000000aa")
TEST_KEY = "ak_test_key"

# settings/app/client/auth/memory_pg 夹具来自 conftest.py。
# 加密密钥走 get_settings() 全局单例（见 repositories/model_configs.py:48），
# 不读注入的 settings，所以此处无需自备 jwt_secret。


# ---------- 仓储测试 ----------


class TestModelConfigRepository:
    async def test_create_and_get(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.model_configs import (
            LlmModelConfigRepository,
            decrypt_api_key,
        )

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            config_id, prefix = await repo.create(
                project_id=TEST_PROJECT,
                name="我的 GPT-4o",
                provider="openai",
                model="gpt-4o",
                api_key="sk-test-1234567890",
                base_url="https://api.openai.com",
                encryption_secret="secret",
            )
        assert config_id is not None
        assert prefix == "sk-test-1234"

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            row = await repo.get(project_id=TEST_PROJECT, config_id=config_id)
            assert row is not None
            assert row.model == "gpt-4o"
            # 解密往返
            plaintext = decrypt_api_key(row.api_key_encrypted, "secret")
            assert plaintext == "sk-test-1234567890"

    async def test_duplicate_name_rejected(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.model_configs import (
            DuplicateLlmModelConfigError,
            LlmModelConfigRepository,
        )

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            await repo.create(
                project_id=TEST_PROJECT,
                name="dup",
                provider="openai",
                model="gpt-4o",
                api_key="k",
                base_url="",
                encryption_secret="s",
            )
            with pytest.raises(DuplicateLlmModelConfigError):
                await repo.create(
                    project_id=TEST_PROJECT,
                    name="dup",
                    provider="openai",
                    model="gpt-4o",
                    api_key="k",
                    base_url="",
                    encryption_secret="s",
                )

    async def test_default_uniqueness(self, memory_pg: Any) -> None:
        """is_default=True 时清掉同项目其他 default。"""
        from ariadne.storage.postgres.repositories.model_configs import (
            LlmModelConfigRepository,
        )

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            await repo.create(
                project_id=TEST_PROJECT,
                name="first",
                provider="openai",
                model="gpt-4o",
                api_key="k1",
                base_url="",
                is_default=True,
                encryption_secret="s",
            )
            await repo.create(
                project_id=TEST_PROJECT,
                name="second",
                provider="anthropic",
                model="claude",
                api_key="k2",
                base_url="",
                is_default=True,
                encryption_secret="s",
            )
            rows = await repo.list(project_id=TEST_PROJECT)
            defaults = [r for r in rows if r.is_default]
            assert len(defaults) == 1
            assert defaults[0].name == "second"

    async def test_update_preserves_api_key_when_none(self, memory_pg: Any) -> None:
        from ariadne.storage.postgres.repositories.model_configs import (
            LlmModelConfigRepository,
            decrypt_api_key,
        )

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            config_id, _ = await repo.create(
                project_id=TEST_PROJECT,
                name="cfg",
                provider="openai",
                model="gpt-4o",
                api_key="sk-original",
                base_url="",
                encryption_secret="s",
            )
            # 更新 model 但不传 api_key
            await repo.update(
                project_id=TEST_PROJECT,
                config_id=config_id,
                model="gpt-4o-mini",
                encryption_secret="s",
            )
        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            row = await repo.get(project_id=TEST_PROJECT, config_id=config_id)
            assert row is not None
            assert row.model == "gpt-4o-mini"
            assert decrypt_api_key(row.api_key_encrypted, "s") == "sk-original"


# ---------- 加密降级 ----------


class TestEncryption:
    def test_empty_key_roundtrip(self) -> None:
        from ariadne.storage.postgres.repositories.model_configs import (
            decrypt_api_key,
            encrypt_api_key,
        )

        assert encrypt_api_key("", "s") == ""
        assert decrypt_api_key("", "s") == ""

    def test_encrypt_decrypt_roundtrip(self) -> None:
        from ariadne.storage.postgres.repositories.model_configs import (
            decrypt_api_key,
            encrypt_api_key,
        )

        plaintext = "sk-proj-abc123XYZ"
        enc = encrypt_api_key(plaintext, "my-secret")
        assert enc != plaintext  # 确实加密了
        assert decrypt_api_key(enc, "my-secret") == plaintext

    def test_rotation_old_key_still_decrypts(self) -> None:
        """密钥轮换：主密钥解不开时落到历史密钥。"""
        from ariadne.storage.postgres.repositories.model_configs import (
            ApiKeyDecryptionError,
            decrypt_api_key,
            encrypt_api_key,
        )

        enc = encrypt_api_key("sk-old", "old-secret")  # 用旧密钥加密
        with pytest.raises(ApiKeyDecryptionError):
            decrypt_api_key(enc, "new-secret")  # 新密钥解不开
        assert (
            decrypt_api_key(
                enc, "new-secret", previous_secrets=["older", "old-secret"]
            )
            == "sk-old"
        )

    def test_undecryptable_token_fails_closed(self) -> None:
        """解密失败必须抛错，不能把密文当明文返回（fail-open 是伪装可用）。"""
        from ariadne.storage.postgres.repositories.model_configs import (
            ApiKeyDecryptionError,
            decrypt_api_key,
        )

        with pytest.raises(ApiKeyDecryptionError, match="无法解密"):
            decrypt_api_key("gAAAAA_not-a-real-token", "secret")

    def test_plaintext_passthrough_unchanged(self) -> None:
        """降级明文（非 token 形态）原样返回。"""
        from ariadne.storage.postgres.repositories.model_configs import (
            decrypt_api_key,
        )

        assert decrypt_api_key("sk-plain", "secret") == "sk-plain"

    def test_fernet_cache_is_per_secret(self) -> None:
        """按密钥缓存：两个密钥并存互不污染（单例缓存时轮换即坏）。"""
        from ariadne.storage.postgres.repositories import model_configs
        from ariadne.storage.postgres.repositories.model_configs import (
            decrypt_api_key,
            encrypt_api_key,
        )

        enc_a = encrypt_api_key("sk-a", "secret-a")
        enc_b = encrypt_api_key("sk-b", "secret-b")
        assert len(model_configs._fernets) >= 2
        assert decrypt_api_key(enc_a, "secret-a") == "sk-a"
        assert decrypt_api_key(enc_b, "secret-b") == "sk-b"

    def test_decrypt_plaintext_fallback(self) -> None:
        """降级写入的明文应能被 decrypt 直接读回。"""
        from ariadne.storage.postgres.repositories.model_configs import (
            decrypt_api_key,
        )

        assert decrypt_api_key("sk-plain-key", "s") == "sk-plain-key"


# ---------- API 端点测试 ----------


class TestModelsAPI:
    def test_create_then_list(self, client: Any, auth: dict[str, str]) -> None:
        # 创建
        resp = client.post(
            "/v1/models",
            json={
                "name": "我的 GPT-4o",
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-test-12345",
                "base_url": "https://api.openai.com",
                "is_default": True,
            },
            headers=auth,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["api_key"] == "sk-test-12345"  # 创建时返回明文
        assert body["api_key_prefix"] == "sk-test-1234"
        assert body["is_default"] is True
        config_id = body["id"]

        # 列表
        resp = client.get("/v1/models", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["models"]) == 1
        item = data["models"][0]
        assert item["id"] == config_id
        assert "api_key" not in item  # 列表不含明文
        assert item["api_key_prefix"] == "sk-test-1234"
        assert data["cryptography_available"] is True

    def test_invalid_provider_rejected(self, client: Any, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/models",
            json={
                "name": "bad",
                "provider": "not-a-provider",
                "model": "x",
                "api_key": "k",
                "base_url": "",
            },
            headers=auth,
        )
        assert resp.status_code == 400
        assert resp.json()["type"].endswith("/bad-request")

    def test_duplicate_name_conflict(self, client: Any, auth: dict[str, str]) -> None:
        payload = {
            "name": "dup",
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "k",
            "base_url": "",
        }
        resp = client.post("/v1/models", json=payload, headers=auth)
        assert resp.status_code == 201
        resp = client.post("/v1/models", json=payload, headers=auth)
        assert resp.status_code == 409
        assert resp.json()["type"].endswith("/conflict")

    def test_default_endpoint(self, client: Any, auth: dict[str, str]) -> None:
        # 无默认配置
        resp = client.get("/v1/models/default", headers=auth)
        assert resp.status_code == 200
        assert resp.json()["found"] is False

        # 创建默认配置
        client.post(
            "/v1/models",
            json={
                "name": "默认",
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "sk-default-xyz",
                "base_url": "https://api.openai.com",
                "is_default": True,
            },
            headers=auth,
        )
        resp = client.get("/v1/models/default", headers=auth)
        assert resp.status_code == 200
        data = resp.json()
        assert data["found"] is True
        assert "api_key" not in data  # 读取接口不得回传 provider 密钥
        assert data["model"] == "gpt-4o"

    def test_update_then_delete(self, client: Any, auth: dict[str, str]) -> None:
        resp = client.post(
            "/v1/models",
            json={
                "name": "to-update",
                "provider": "openai",
                "model": "gpt-4o",
                "api_key": "k1",
                "base_url": "",
            },
            headers=auth,
        )
        config_id = resp.json()["id"]

        # 更新 model + api_key
        resp = client.put(
            f"/v1/models/{config_id}",
            json={"model": "gpt-4o-mini", "api_key": "k2-new"},
            headers=auth,
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == "gpt-4o-mini"
        assert resp.json()["api_key_prefix"] == "k2-new"

        # 删除
        resp = client.delete(f"/v1/models/{config_id}", headers=auth)
        assert resp.status_code == 204

        # 列表为空
        resp = client.get("/v1/models", headers=auth)
        assert len(resp.json()["models"]) == 0

    def test_openai_compatible_provider(self, client: Any, auth: dict[str, str]) -> None:
        """openai_compatible 覆盖 Ollama / vLLM 等网关。"""
        resp = client.post(
            "/v1/models",
            json={
                "name": "本地 Ollama",
                "provider": "openai_compatible",
                "model": "llama3.1",
                "api_key": "ollama-no-key",
                "base_url": "http://localhost:11434/v1",
            },
            headers=auth,
        )
        assert resp.status_code == 201
        assert resp.json()["provider"] == "openai_compatible"


# ---------- resolve_project_llm 测试 ----------


class TestResolveProjectLLM:
    async def test_supports_session_only_tenant_proxy(
        self, memory_pg: Any
    ) -> None:
        """API 的 TenantPg 只有 session() 时仍走项目作用域查询。"""
        from ariadne.runtime_module.llm.resolver import resolve_project_llm

        class SessionOnlyProxy:
            def __init__(self, inner: Any) -> None:
                self._inner = inner

            def session(self) -> Any:
                return self._inner.session()

        class StubClient:
            async def complete(self, prompt: str, *, model: str) -> Any:
                ...

            async def aclose(self) -> None: ...

        env = LlmSettings(provider="openai", model="gpt-4o", api_key=SecretStr(""))
        stub = StubClient()
        client, model = await resolve_project_llm(
            SessionOnlyProxy(memory_pg),
            TEST_PROJECT,
            env_settings=env,
            fallback_client=stub,  # type: ignore[arg-type]
        )
        assert client is stub
        assert model == "gpt-4o"

    async def test_falls_back_to_injected_client(
        self, memory_pg: Any
    ) -> None:
        """无项目配置时回退 fallback_client（保留测试桩/已装配的 env client）。"""
        from ariadne.runtime_module.llm.resolver import resolve_project_llm

        class StubClient:
            async def complete(self, prompt: str, *, model: str) -> Any:
                ...

            async def aclose(self) -> None: ...

        env = LlmSettings(provider="openai", model="gpt-4o", api_key=SecretStr(""))
        stub = StubClient()  # type: ignore[assignment]
        client, model = await resolve_project_llm(
            memory_pg, TEST_PROJECT, env_settings=env, fallback_client=stub  # type: ignore[arg-type]
        )
        assert client is stub
        assert model == "gpt-4o"

    async def test_uses_project_config_when_present(
        self, memory_pg: Any, monkeypatch: Any
    ) -> None:
        """有项目默认配置时用配置装配新 client。"""
        from ariadne.runtime_module.llm import resolver as resolver_mod
        from ariadne.runtime_module.llm.resolver import resolve_project_llm
        from ariadne.storage.postgres.repositories.model_configs import (
            LlmModelConfigRepository,
        )

        monkeypatch.setattr(
            resolver_mod,
            "_encryption_secret",
            lambda: "test-jwt-secret-for-encryption",
        )

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            await repo.create(
                project_id=TEST_PROJECT,
                name="自定义",
                provider="openai",
                model="my-custom-model",
                api_key="sk-custom-real",
                base_url="https://gateway.example.com",
                is_default=True,
                encryption_secret="test-jwt-secret-for-encryption",
            )

        env = LlmSettings(provider="anthropic", model="claude", api_key=SecretStr(""))
        client, model = await resolve_project_llm(
            memory_pg, TEST_PROJECT, env_settings=env, fallback_client=None
        )
        assert model == "my-custom-model"
        # 应该是 OpenAI 适配器（provider=openai），不是 Anthropic
        from ariadne.runtime_module.llm.openai import OpenAILLMClient

        assert isinstance(client, OpenAILLMClient)

    async def test_loads_db_pricing_when_custom_config(
        self, memory_pg: Any, monkeypatch: Any
    ) -> None:
        """自定义模型配置时，从 model_pricing 表读计价表注入 client。

        验证计价的真实接线：upsert 一条自定义价 → resolver 注入的计价表
        按新价查询。若 resolver 没把 DB 计价表传进装配（回退内置），
        抓到的是内置 gpt-4o 价（2.50/10.00），测试会失败。
        """
        from datetime import UTC, datetime
        from decimal import Decimal

        from ariadne.runtime_module.llm import resolver as resolver_mod
        from ariadne.runtime_module.llm.resolver import resolve_project_llm
        from ariadne.storage.postgres.repositories.model_configs import (
            LlmModelConfigRepository,
        )
        from ariadne.storage.postgres.repositories.pricing import PricingRepository
        from ariadne.telemetry.pricing import Price

        monkeypatch.setattr(
            resolver_mod,
            "_encryption_secret",
            lambda: "test-jwt-secret-for-encryption",
        )

        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            repo = LlmModelConfigRepository(session)
            await repo.create(
                project_id=TEST_PROJECT,
                name="自定义",
                provider="openai",
                model="gpt-4o",
                api_key="sk-custom-real",
                base_url="https://gateway.example.com",
                is_default=True,
                encryption_secret="test-jwt-secret-for-encryption",
            )
            await PricingRepository(session).upsert_price(
                provider="openai",
                model="gpt-4o",
                price=Price(
                    input_=Decimal("20.00"),
                    output=Decimal("40.00"),
                    cache_read=Decimal("0"),
                    cache_write=Decimal("0"),
                    reasoning=Decimal("0"),
                ),
            )

        # 捕获 resolver 内部对 build_llm_client 的调用，看传入的定价表
        import ariadne.runtime_module.llm.resolver as resolver_mod

        captured: dict[str, Any] = {}
        original = resolver_mod.build_llm_client

        def fake_build(settings: Any, *, pricing: Any = None) -> Any:
            captured["pricing"] = pricing
            return original(settings, pricing=pricing)

        resolver_mod.build_llm_client = fake_build  # type: ignore[assignment]
        try:
            env = LlmSettings(provider="anthropic", model="claude", api_key=SecretStr(""))
            _client, model = await resolve_project_llm(
                memory_pg, TEST_PROJECT, env_settings=env, fallback_client=None
            )
        finally:
            resolver_mod.build_llm_client = original

        assert model == "gpt-4o"
        assert captured["pricing"] is not None
        # 抓到的计价表应含 DB 里的价（20/40），不是内置 2.5/10
        price = captured["pricing"].lookup("openai", "gpt-4o", datetime.now(UTC))
        assert price is not None
        assert price.input_ == Decimal("20.00")
        assert price.output == Decimal("40.00")

    async def test_undecryptable_config_raises_instead_of_fail_open(
        self, memory_pg: Any, monkeypatch: Any
    ) -> None:
        """api_key 密文解不开 → 报错指明配置坏了，而不是拿密文当 key 装配。

        旧实现回退返回存储值，配出来的 client 带着密文当 key，401 在
        provider 那头才炸 —— fail-open 把配置错误伪装成可用配置。
        """
        import base64

        from ariadne.runtime_module.llm.resolver import resolve_project_llm
        from ariadne.storage.postgres.model_config_models import LlmModelConfig

        # 形态合法（gAAAAA 前缀）但任何密钥都解不开的"密文"
        fake_token = "gAAAAA" + base64.urlsafe_b64encode(b"junk").decode()
        async with memory_pg.tenant_session(TEST_PROJECT) as session:
            row = LlmModelConfig(
                project_id=TEST_PROJECT,
                name="坏配置",
                provider="openai",
                model="gpt-4o",
                api_key_encrypted=fake_token,
                base_url="",
                is_default=True,
                is_active=True,
            )
            session.add(row)

        env = LlmSettings(provider="openai", model="gpt-4o", api_key=SecretStr(""))
        with pytest.raises(ValueError, match="无法解密"):
            await resolve_project_llm(
                memory_pg, TEST_PROJECT, env_settings=env, fallback_client=None
            )
