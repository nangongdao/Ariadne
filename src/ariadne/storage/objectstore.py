"""大 payload 外溢存储。

分级策略（阈值见 PayloadSettings）：
  ≤ 8KB   内联存 ClickHouse
  8-32KB  zstd 压缩后内联
  > 32KB  外溢到对象存储，库里只留引用 + 前 512 字节预览

M1 只实现本地后端；S3 后端留 M6（接口已按对象键设计，替换不影响调用方）。
"""

from __future__ import annotations

import contextlib
import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import zstandard

from ariadne.config import PayloadSettings
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_ZSTD_LEVEL: Final = 3
_COMPRESSED_MARK: Final = "zstd:"


@dataclass(frozen=True)
class StoredPayload:
    """加工结果：preview 入库，ref 非空表示内容在对象存储里。"""

    preview: str
    ref: str
    original_bytes: int
    stored_bytes: int


class ObjectStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes) -> str:
        """写入并返回可用于回读的引用。"""

    @abstractmethod
    def get(self, ref: str) -> bytes | None: ...

    @abstractmethod
    def delete(self, ref: str) -> None:
        """删除单个对象。不存在视为成功（幂等）。"""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """删除所有以 prefix 开头的对象，返回删除数量。"""


class LocalObjectStore(ObjectStore):
    """本地文件后端。按日期分目录，避免单目录文件数爆炸。"""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def put(self, key: str, data: bytes) -> str:
        path = self._root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"local://{key}"

    def get(self, ref: str) -> bytes | None:
        if not ref.startswith("local://"):
            return None
        path = self._root / ref.removeprefix("local://")
        return path.read_bytes() if path.is_file() else None

    def delete(self, ref: str) -> None:
        if not ref.startswith("local://"):
            return
        path = self._root / ref.removeprefix("local://")
        if path.is_file():
            path.unlink()

    def delete_prefix(self, prefix: str) -> int:
        """删除 root 下以 prefix 开头的所有文件。"""
        target = self._root / prefix
        if not target.exists():
            return 0
        count = 0
        if target.is_file():
            target.unlink()
            return 1
        for p in target.rglob("*"):
            if p.is_file():
                p.unlink()
                count += 1
        # 清理空目录
        if target.is_dir():
            for p in sorted(target.rglob("*"), reverse=True):
                if p.is_dir():
                    with contextlib.suppress(OSError):
                        p.rmdir()
            with contextlib.suppress(OSError):
                target.rmdir()
        return count


def build_store(settings: PayloadSettings) -> ObjectStore:
    if settings.store_backend == "local":
        return LocalObjectStore(settings.local_dir)
    if settings.store_backend == "s3":
        from ariadne.storage.objectstore_s3 import S3ObjectStore

        return S3ObjectStore(
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
        )
    raise ValueError(f"不支持的 payload 后端: {settings.store_backend}")


class PayloadProcessor:
    """按大小分级处理 payload。

    store_full_payload=False 时只保留 preview_chars 长度的预览，不压缩内联、
    不外溢 —— 数据最小化档，供不允许全文留存的部署使用。此时超阈值 payload
    的全文**不可恢复**，前端展开只能看到预览。
    """

    def __init__(
        self,
        settings: PayloadSettings,
        store: ObjectStore | None = None,
        *,
        store_full_payload: bool = True,
    ) -> None:
        self._settings = settings
        self._store = store or build_store(settings)
        self._compressor = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
        self._store_full = store_full_payload

    def process(
        self, text: str, *, project_id: str, span_id: str, slot: str
    ) -> StoredPayload:
        if not text:
            return StoredPayload("", "", 0, 0)

        raw = text.encode("utf-8")
        size = len(raw)

        if size <= self._settings.inline_max_bytes:
            return StoredPayload(text, "", size, size)

        if not self._store_full:
            # 数据最小化：超过内联阈值即截断，全文不留存
            return StoredPayload(text[: self._settings.preview_chars], "", size, 0)

        if size <= self._settings.compress_max_bytes:
            compressed = self._compressor.compress(raw)
            # 压缩后仍内联，但用标记前缀让读取侧知道要解压
            encoded = _COMPRESSED_MARK + compressed.hex()
            return StoredPayload(encoded, "", size, len(encoded))

        # 超阈值：外溢
        digest = hashlib.sha256(raw).hexdigest()[:16]
        day = datetime.now(UTC).strftime("%Y/%m/%d")
        key = f"{project_id}/{day}/{span_id}.{slot}.{digest}.zst"
        compressed = self._compressor.compress(raw)
        try:
            ref = self._store.put(key, compressed)
        except OSError as exc:
            # 外溢失败不能丢整条 span，降级为只留预览
            logger.error("payload spill failed", extra={"key": key, "error": str(exc)})
            return StoredPayload(text[: self._settings.preview_chars], "", size, 0)

        return StoredPayload(
            preview=text[: self._settings.preview_chars],
            ref=ref,
            original_bytes=size,
            stored_bytes=len(compressed),
        )

    def read(self, preview: str, ref: str) -> str:
        """回读全文：优先对象存储，其次解压内联标记，最后回退预览。"""
        if ref:
            data = self._store.get(ref)
            if data is not None:
                return zstandard.ZstdDecompressor().decompress(data).decode("utf-8")
        if preview.startswith(_COMPRESSED_MARK):
            blob = bytes.fromhex(preview.removeprefix(_COMPRESSED_MARK))
            return zstandard.ZstdDecompressor().decompress(blob).decode("utf-8")
        return preview
