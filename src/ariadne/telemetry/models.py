"""内部 Span 契约。

这是平台的稳定契约层：上游 OTel GenAI semconv / OpenInference 的属性名变动
只影响 adapters/ 中的映射，不影响此处字段名与存储列名。
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEX32 = r"^[0-9a-f]{32}$"
HEX16 = r"^[0-9a-f]{16}$"

MAX_PREVIEW_CHARS = 2048
MAX_ATTRIBUTE_COUNT = 128
MAX_ATTRIBUTE_VALUE_CHARS = 1024


class SpanKind(StrEnum):
    """Span 的业务类别，决定前端图标与可用的下钻视图。"""

    LLM = "llm"
    TOOL = "tool"
    RAG = "rag"
    CODE = "code"
    LOOP = "loop"
    HARNESS = "harness"
    EVAL = "eval"
    INTERNAL = "internal"


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"


class TokenUsage(BaseModel):
    """Token 用量。

    缓存与推理 Token 必须与普通 Token 分开计量：多轮场景缓存命中率高，
    按标准价折算会显著高估成本，进而导致预算熔断误触发。
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class AriadneSpan(BaseModel):
    """归一化后的 Span，与 ClickHouse spans 表列一一对应。"""

    model_config = ConfigDict(frozen=True)

    project_id: UUID
    trace_id: Annotated[str, Field(pattern=HEX32)]
    span_id: Annotated[str, Field(pattern=HEX16)]
    parent_span_id: str = ""

    name: str = Field(min_length=1, max_length=256)
    kind: SpanKind = SpanKind.INTERNAL
    operation: str = ""
    provider: str = ""
    model_request: str = ""
    model_response: str = ""

    started_at: datetime
    duration_ms: int = Field(default=0, ge=0)
    status: SpanStatus = SpanStatus.OK
    error_type: str = ""

    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: Decimal = Field(default=Decimal("0"), ge=0)

    # M3 启用，M1 预留以避免后续改表
    loop_id: str = ""
    iteration: int = Field(default=0, ge=0)
    failure_fp: str = ""

    input_preview: str = ""
    output_preview: str = ""
    input_ref: str = ""
    output_ref: str = ""

    attributes: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    @field_validator("parent_span_id")
    @classmethod
    def _check_parent(cls, v: str) -> str:
        import re

        if v and not re.match(HEX16, v):
            raise ValueError("parent_span_id 必须是 16 位小写 hex 或空字符串")
        return v

    @field_validator("input_preview", "output_preview")
    @classmethod
    def _truncate_preview(cls, v: str) -> str:
        return v[:MAX_PREVIEW_CHARS]

    @field_validator("attributes")
    @classmethod
    def _cap_attributes(cls, v: dict[str, str]) -> dict[str, str]:
        # 防御恶意/失控的属性膨胀打爆存储
        capped = dict(list(v.items())[:MAX_ATTRIBUTE_COUNT])
        return {k: str(val)[:MAX_ATTRIBUTE_VALUE_CHARS] for k, val in capped.items()}


class RawSpanBatch(BaseModel):
    """入队载荷：适配前的原始批次。"""

    model_config = ConfigDict(frozen=True)

    format: str
    project_id: UUID
    received_at: datetime
    payload: list[dict[str, object]]
