"""数据集与版本化。

`content_hash` 是复现的锚点：实验记录里存 (dataset_id, version, content_hash)，
任何人拿到这三个值都能确认自己的数据集与原实验完全一致。

哈希必须对**样本顺序不敏感** —— 同一组样本换个顺序仍是同一数据集，
否则导出再导入会得到不同的 hash，复现校验形同虚设。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

HASH_PREFIX_LEN = 16


@dataclass(frozen=True)
class DatasetItem:
    """一条样本。"""

    item_id: str
    input: str
    expected: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def content_key(self) -> str:
        """参与哈希的规范化表示。

        metadata 的键排序后序列化 —— dict 顺序不该影响 hash。
        """
        return json.dumps(
            {
                "item_id": self.item_id,
                "input": self.input,
                "expected": self.expected,
                "metadata": dict(sorted(self.metadata.items())),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def compute_content_hash(items: Iterable[DatasetItem]) -> str:
    """样本集的内容哈希。对顺序不敏感。"""
    keys = sorted(item.content_key() for item in items)
    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode("utf-8"))
        digest.update(b"\x00")  # 分隔符防止拼接歧义
    return digest.hexdigest()[:HASH_PREFIX_LEN]


class DuplicateItemIdError(ValueError):
    """同一数据集内 item_id 重复。

    必须拒绝而非静默去重：重复 id 会让样本级 diff 对不上，
    而那是发现"一半变好一半变差"的唯一手段。
    """


@dataclass(frozen=True)
class Dataset:
    """不可变的数据集版本。"""

    dataset_id: str
    name: str
    version: int
    items: tuple[DatasetItem, ...]
    content_hash: str
    description: str = ""

    @classmethod
    def create(
        cls,
        *,
        dataset_id: str,
        name: str,
        version: int,
        items: Sequence[DatasetItem],
        description: str = "",
    ) -> Dataset:
        if not items:
            raise ValueError("数据集不能为空")

        seen: set[str] = set()
        duplicates: list[str] = []
        for item in items:
            if item.item_id in seen:
                duplicates.append(item.item_id)
            seen.add(item.item_id)
        if duplicates:
            raise DuplicateItemIdError(
                f"数据集 {name!r} 存在重复 item_id: {sorted(set(duplicates))}"
            )

        return cls(
            dataset_id=dataset_id,
            name=name,
            version=version,
            items=tuple(items),
            content_hash=compute_content_hash(items),
            description=description,
        )

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Iterator[DatasetItem]:
        return iter(self.items)

    @property
    def ref(self) -> str:
        """实验记录里引用数据集的规范形式。"""
        return f"{self.name}@v{self.version}#{self.content_hash}"

    def verify(self) -> bool:
        """校验内容哈希是否与样本一致（检测被篡改的持久化数据）。"""
        return compute_content_hash(self.items) == self.content_hash

    def slice_by(self, key: str, value: str) -> tuple[DatasetItem, ...]:
        """按 metadata 分片。用于"按难度/领域分别看指标"。"""
        return tuple(i for i in self.items if i.metadata.get(key) == value)

    def slice_keys(self, key: str) -> tuple[str, ...]:
        return tuple(
            sorted({i.metadata[key] for i in self.items if key in i.metadata})
        )


def parse_jsonl(lines: Iterable[str]) -> list[DatasetItem]:
    """从 JSONL 解析样本。

    容错策略与遥测适配层相反：这里**不容错**。数据集是实验的基准，
    静默跳过坏行会导致"两次实验用的其实不是同一数据集"。
    """
    items: list[DatasetItem] = []
    for line_no, raw in enumerate(lines, start=1):
        text = raw.strip()
        if not text or text.startswith("//"):
            continue
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_no} 行 JSON 解析失败: {exc.msg}") from exc

        if not isinstance(payload, dict):
            raise ValueError(f"第 {line_no} 行不是对象")
        if "input" not in payload:
            raise ValueError(f"第 {line_no} 行缺少 input 字段")

        items.append(
            DatasetItem(
                item_id=str(payload.get("item_id") or f"item-{line_no}"),
                input=str(payload["input"]),
                expected=(
                    str(payload["expected"]) if payload.get("expected") is not None
                    else None
                ),
                metadata={
                    str(k): str(v) for k, v in (payload.get("metadata") or {}).items()
                },
            )
        )

    if not items:
        raise ValueError("JSONL 中没有有效样本")
    return items


def to_jsonl(dataset: Dataset) -> str:
    """导出为 JSONL。往返转换后 content_hash 必须不变。"""
    lines = [
        json.dumps(
            {
                "item_id": item.item_id,
                "input": item.input,
                **({"expected": item.expected} if item.expected is not None else {}),
                **({"metadata": item.metadata} if item.metadata else {}),
            },
            ensure_ascii=False,
        )
        for item in dataset.items
    ]
    return "\n".join(lines) + "\n"
