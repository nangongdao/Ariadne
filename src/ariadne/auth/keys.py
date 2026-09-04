"""API Key 哈希校验 —— Argon2id。

M6 §6 选择 argon2-cffi：抗 GPU 暴破，优于 bcrypt。

安全设计：
- 明文 key 仅在创建时返回一次，之后只存 Argon2id 哈希
- key_prefix（前 16 字符）用于列表展示和索引查找
  （哈希不可逆查，所以需要一个可查的字段）
- 验证用 argon2 的 verify，内部做定时安全比较
"""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from ariadne.config import get_settings

_KEY_PREFIX = "ak_live_"
_PREFIX_LEN = 16  # ak_live_ + 7 字符明文 = 16 字符


def _get_hasher() -> PasswordHasher:
    """从配置构建 Argon2id hasher。"""
    api = get_settings().api
    return PasswordHasher(
        memory_cost=api.argon2_memory_cost,
        time_cost=api.argon2_time_cost,
        parallelism=api.argon2_parallelism,
    )


def generate_api_key() -> str:
    """生成新的 API Key 明文。

    格式: ak_live_ + 32 字节 URL-safe base64。
    """
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def hash_api_key(plain: str) -> str:
    """Argon2id 哈希。返回自描述格式（含盐 + 参数）。"""
    hasher = _get_hasher()
    return hasher.hash(plain)


def verify_api_key(plain: str, hash: str) -> bool:
    """验证 API Key。定时安全（Argon2 内部实现）。"""
    hasher = _get_hasher()
    try:
        hasher.verify(hash, plain)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        # 损坏的 hash 或其他异常 —— 视为不匹配
        return False


def extract_prefix(plain: str) -> str:
    """提取 key 的展示前缀（前 16 字符）。

    用于列表展示（如 ak_live_abc1234…）和索引查找。
    """
    return plain[:_PREFIX_LEN]


def needs_rehash(hash: str) -> bool:
    """检查 hash 是否需要重新计算（参数已变更）。"""
    hasher = _get_hasher()
    return hasher.check_needs_rehash(hash)


__all__ = [
    "extract_prefix",
    "generate_api_key",
    "hash_api_key",
    "needs_rehash",
    "verify_api_key",
]
