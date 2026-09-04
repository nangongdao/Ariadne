"""对象存储 delete/delete_prefix 测试（LocalObjectStore 后端）。

S3 后端的集成测试需容器环境，不在此测。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.storage.objectstore import LocalObjectStore


class TestLocalObjectStoreDelete:
    def test_delete_existing_object(self, tmp_path: Path) -> None:
        store = LocalObjectStore(str(tmp_path))
        ref = store.put("proj1/file.txt", b"hello")
        assert store.get(ref) == b"hello"
        store.delete(ref)
        assert store.get(ref) is None

    def test_delete_nonexistent_is_idempotent(self, tmp_path: Path) -> None:
        store = LocalObjectStore(str(tmp_path))
        # 不存在的 ref 不报错
        store.delete("local://nonexistent/path")
        store.delete("not-a-local-ref")

    def test_delete_prefix_removes_all(self, tmp_path: Path) -> None:
        store = LocalObjectStore(str(tmp_path))
        store.put("proj1/2026/01/file1.txt", b"data1")
        store.put("proj1/2026/01/file2.txt", b"data2")
        store.put("proj1/2026/02/file3.txt", b"data3")
        store.put("proj2/2026/01/file4.txt", b"data4")

        count = store.delete_prefix("proj1/")
        assert count == 3
        # proj1 下全删
        assert store.get("local://proj1/2026/01/file1.txt") is None
        assert store.get("local://proj1/2026/02/file3.txt") is None
        # proj2 不受影响
        assert store.get("local://proj2/2026/01/file4.txt") == b"data4"

    def test_delete_prefix_nonexistent_returns_zero(self, tmp_path: Path) -> None:
        store = LocalObjectStore(str(tmp_path))
        assert store.delete_prefix("nonexistent/") == 0

    def test_delete_prefix_single_file(self, tmp_path: Path) -> None:
        store = LocalObjectStore(str(tmp_path))
        store.put("proj1/file.txt", b"data")
        count = store.delete_prefix("proj1/file.txt")
        assert count == 1
        assert store.get("local://proj1/file.txt") is None


class TestBuildStoreDispatch:
    def test_local_backend(self) -> None:
        from ariadne.config import PayloadSettings
        from ariadne.storage.objectstore import LocalObjectStore, build_store

        settings = PayloadSettings(store_backend="local", local_dir="/tmp/test")
        store = build_store(settings)
        assert isinstance(store, LocalObjectStore)

    def test_s3_backend_imports(self) -> None:
        """S3 后端应能被工厂构造（不实际连接）。"""
        from ariadne.config import PayloadSettings
        from ariadne.storage.objectstore import build_store

        # 构造即可——不调用任何 S3 API
        settings = PayloadSettings(
            store_backend="s3", s3_bucket="test-bucket", s3_prefix="payloads"
        )
        # S3ObjectStore.__init__ 会调用 boto3.client("s3")
        # 这里只验证工厂能分发到 S3 路径
        store = build_store(settings)
        assert store.__class__.__name__ == "S3ObjectStore"

    def test_unsupported_backend_raises(self) -> None:
        from ariadne.config import PayloadSettings
        from ariadne.storage.objectstore import build_store

        settings = PayloadSettings.model_construct(store_backend="gcs")
        with pytest.raises(ValueError):
            build_store(settings)
