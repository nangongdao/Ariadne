"""ariadne rules —— 规则集离线试跑与压测 CLI。

路线图（docs/11-roadmap.md）标注「计划实现」的 `ariadne rules test`。
API 侧 `POST /v1/rules/test` 已存在（在线，连库读项目规则集），这里补
离线路径：直接加载本地 YAML 规则文件 + 给定 hook/context 试跑，不改库、
无需起服务。规则作者改 YAML 后在本地立即验证，不必造样例数据进 API。

子命令：
  - `ariadne rules test --rules <文件|目录> --hook pre_model [--context '{"input":...}']`
    试跑：给定 hook + 上下文，返回裁决与命中规则。
  - `ariadne rules bench --rules <文件|目录> --hook pre_tool [--iterations N]`
    压测：重复求值测 p50/p99（规则改动后看延迟有没有量级退化）。

退出码：0 成功；2 配置/加载/编译错误（与 ariadne-eval 的 2 语义对齐）。
试跑本身不因 BLOCK 而失败 —— 这是"试跑规则"，不是"跑门禁"。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from statistics import quantiles
from typing import Any

from ariadne.harness_module.loader import RuleLoadError, compile_rule_set, load_rules
from ariadne.harness_module.models import (
    HarnessContext,
    HookKind,
    RuleHit,
)
from ariadne.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)

# 试跑默认迭代数。压测要覆盖 p99 需要足够样本。
DEFAULT_ITERATIONS = 1000
MIN_ITERATIONS = 50


class CliError(Exception):
    """CLI 用法/配置错误 → 退出码 2。"""


def _load_rules(path: str) -> list[Any]:
    """按路径加载规则：单个 YAML 文件或目录（含全部 .yaml）。"""
    p = Path(path)
    if p.is_file():
        return load_rules(p)
    if p.is_dir():
        # 目录加载合并全部 yaml——与生产 load_rule_set 一致
        from ariadne.harness_module.loader import load_rule_set

        return load_rule_set(p)
    raise CliError(f"路径不存在: {path}")


def _literal_context(raw: str | None) -> dict[str, Any]:
    """解析 --context JSON。为空给空 dict（各卡点正常可求值）。"""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"--context 不是合法 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CliError("--context 必须是 JSON 对象")
    return parsed


def _warn_missing_context_hint(
    hook: HookKind, raw_context: str | None, hits: Sequence[RuleHit]
) -> None:
    """没传 context 却有规则命中 —— 大概率是缺键被 fail-closed 兜成命中。

    pre_model 的规则读 input.text / loop、pre_persist 读 artifact.text：
    这些键不填时 CEL 求值报错，按 fail-closed 全部命中，用户会误以为
    "规则写错了全拦"（block 时最危险，warn 也误导 —— 空文本被说成
    "敏感度 > 0.7" 明显不合理）。这句提示把误导变成指导（pre_tool 有
    TOOL_CONTEXT_DEFAULTS 兜底，空 context 不会触发缺键错误）。
    """
    if raw_context or not hits:
        return
    needs = {
        HookKind.PRE_MODEL: '{"input": {"text": "..."}, "loop": {}}',
        HookKind.POST_MODEL: '{"output": {"text": "..."}, "usage": {}, "loop": {}}',
        HookKind.PRE_PERSIST: '{"artifact": {"text": "..."}}',
    }
    if hook not in needs:
        return
    print(
        f"提示：{hook.value} 的规则读了上下文里的键（{needs[hook]}），"
        "缺键会被 fail-closed 判成命中。若这不是预期拦截，请补 --context 再试。",
        file=sys.stderr,
    )


def _summarize(decision: Any) -> dict[str, Any]:
    """把 Decision 转成可打印的摘要。"""
    return {
        "action": decision.action.value,
        "blocked": decision.blocked,
        "winning_hit": (
            {
                "rule_id": decision.winning_hit.rule.id,
                "action": decision.winning_hit.rule.action.value,
                "message": decision.winning_hit.message,
            }
            if decision.winning_hit
            else None
        ),
        "hits": [h.rule.id for h in decision.hits],
    }


def _cmd_test(args: argparse.Namespace) -> int:
    """试跑：加载规则集 + 给定 hook/context，输出裁决。"""
    try:
        rules = _load_rules(args.rules)
        evaluator = compile_rule_set(rules)
        hook = HookKind(args.hook)
        context = _literal_context(args.context)
        harness_ctx = HarnessContext(hook=hook, **context)
    except (CliError, RuleLoadError, ValueError) as exc:
        # ValueError 覆盖 HookKind 非法值
        logger.error("rules test 配置错误", extra={"error": str(exc)})
        return 2

    try:
        decision = evaluator.evaluate(hook=hook, context=harness_ctx)
    except Exception as exc:  # 求值异常不应让规则作者看不到结果
        logger.error("rules test 求值失败", extra={"error": str(exc)})
        return 2

    if decision.hits:
        _warn_missing_context_hint(hook, args.context, decision.hits)
    print(json.dumps(_summarize(decision), ensure_ascii=False, indent=2))
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    """压测：重复求值测延迟分位数。"""
    try:
        rules = _load_rules(args.rules)
        evaluator = compile_rule_set(rules)
        hook = HookKind(args.hook)
        context = _literal_context(args.context)
        harness_ctx = HarnessContext(hook=hook, **context)
    except (CliError, RuleLoadError, ValueError) as exc:
        logger.error("rules bench 配置错误", extra={"error": str(exc)})
        return 2

    n = max(args.iterations, MIN_ITERATIONS)

    # 预热：把 CEL 首次编译 / 惰性 import 移出测量窗口（同 test_harness_benchmark）
    for _ in range(20):
        evaluator.evaluate(hook=hook, context=harness_ctx)

    latencies: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        evaluator.evaluate(hook=hook, context=harness_ctx)
        latencies.append((time.perf_counter() - start) * 1000)

    if n < 100:
        p50 = sorted(latencies)[n // 2]
        p99 = sorted(latencies)[-1]
    else:
        qs = quantiles(latencies, n=1000, method="inclusive")
        p50, p99 = qs[499], qs[989]

    report = {
        "iterations": n,
        "p50_ms": round(p50, 3),
        "p99_ms": round(p99, 3),
        "max_ms": round(max(latencies), 3),
        "rules": len(rules),
        "note": "绝对毫秒受机器负载影响，跨机对比用两条规则互比而非绝对值",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne rules", description="规则集离线试跑与压测"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    test = sub.add_parser("test", help="试跑：给定 hook + 上下文，返回裁决")
    test.add_argument("--rules", required=True, help="规则 YAML 文件或目录")
    test.add_argument(
        "--hook",
        required=True,
        help="卡点：pre_model/post_model/pre_tool/post_tool/pre_persist",
    )
    test.add_argument("--context", help='上下文 JSON，如 \'{"input": {"text": "..."}}\'')
    test.set_defaults(func=_cmd_test)

    bench = sub.add_parser("bench", help="压测：规则求值延迟分位数")
    bench.add_argument("--rules", required=True, help="规则 YAML 文件或目录")
    bench.add_argument("--hook", required=True, help="卡点")
    bench.add_argument("--context", help='上下文 JSON')
    bench.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS, help="迭代次数")
    bench.set_defaults(func=_cmd_bench)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging("WARNING", json_output=False)

    # Windows 控制台默认 GBK，强制 UTF-8 输出（同 eval_cli；stderr 也要，
    # 否则提示信息里的中文在 GBK 终端乱码）
    if sys.platform == "win32":
        import io
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
            else:
                sys.stdout = io.TextIOWrapper(
                    sys.stdout.buffer,
                    encoding="utf-8",
                    errors="replace",
                    line_buffering=True,
                )

    args = build_parser().parse_args(argv)
    try:
        result = args.func(args)
        return int(result)
    except CliError as exc:
        logger.error("rules CLI 错误", extra={"error": str(exc)})
        return 2


def run_rules() -> None:
    """pyproject console_scripts 入口。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())