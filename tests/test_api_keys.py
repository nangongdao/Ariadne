"""API Key 哈希校验 + 仓库测试。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ariadne.auth.keys import (
    extract_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from ariadne.storage.postgres.repositories.api_keys import (
    ApiKeyNotFoundError,
    ApiKeyRepository,
)

TEST_PROJECT = uuid.UUID("00000000-0000-0000-0000-000000000001")


class TestKeyHashing:
    def test_generate_key_has_prefix(self) -> None:
        key = generate_api_key()
        assert key.startswith("ak_live_")

    def test_generate_key_unique(self) -> None:
        key1 = generate_api_key()
        key2 = generate_api_key()
        assert key1 != key2

    def test_hash_and_verify(self) -> None:
        key = generate_api_key()
        hashed = hash_api_key(key)
        assert hashed != key
        assert verify_api_key(key, hashed)

    def test_verify_wrong_key(self) -> None:
        key = generate_api_key()
        wrong_key = generate_api_key()
        hashed = hash_api_key(key)
        assert not verify_api_key(wrong_key, hashed)

    def test_verify_corrupted_hash(self) -> None:
        key = generate_api_key()
        assert not verify_api_key(key, "corrupted-hash")

    def test_extract_prefix(self) -> None:
        key = "ak_live_abcdefghijklmnop"
        prefix = extract_prefix(key)
        assert prefix == "ak_live_abcdefgh"
        assert len(prefix) == 16

    def test_hash_is_argon2id(self) -> None:
        key = generate_api_key()
        hashed = hash_api_key(key)
        # Argon2id hash 以 $argon2id$ 开头
        assert hashed.startswith("$argon2id$")


class TestApiKeyRepository:
    async def test_create_key_returns_plain(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            key_id, plain_key = await repo.create(
                project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
                name="test-key",
                role="admin",
            )
            assert isinstance(key_id, uuid.UUID)
            assert plain_key.startswith("ak_live_")
            await session.commit()

    async def test_get_by_prefix(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            key_id, plain_key = await repo.create(
                project_id=TEST_PROJECT,
                name="test-key",
                role="viewer",
            )
            await session.flush()

            prefix = extract_prefix(plain_key)
            found = await repo.get_by_prefix(
                project_id=TEST_PROJECT, key_prefix=prefix
            )
            assert found is not None
            assert found.id == key_id
            assert found.role == "viewer"
            await session.commit()

    async def test_list_keys(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            await repo.create(project_id=TEST_PROJECT, name="key1", role="viewer")
            await repo.create(project_id=TEST_PROJECT, name="key2", role="admin")
            await session.flush()

            keys = await repo.list(project_id=TEST_PROJECT)
            assert len(keys) == 2
            names = {k.name for k in keys}
            assert names == {"key1", "key2"}
            await session.commit()

    async def test_revoke_key(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            key_id, _ = await repo.create(
                project_id=TEST_PROJECT, name="to-revoke", role="admin"
            )
            await session.flush()

            await repo.revoke(project_id=TEST_PROJECT, key_id=key_id)
            await session.flush()

            found = await repo.get_by_id(project_id=TEST_PROJECT, key_id=key_id)
            assert found is not None
            assert found.is_active is False
            await session.commit()

    async def test_revoke_nonexistent_raises(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            with pytest.raises(ApiKeyNotFoundError):
                await repo.revoke(
                    project_id=TEST_PROJECT,
                    key_id=uuid.uuid4(),
                )

    async def test_get_by_prefix_inactive_not_found(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            key_id, plain_key = await repo.create(
                project_id=TEST_PROJECT, name="test", role="viewer"
            )
            await session.flush()
            await repo.revoke(project_id=TEST_PROJECT, key_id=key_id)
            await session.flush()

            prefix = extract_prefix(plain_key)
            found = await repo.get_by_prefix(
                project_id=TEST_PROJECT, key_prefix=prefix
            )
            assert found is None  # 吊销后查不到
            await session.commit()

    async def test_is_expired_not_expired(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            _, _ = await repo.create(project_id=TEST_PROJECT, name="test", role="viewer")
            await session.flush()
            keys = await repo.list(project_id=TEST_PROJECT)
            key = keys[0]
            assert not await repo.is_expired(key)
            await session.commit()

    async def test_is_expired_with_future_expiry(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            future = datetime.now(UTC) + timedelta(days=7)
            await repo.create(
                project_id=TEST_PROJECT,
                name="test",
                role="viewer",
                expires_at=future,
            )
            await session.flush()
            keys = await repo.list(project_id=TEST_PROJECT)
            assert not await repo.is_expired(keys[0])
            await session.commit()

    async def test_is_expired_with_past_expiry(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            repo = ApiKeyRepository(session)
            past = datetime.now(UTC) - timedelta(days=1)
            await repo.create(
                project_id=TEST_PROJECT,
                name="test",
                role="viewer",
                expires_at=past,
            )
            await session.flush()
            keys = await repo.list(project_id=TEST_PROJECT)
            assert await repo.is_expired(keys[0])
            await session.commit()
