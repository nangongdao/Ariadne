"""实验结果的文件持久化。

存在的理由：CI 门禁需要"跑 baseline → 存 → 跑 current → 对比"，
而 Postgres 在 M2 后半段才接入。用 JSON 文件先把门禁链路打通，
接入数据库后这层仍有用（CI artifact 传递、离线复现）。

格式带 schema_version：后续字段变更时能给出明确的不兼容提示，
而非在反序列化时抛一个看不懂的 KeyError。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ariadne.eval_module.base import EvalResult, Severity, Violation
from ariadne.experiment.runner import ExperimentResult, ItemOutcome

SCHEMA_VERSION = 1


class SchemaVersionError(ValueError):
    """持久化格式版本不兼容。"""


@dataclass(frozen=True)
class PersistedMeta:
    """随结果一起存的元数据。复现实验需要这些。"""

    judge_models: tuple[str, ...] = ()
    note: str = ""


def _result_to_dict(result: EvalResult) -> dict[str, Any]:
    return {
        "name": result.name,
        "value": result.value,
        "passed": result.passed,
        "evidence": result.evidence,
        "violations": [
            {
                "dimension": v.dimension,
                "span": v.span,
                "severity": v.severity.value,
                "detail": v.detail,
            }
            for v in result.violations
        ],
        "judge_model": result.judge_model,
        "duration_ms": result.duration_ms,
        "cost_usd": str(result.cost_usd),
        "errored": result.errored,
    }


def _result_from_dict(payload: dict[str, Any]) -> EvalResult:
    return EvalResult(
        name=str(payload["name"]),
        value=float(payload["value"]),
        passed=bool(payload["passed"]),
        evidence=str(payload.get("evidence", "")),
        violations=tuple(
            Violation(
                dimension=str(v.get("dimension", "")),
                span=str(v.get("span", "")),
                severity=Severity(str(v.get("severity", "medium"))),
                detail=str(v.get("detail", "")),
            )
            for v in payload.get("violations", [])
        ),
        judge_model=str(payload.get("judge_model", "")),
        duration_ms=int(payload.get("duration_ms", 0)),
        cost_usd=Decimal(str(payload.get("cost_usd", "0"))),
        errored=bool(payload.get("errored", False)),
    )


def to_dict(result: ExperimentResult, meta: PersistedMeta | None = None) -> dict[str, Any]:
    resolved = meta or PersistedMeta()
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": result.experiment_id,
        "dataset_ref": result.dataset_ref,
        "config_label": result.config_label,
        # 存 Judge 模型：版本切换是破坏性变更，历史分数不可与新分数直接比
        "judge_models": list(resolved.judge_models) or _collect_judge_models(result),
        "note": resolved.note,
        "outcomes": [
            {
                "item_id": o.item_id,
                "output": o.output,
                "results": [_result_to_dict(r) for r in o.results],
                "composite_score": o.composite_score,
                "passed": o.passed,
                "cost_usd": str(o.cost_usd),
                "duration_ms": o.duration_ms,
                "generation_error": o.generation_error,
                "metadata": o.metadata,
            }
            for o in result.outcomes
        ],
    }


def _collect_judge_models(result: ExperimentResult) -> list[str]:
    return sorted(
        {r.judge_model for o in result.outcomes for r in o.results if r.judge_model}
    )


def from_dict(payload: dict[str, Any]) -> ExperimentResult:
    version = int(payload.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"实验结果格式版本为 {version}，当前支持 {SCHEMA_VERSION}。"
            "请用同版本的 Ariadne 重跑 baseline。"
        )

    outcomes = tuple(
        ItemOutcome(
            item_id=str(o["item_id"]),
            output=str(o.get("output", "")),
            results=tuple(_result_from_dict(r) for r in o.get("results", [])),
            composite_score=float(o.get("composite_score", 0.0)),
            passed=bool(o.get("passed", False)),
            cost_usd=Decimal(str(o.get("cost_usd", "0"))),
            duration_ms=int(o.get("duration_ms", 0)),
            generation_error=str(o.get("generation_error", "")),
            metadata={str(k): str(v) for k, v in (o.get("metadata") or {}).items()},
        )
        for o in payload.get("outcomes", [])
    )

    return ExperimentResult(
        experiment_id=str(payload.get("experiment_id", "")),
        dataset_ref=str(payload.get("dataset_ref", "")),
        config_label=str(payload.get("config_label", "")),
        outcomes=outcomes,
    )


def save(result: ExperimentResult, path: Path, meta: PersistedMeta | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(to_dict(result, meta), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load(path: Path) -> ExperimentResult:
    if not path.is_file():
        raise FileNotFoundError(f"实验结果文件不存在: {path}")
    return from_dict(json.loads(path.read_text(encoding="utf-8")))


def judge_models_of(path: Path) -> tuple[str, ...]:
    """读取 Judge 模型列表而不反序列化全部结果。

    用于对比前校验两侧 Judge 版本是否一致 —— 不一致时分数不可比。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(str(m) for m in payload.get("judge_models", []))
