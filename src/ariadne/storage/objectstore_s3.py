"""S3 兼容对象存储后端。

M1 用 LocalObjectStore，M6 Week 3 替换为 S3。
key 以 `{project_id}/` 为前缀（第四层隔离），GDPR 删除按前缀清理。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mypy_boto3_s3 import S3Client

if TYPE_CHECKING:
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

from ariadne.storage.objectstore import ObjectStore

logger = logging.getLogger(__name__)


class S3ObjectStore(ObjectStore):
    """S3 / MinIO 对象存储后端。

    用 boto3 同步客户端。写入路径在 worker 侧（PayloadProcessor.process），
    非热路径 —— 采集热路径只写 ClickHouse，大 payload 外溢已是异步。
    """

    def __init__(self, bucket: str, prefix: str = "payloads") -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._client: S3Client = self._make_client()

    def _make_client(self) -> S3Client:
        import boto3

        return boto3.client("s3")

    def _full_key(self, key: str) -> str:
        if self._prefix:
            return f"{self._prefix}/{key}"
        return key

    def put(self, key: str, data: bytes) -> str:
        s3_key = self._full_key(key)
        self._client.put_object(Bucket=self._bucket, Key=s3_key, Body=data)
        return f"s3://{self._bucket}/{s3_key}"

    def get(self, ref: str) -> bytes | None:
        if not ref.startswith("s3://"):
            return None
        rest = ref.removeprefix("s3://")
        slash = rest.find("/")
        if slash < 0:
            return None
        bucket = rest[:slash]
        key = rest[slash + 1 :]
        try:
            resp = self._client.get_object(Bucket=bucket, Key=key)
            body: Any = resp["Body"]
            data: bytes = body.read()
            return data
        except Exception:
            logger.warning("s3 get failed", extra={"ref": ref})
            return None

    def delete(self, ref: str) -> None:
        if not ref.startswith("s3://"):
            return
        rest = ref.removeprefix("s3://")
        slash = rest.find("/")
        if slash < 0:
            return
        bucket = rest[:slash]
        key = rest[slash + 1 :]
        self._client.delete_object(Bucket=bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        """删除 S3 中以 prefix 开头的所有对象。

        S3 没有目录概念，需要 list + batch delete（每批 ≤ 1000）。
        """
        s3_prefix = self._full_key(prefix)
        count = 0
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=s3_prefix):
            objects = page.get("Contents", [])
            if not objects:
                continue
            keys: list[ObjectIdentifierTypeDef] = [{"Key": obj["Key"]} for obj in objects]
            self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": keys, "Quiet": True},
            )
            count += len(keys)
        return count


__all__ = ["S3ObjectStore"]
