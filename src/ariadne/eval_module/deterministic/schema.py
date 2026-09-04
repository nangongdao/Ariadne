"""JSON Schema 校验评估器。

用独立的 jsonschema 而非 Pydantic：用户应该能直接提供一份 JSON Schema，
不必为了评测去写 Python 模型。这是 M2 规格里的选型理由。
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from ariadne.eval_module import register_evaluator
from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
    truncate_evidence,
)

MAX_ERRORS_REPORTED = 10


def _extract_json(text: str, *, allow_fenced: bool = True) -> tuple[Any | None, str]:
    """从可能带围栏的文本里取出 JSON。

    模型常把 JSON 包在 ```json 围栏里，直接 json.loads 会失败。
    这属于"格式没完全对但意图明确"，应该容忍而非判失败 ——
    真正该判失败的是 JSON 本身不合法或不符合 schema。

    `allow_fenced=False` 时**不**剥围栏，带围栏的输出直接判解析失败。
    这个参数原先只存在于两个调用方的字段里、从未传到这里：它们写的是
    `ctx.output if self._allow_fenced else ctx.output.strip()`，两个分支
    的差别仅是 strip，而剥围栏在本函数里无条件执行 —— 于是配
    `allow_fenced: false` 与不配完全等价。要求严格 JSON 的场景（下游要直接
    喂给解析器）因此拿到了"围栏也算通过"的判定。
    """
    stripped = text.strip()
    if not stripped:
        return None, "输出为空"

    if stripped.startswith("```"):
        if not allow_fenced:
            return None, "输出被 ``` 围栏包裹，但本项要求严格 JSON"
        lines = stripped.splitlines()
        # 去掉首行围栏（可能带语言标注）与末行围栏
        if len(lines) >= 2:
            body = lines[1:]
            if body and body[-1].strip().startswith("```"):
                body = body[:-1]
            stripped = "\n".join(body).strip()

    try:
        return json.loads(stripped), ""
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败: {exc.msg} (行 {exc.lineno} 列 {exc.colno})"


@register_evaluator("json_schema")
class JsonSchemaEvaluator(BaseEvaluator):
    """校验输出是否为符合给定 Schema 的 JSON。

    这是可信度最高的一类断言：结果二值、无歧义、零成本。
    结构化输出场景应优先用它而非 Judge。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "json_schema",
        *,
        schema: dict[str, Any],
        allow_fenced: bool = True,
    ) -> None:
        super().__init__(name)
        self._schema = schema
        self._allow_fenced = allow_fenced
        self._validator = self._build_validator(schema)

    @staticmethod
    def _build_validator(schema: dict[str, Any]) -> Any:
        """构造时就校验 schema 本身合法，避免每次评测才发现配置写错。"""
        import jsonschema

        validator_cls = jsonschema.validators.validator_for(schema)
        validator_cls.check_schema(schema)
        return validator_cls(schema)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        payload, parse_error = _extract_json(
            ctx.output, allow_fenced=self._allow_fenced
        )

        if parse_error:
            return EvalResult(
                name=self.name, value=0.0, passed=False, evidence=parse_error
            )

        errors = sorted(self._validator.iter_errors(payload), key=lambda e: e.path)
        if not errors:
            return EvalResult(name=self.name, value=1.0, passed=True)

        lines = [
            f"{'/'.join(str(p) for p in err.absolute_path) or '<root>'}: {err.message}"
            for err in errors[:MAX_ERRORS_REPORTED]
        ]
        if len(errors) > MAX_ERRORS_REPORTED:
            lines.append(f"… 另有 {len(errors) - MAX_ERRORS_REPORTED} 处错误")

        return EvalResult(
            name=self.name,
            value=0.0,
            passed=False,
            evidence=truncate_evidence("\n".join(lines)),
        )


@register_evaluator("json_parsable")
class JsonParsableEvaluator(BaseEvaluator):
    """只校验可解析，不校验结构。

    用于"先确认模型能输出合法 JSON"这一步 —— 分开是为了让 critique
    能给出更准确的指令（"JSON 语法错" vs "字段缺失"是不同的修正方向）。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(self, name: str = "json_parsable", *, allow_fenced: bool = True) -> None:
        super().__init__(name)
        self._allow_fenced = allow_fenced

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        _, parse_error = _extract_json(
            ctx.output, allow_fenced=self._allow_fenced
        )
        return EvalResult(
            name=self.name,
            value=0.0 if parse_error else 1.0,
            passed=not parse_error,
            evidence=parse_error,
        )


@register_evaluator("required_fields")
class RequiredFieldsEvaluator(BaseEvaluator):
    """校验 JSON 里必须存在的字段（支持点分路径）。

    比完整 Schema 轻量：很多场景只关心几个关键字段在不在。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self, name: str = "required_fields", *, fields: tuple[str, ...]
    ) -> None:
        super().__init__(name)
        self._fields = fields

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        payload, parse_error = _extract_json(ctx.output)
        if parse_error:
            return EvalResult(
                name=self.name, value=0.0, passed=False, evidence=parse_error
            )

        missing = [path for path in self._fields if not _has_path(payload, path)]
        found = len(self._fields) - len(missing)
        ratio = found / len(self._fields) if self._fields else 1.0

        return EvalResult(
            name=self.name,
            value=ratio,
            passed=not missing,
            evidence="" if not missing else f"缺少字段: {', '.join(missing)}",
        )


def _has_path(payload: Any, path: str) -> bool:
    """点分路径查找。列表索引用数字段表示，如 items.0.name。"""
    node = payload
    for part in path.split("."):
        if isinstance(node, dict):
            if part not in node:
                return False
            node = node[part]
        elif isinstance(node, list):
            if not part.isdigit() or int(part) >= len(node):
                return False
            node = node[int(part)]
        else:
            return False
    return True
