"""配置字典 → 评估器实例，走 EVALUATOR_REGISTRY 而非 if-chain。

R12 第七实例，但失效方式和前六个不同：这次**有**生产调用路径，
路径本身却是一条写死 10 个分支的 if-chain（原
`build_deterministic_evaluator`），而注册表里有 14 个评估器。于是
`rouge_l` / `token_f1` / `edit_distance` / `judge` 四个实现完整、单测
全绿的评估器在生产侧不可达 —— 注册表被短路了。

改成注册表驱动后这类漂移不可能再发生：新评估器加上 @register_evaluator
就自动可配，不需要记得同时改工厂。

第二类缺陷同时修掉：if-chain 只往构造器传了部分参数，其余**静默丢弃**。
`regex` 的 must_match / flags / target、`exact_match` 的 case_sensitive、
`json_schema` 的 allow_fenced、`markdown_structure` 的全部三个参数都是
配了不生效。`{"type": "regex", "pattern": "TODO", "must_match": false}`
本意是"不许出现 TODO"，旧代码会把它变成"必须出现 TODO"—— 断言反向，
且没有任何报错。所以这里对未知键 fail-fast，而不是忽略。

参数取值按构造器签名的默认值类型强转（配置来自 JSON，数字可能是字符串、
tuple 一定是 list）。`threshold` / `op` 两个键有歧义：既是 ScoreSpec 的
编排参数，也是部分评估器的构造参数 —— 见 _CONSTRUCTOR_SKIP 的说明。
"""

from __future__ import annotations

import inspect
import re
from enum import Enum
from typing import Any

from ariadne.eval_module import EVALUATOR_REGISTRY, EvaluatorFactory, available_evaluators
from ariadne.eval_module.base import BaseEvaluator, EvaluatorKind

# 这些键属于 ScoreSpec（复合分编排），永不传给评估器构造器。
# `threshold` / `op` 刻意不在此列：numeric_range 的 threshold 是数值边界、
# rouge_l 的 threshold 是相似度下限，都必须进构造器。两处同时消费同一个
# 键是有意的 —— 语义一致（都是"该项达标线"），不会互相矛盾。
_CONSTRUCTOR_SKIP = frozenset({"type", "name", "weight", "normalize_max"})

# 历史别名：旧配置里 citation_count 用的是 min_citations
_ALIASES: dict[str, dict[str, str]] = {"citation_count": {"min_citations": "min_count"}}


class JudgeNeedsClientError(ValueError):
    """judge 无法从纯配置构造 —— 它需要注入一个 JudgeClient。

    单独成类而不是笼统报 ValueError：调用方（实验配置校验）要能区分
    "写错了类型名"和"这个类型对但得走另一条装配路径"。
    """


# re 模块的编译标志白名单。JSON 配不出 `re.IGNORECASE`，而 flags 的类型是
# int —— 只能写 `"flags": 2`，没人记得 2 是哪个。所以接受名字列表。
# 白名单而非 getattr(re, name)：后者能取到 re 模块里任何属性。
_REGEX_FLAGS: dict[str, re.RegexFlag] = {
    "IGNORECASE": re.IGNORECASE,
    "MULTILINE": re.MULTILINE,
    "DOTALL": re.DOTALL,
    "VERBOSE": re.VERBOSE,
    "ASCII": re.ASCII,
    "UNICODE": re.UNICODE,
}


def _coerce_regex_flags(value: Any) -> int:
    """["IGNORECASE", "MULTILINE"] → re.IGNORECASE | re.MULTILINE。"""
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    names = [value] if isinstance(value, str) else list(value)
    flags = 0
    for raw in names:
        key = str(raw).upper().removeprefix("RE.")
        if key not in _REGEX_FLAGS:
            known = ", ".join(sorted(_REGEX_FLAGS))
            raise ValueError(f"未知的正则标志 {raw!r}，可用: {known}")
        flags |= int(_REGEX_FLAGS[key])
    return flags


