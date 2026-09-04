"""按项目解析 LLM 客户端：自定义模型配置优先于环境变量默认值。

设计意图（让用户在 UI 里配模型而非只靠 .env）：
- Loop Worker 处理某个 loop 时，该 loop 属于某个 project；
  该 project 可能在设置页配了"默认模型配置"（provider/model/key/base_url）。
- 优先用项目自定义配置装配 LLMClient；无配置时回退到 settings.llm
  （环境变量 / .env），保持旧行为不变。

为什么不直接在 settings 里改：settings 是全局 frozen 单例，而模型配置是
project-scoped 的——多租户下不同项目用不同 provider/key 是核心诉求。
故按 project 解析，而非全局覆盖。
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from ariadne.config import LlmSettings, get_settings
from ariadne.loop_module.engine import LLMClient
from ariadne.runtime_module.llm import build_llm_client
from ariadne.storage.postgres.repositories.model_configs import (
    ApiKeyDecryptionError,
    LlmModelConfigRepository,
    decrypt_api_key,
)
from ariadne.storage.postgres.repositories.pricing import PricingRepository
from ariadne.telemetry.pricing import PricingTable

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _project_session(pg: Any, project_id: uuid.UUID) -> AsyncIterator[Any]:
    """打开项目作用域会话，兼容 Worker store 与 API 的 TenantPg 代理。

    Worker 传入完整的 ``PostgresStore``，必须显式设置 RLS project_id；API
    路由传入的 ``TenantScopedPg`` 已在依赖层绑定租户，只暴露 ``session()``。
    统一在这里分派，避免 Graph 路由为了读模型配置而绕过 RLS。
    """
    tenant_session = getattr(pg, "tenant_session", None)
    if tenant_session is not None:
        async with tenant_session(project_id) as session:
            yield session
        return

    session_factory = getattr(pg, "session", None)
    if session_factory is None:
        raise RuntimeError("Postgres store 没有可用的项目会话")
    async with session_factory() as session:
        yield session


def _encryption_secret() -> str:
    return get_settings().api.jwt_secret.get_secret_value()


def _previous_secrets() -> list[str]:
    """轮换期的历史加密密钥（解密兜底，加密永远用主密钥）。"""
    return list(get_settings().api.previous_jwt_secrets)


async def resolve_project_llm(
    pg: Any,
    project_id: uuid.UUID,
    *,
    env_settings: LlmSettings | None = None,
    fallback_client: LLMClient | None = None,
    pricing: PricingTable | None = None,
) -> tuple[LLMClient, str]:
    """为指定 project 解析 LLMClient。

    返回 (client, model_name)。优先用项目默认模型配置；
    无配置或解密失败时回退 fallback_client（生产传 self._llm），
    fallback_client 为 None 时回退 env_settings（默认 settings.llm）。

    pricing: 预装配的 PricingTable。默认从 model_pricing 表读取 ——
    否则 provider 调价后 Worker 端成本仍按内置硬编码价计算，成本
    归因与 Loop 预算硬熔断都基于错误价格。

    pg: PostgresStore（或带 tenant_session 的代理）。
    """
    env = env_settings or get_settings().llm

    # 查项目默认配置。DB 不可用 / pg 无 tenant_session（测试桩）静默回退 env。
    row = None
    try:
        async with _project_session(pg, project_id) as session:
            repo = LlmModelConfigRepository(session)
            row = await repo.get_default(project_id=project_id)
            # 计价表从库读;读失败由仓储回退内置默认，不影响装配
            if pricing is None:
                pricing = await PricingRepository(session).load()
    except Exception as exc:  # DB 缺失/未迁移不应阻断 Loop
        logger.warning(
            "resolve_project_llm: 读取模型配置失败，回退环境变量",
            extra={"project_id": str(project_id), "error": str(exc)},
        )
        row = None

    if row is None:
        # 回退：优先用调用方注入的 client（保留测试桩/已装配的 env client），
        # 否则从 env_settings 现场装配
        if fallback_client is not None:
            return fallback_client, env.model
        if pricing is None:
            pricing = PricingTable()
        if not env.api_key.get_secret_value():
            raise ValueError(
                "项目没有启用的模型配置，且 ARIADNE_LLM_API_KEY 未配置"
            )
        return build_llm_client(env, pricing=pricing), env.model

    # 从项目配置构造 LlmSettings（frozen，新建实例覆盖）。
    # 解密 fail-closed：密文打不开说明配置坏了（密钥轮换且回填缺失），
    # 必须走无配置回退而不是把密文当 key 用 —— 后者以"provider 401"的
    # 形式在远处爆炸，排查不到根因。
    try:
        plaintext_key = decrypt_api_key(
            row.api_key_encrypted,
            _encryption_secret(),
            previous_secrets=_previous_secrets(),
        )
    except ApiKeyDecryptionError as exc:
        logger.error(
            "resolve_project_llm: 模型配置的 api_key 无法解密，按无配置回退",
            extra={"project_id": str(project_id), "config_id": str(row.id)},
        )
        raise ValueError(f"项目默认模型配置不可用: {exc}") from exc
    base_url = row.base_url or _default_base_url(row.provider, env)
    override = LlmSettings(
        provider=_map_provider(row.provider, env),
        model=row.model,
        degraded_model=row.degraded_model or env.degraded_model,
        api_key=plaintext_key if plaintext_key else env.api_key.get_secret_value(),
        base_url=base_url,
    )

    try:
        client = build_llm_client(override, pricing=pricing)
    except ValueError as exc:
        # provider 非白名单：回退 env，避免 Worker 因单条坏配置整体不可用
        logger.warning(
            "resolve_project_llm: provider 不支持，回退环境变量",
            extra={"project_id": str(project_id), "provider": row.provider, "error": str(exc)},
        )
        if pricing is None:
            pricing = PricingTable()
        return build_llm_client(env, pricing=pricing), env.model

    logger.info(
        "resolve_project_llm: 使用项目自定义模型配置",
        extra={
            "project_id": str(project_id),
            "provider": override.provider,
            "model": override.model,
        },
    )
    return client, override.model


def _map_provider(provider: str, env: LlmSettings) -> str:
    """openai_compatible 映射到 openai 适配器（Chat Completions 协议相同）。"""
    if provider == "openai_compatible":
        return "openai"
    return provider


def _default_base_url(provider: str, env: LlmSettings) -> str:
    """provider 无 base_url 时的默认端点。openai_compatible 必须显式给。"""
    if provider == "anthropic":
        return "https://api.anthropic.com"
    if provider == "openai":
        return "https://api.openai.com"
    # openai_compatible：无官方默认，回退 env 的 base_url（通常是 OpenAI 的）
    return env.base_url


__all__ = ["resolve_project_llm"]
