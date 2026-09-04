"""LLM 模型配置仓储。

所有方法强制带 project_id（多租户隔离应用层防线，RLS 兜底）。

api_key 对称加密存储（Fernet）。加密密钥从 Settings.api.jwt_secret
经 HKDF 派生 —— 不复用 JWT 密钥本身，避免密钥多用途。cryptography
不可用（生产未安装）时降级为明文存储并记录警告，保持功能可用；
安装后切换加密不需迁移（读时按 token 形态自动判断）。

密钥轮换（P2-10）：解密按"主密钥 → previous_secrets"顺序尝试。轮换
流程：把旧 jwt_secret 放进 ARIADNE_API_PREVIOUS_JWT_SECRETS → 换新
jwt_secret → 重启 → 逐条重新保存模型配置（update 会用新密钥重加密）
→ 清掉旧密钥。加密始终只用主密钥。

解密失败的语义是 **fail-closed**：token 形态的密文打不开时抛
ApiKeyDecryptionError，而不是把密文当明文返回 —— 那会把配置错误伪装
成一把"可用的" API key，请求 provider 时才以更难排查的方式失败。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.model_config_models import LlmModelConfig

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


class LlmModelConfigNotFoundError(LookupError):
    pass


class DuplicateLlmModelConfigError(LookupError):
    pass


class ApiKeyDecryptionError(RuntimeError):
    """Fernet 密文无法用任何已知密钥解开。

    单独成类让调用方区分"配置坏了"与"配置丢了"：前者应报警并要求
    用户重新录入 key，后者应走无配置的回退路径。
    """


# ---------- 加密 ----------

# 按 secret 缓存 Fernet 实例。此前是单实例全局缓存 —— 换密钥必须重启，
# 测试也无法用两个密钥并存。按密钥缓存后，主密钥与历史密钥可以共存。
_fernets: dict[str, Fernet] = {}
_cryptography_available = True

# Fernet token 的形态前缀。非此形态视为"降级明文"直接返回。
_TOKEN_PREFIX = "gAAAAA"


def _derive_key(secret: str) -> bytes:
    """从 jwt_secret 派生 Fernet 密钥（SHA-256，info 区分用途）。"""
    derived = hashlib.sha256(
        b"ariadne.llm_model_config.v1\x00" + secret.encode()
    ).digest()
    return base64.urlsafe_b64encode(derived)


def _get_fernet(secret: str) -> Fernet | None:
    """按 secret 惰性构造 Fernet。cryptography 缺失时返回 None（降级明文）。"""
    global _cryptography_available
    if secret in _fernets:
        return _fernets[secret]
    if not _cryptography_available:
        return None
    try:
        from cryptography.fernet import Fernet

        _fernets[secret] = Fernet(_derive_key(secret))
        _cryptography_available = True
        return _fernets[secret]
    except ImportError:
        _cryptography_available = False
        logger.warning(
            "cryptography 未安装，llm_model_config.api_key 将明文存储。"
            "生产环境应安装 cryptography（argon2-cffi 传递依赖通常已有）。"
        )
        return None


def encrypt_api_key(plaintext: str, secret: str) -> str:
    """加密 provider API key。返回 Fernet token 或明文（降级时）。"""
    if not plaintext:
        return ""
    fernet = _get_fernet(secret)
    if fernet is None:
        return plaintext  # 降级：明文存储
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt_api_key(
    stored: str,
    secret: str,
    *,
    previous_secrets: Sequence[str] = (),
) -> str:
    """解密 provider API key。自动识别明文（降级写入的）与 Fernet token。

    尝试顺序：secret（主密钥，即最近一次加密用的）→ previous_secrets
    （轮换期的历史密钥）。全部失败时抛 ApiKeyDecryptionError ——
    返回密文本体会让"配置坏了"伪装成"key 可用"。
    """
    if not stored:
        return ""
    if not stored.startswith(_TOKEN_PREFIX):
        return stored  # 降级明文
    candidates = [secret, *previous_secrets]
    for candidate in candidates:
        fernet = _get_fernet(candidate)
        if fernet is None:
            continue
        try:
            return fernet.decrypt(stored.encode()).decode()
        except Exception:
            continue
    raise ApiKeyDecryptionError(
        "llm_model_config.api_key 无法解密：密钥已轮换且旧密钥不在 "
        "ARIADNE_API_PREVIOUS_JWT_SECRETS 里。请重新录入该配置的 API key"
    )


def is_cryptography_available() -> bool:
    """测试与健康检查用：报告加密是否真正生效。"""
    return _cryptography_available


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


# ---------- 仓储 ----------


class LlmModelConfigRepository:
    """LLM 模型配置读写。所有方法强制带 project_id。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, *, project_id: uuid.UUID) -> list[LlmModelConfig]:
        result = await self._session.execute(
            select(LlmModelConfig)
            .where(
                LlmModelConfig.project_id == project_id,
                LlmModelConfig.is_active.is_(True),
            )
            .order_by(LlmModelConfig.sort_order, LlmModelConfig.created_at)
        )
        return list(result.scalars().all())

    async def get(
        self, *, project_id: uuid.UUID, config_id: uuid.UUID
    ) -> LlmModelConfig | None:
        result = await self._session.execute(
            select(LlmModelConfig).where(
                LlmModelConfig.project_id == project_id,
                LlmModelConfig.id == config_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_default(
        self, *, project_id: uuid.UUID
    ) -> LlmModelConfig | None:
        result = await self._session.execute(
            select(LlmModelConfig).where(
                LlmModelConfig.project_id == project_id,
                LlmModelConfig.is_default.is_(True),
                LlmModelConfig.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        provider: str,
        model: str,
        api_key: str,
        base_url: str,
        degraded_model: str = "",
        is_default: bool = False,
        sort_order: int = 0,
        encryption_secret: str = "",
    ) -> tuple[uuid.UUID, str]:
        """创建配置。返回 (config_id, api_key_prefix)。

        若 is_default=True，先把同项目其他 default 清掉（保证唯一）。
        名称项目内唯一，冲突抛 DuplicateLlmModelConfigError。
        """
        await self._ensure_name_unique(project_id=project_id, name=name)
        if is_default:
            await self._clear_other_defaults(project_id=project_id)

        config = LlmModelConfig(
            project_id=project_id,
            name=name,
            provider=provider,
            model=model,
            api_key_encrypted=encrypt_api_key(api_key, encryption_secret),
            base_url=base_url,
            degraded_model=degraded_model,
            is_default=is_default,
            sort_order=sort_order,
            is_active=True,
        )
        self._session.add(config)
        await self._session.flush()
        return config.id, _prefix(api_key)

    async def update(
        self,
        *,
        project_id: uuid.UUID,
        config_id: uuid.UUID,
        name: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        degraded_model: str | None = None,
        is_default: bool | None = None,
        is_active: bool | None = None,
        sort_order: int | None = None,
        encryption_secret: str = "",
    ) -> str | None:
        """更新配置。api_key 传入非 None 才更新（避免误清空）。

        返回更新后的 api_key_prefix（若未更新 key 则 None）。
        名称变更需保证唯一，冲突抛异常。
        """
        existing = await self.get(project_id=project_id, config_id=config_id)
        if existing is None:
            raise LlmModelConfigNotFoundError(f"模型配置 {config_id} 不存在")

        if name is not None and name != existing.name:
            await self._ensure_name_unique(
                project_id=project_id, name=name, exclude_id=config_id
            )

        if is_default is True:
            await self._clear_other_defaults(
                project_id=project_id, exclude_id=config_id
            )

        values: dict[str, object] = {}
        if name is not None:
            values["name"] = name
        if provider is not None:
            values["provider"] = provider
        if model is not None:
            values["model"] = model
        if base_url is not None:
            values["base_url"] = base_url
        if degraded_model is not None:
            values["degraded_model"] = degraded_model
        if is_default is not None:
            values["is_default"] = is_default
        if is_active is not None:
            values["is_active"] = is_active
        if sort_order is not None:
            values["sort_order"] = sort_order

        new_prefix: str | None = None
        if api_key is not None:
            values["api_key_encrypted"] = encrypt_api_key(api_key, encryption_secret)
            new_prefix = _prefix(api_key)

        if values:
            await self._session.execute(
                update(LlmModelConfig)
                .where(
                    LlmModelConfig.project_id == project_id,
                    LlmModelConfig.id == config_id,
                )
                .values(**values)
            )
        return new_prefix

    async def delete(
        self, *, project_id: uuid.UUID, config_id: uuid.UUID
    ) -> None:
        existing = await self.get(project_id=project_id, config_id=config_id)
        if existing is None:
            raise LlmModelConfigNotFoundError(f"模型配置 {config_id} 不存在")
        await self._session.delete(existing)

    async def touch_last_used(
        self, *, project_id: uuid.UUID, config_id: uuid.UUID
    ) -> None:
        await self._session.execute(
            update(LlmModelConfig)
            .where(
                LlmModelConfig.project_id == project_id,
                LlmModelConfig.id == config_id,
            )
            .values(last_used_at=datetime.now(UTC))
        )

    async def next_sort_order(self, *, project_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.coalesce(func.max(LlmModelConfig.sort_order), -1)).where(
                LlmModelConfig.project_id == project_id
            )
        )
        return int(result.scalar_one()) + 1

    async def _ensure_name_unique(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        exclude_id: uuid.UUID | None = None,
    ) -> None:
        stmt = select(LlmModelConfig.id).where(
            LlmModelConfig.project_id == project_id,
            LlmModelConfig.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(LlmModelConfig.id != exclude_id)
        result = await self._session.execute(stmt)
        if result.scalar_one_or_none() is not None:
            raise DuplicateLlmModelConfigError(f"模型配置名 {name!r} 已存在")

    async def _clear_other_defaults(
        self, *, project_id: uuid.UUID, exclude_id: uuid.UUID | None = None
    ) -> None:
        stmt = (
            update(LlmModelConfig)
            .where(
                LlmModelConfig.project_id == project_id,
                LlmModelConfig.is_default.is_(True),
            )
            .values(is_default=False)
        )
        if exclude_id is not None:
            stmt = stmt.where(LlmModelConfig.id != exclude_id)
        await self._session.execute(stmt)


def _prefix(api_key: str) -> str:
    """展示用前缀（明文 key 的前 12 字符，不足全显）。"""
    return api_key[:12] if api_key else ""


__all__ = [
    "ApiKeyDecryptionError",
    "DuplicateLlmModelConfigError",
    "LlmModelConfigNotFoundError",
    "LlmModelConfigRepository",
    "constant_time_eq",
    "decrypt_api_key",
    "encrypt_api_key",
    "is_cryptography_available",
]