def _coerce(value: Any, param: inspect.Parameter) -> Any:
    """按构造器参数的默认值/注解把 JSON 值转成期望类型。"""
    default = param.default
    annotation = str(param.annotation)

    if param.name == "flags":
        return _coerce_regex_flags(value)
    if isinstance(default, Enum):
        return type(default)(value)
    if "tuple" in annotation:
        return tuple(value) if isinstance(value, (list, tuple)) else (value,)
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if "int" in annotation and "None" in annotation and value is not None:
        return int(value)
    return value


def build_evaluator_from_config(config: dict[str, Any]) -> BaseEvaluator:
    """从配置字典构建任意已注册的评估器。

    config["type"] 取注册表名，其余键按构造器签名传入。未知类型与未知
    参数键都 fail-fast：实验配置写错要立刻暴露，而不是跑完才发现某项
    没测（或按反向语义测了）。
    """
    etype = str(config.get("type", ""))
    known = ", ".join(available_evaluators())
    if etype not in EVALUATOR_REGISTRY and etype not in available_evaluators():
        raise ValueError(f"不支持的评估器类型 {etype!r}，已注册: {known}")

    cls = EVALUATOR_REGISTRY[etype]
    params = inspect.signature(cls.__init__).parameters
    if "client" in params:
        raise JudgeNeedsClientError(
            f"评估器 {etype!r} 需要注入 client，不能从实验配置直接构造"
        )

    aliases = _ALIASES.get(etype, {})
    kwargs: dict[str, Any] = {"name": str(config.get("name", etype))}
    for raw_key, value in config.items():
        key = aliases.get(raw_key, raw_key)
        if raw_key in _CONSTRUCTOR_SKIP:
            continue
        if key not in params:
            accepted = ", ".join(sorted(k for k in params if k != "self"))
            raise ValueError(
                f"评估器 {etype!r} 不接受参数 {raw_key!r}（可用: {accepted}）"
            )
        kwargs[key] = _coerce(value, params[key])

    return EvaluatorFactory(etype, **kwargs)


# 可以从纯配置构造、且不产生 LLM 调用的 kind。
# statistical 在列的理由：rouge_l / token_f1 / edit_distance 都是本地纯计算
# （LCS、集合运算、Levenshtein），零 API 成本、ms 级延迟 —— 与 deterministic
# 共享同一个成本模型。把它们排除掉正是旧 if-chain 的错误：那 10 个分支恰好
# 就是 10 个 deterministic 类型，注册表里多出来的 4 个是 3 个 statistical
# 加 judge。只按名字挡 judge 会在新增 judge 类评估器时重新漏掉。
_CONFIG_BUILDABLE_KINDS = frozenset(
    {EvaluatorKind.DETERMINISTIC, EvaluatorKind.STATISTICAL}
)


def build_deterministic_evaluator(config: dict[str, Any]) -> BaseEvaluator:
    """构造不产生 LLM 调用的评估器（保留原名，docs/M6-spec 引用）。

    名字里的 deterministic 是历史包袱：实际门槛是"零 API 成本"，
    statistical 同样满足，见 _CONFIG_BUILDABLE_KINDS。judge 显式报错而不是
    默默构造 —— 调用点（experiment runner）按零成本、ms 级延迟做的容量假设，
    拿到一个每条样本都打一次 LLM 的评估器会让实验成本与耗时失控。
    """
    evaluator = build_evaluator_from_config(config)
    if evaluator.kind not in _CONFIG_BUILDABLE_KINDS:
        allowed = ", ".join(sorted(k.value for k in _CONFIG_BUILDABLE_KINDS))
        raise ValueError(
            f"{config.get('type')!r} 是 {evaluator.kind.value} 评估器，"
            f"会产生 LLM 调用；此入口只接受: {allowed}"
        )
    return evaluator


__all__ = [
    "JudgeNeedsClientError",
    "build_deterministic_evaluator",
    "build_evaluator_from_config",
]
