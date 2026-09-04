"""ariadne-eval 命令：CI 回归门禁入口。

用法：
    ariadne-eval compare --baseline base.json --current cur.json
    ariadne-eval compare --baseline base.json --current cur.json --rules gate.yaml
    ariadne-eval show --result cur.json

退出码：0 通过 / 1 有退化 / 2 配置或数据问题（供 CI 判定）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ariadne.experiment import (
    DEFAULT_RULES,
    EXIT_ERROR,
    METRIC_COMPOSITE,
    METRIC_COST,
    METRIC_PASS_RATE,
    GateRule,
    compare,
    format_report,
    parse_rules,
)
from ariadne.experiment.compare import DatasetMismatchError
from ariadne.experiment.persist import (
    SchemaVersionError,
    judge_models_of,
    load,
)
from ariadne.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


def _load_rules(path: Path | None) -> tuple[GateRule, ...]:
    if path is None:
        return DEFAULT_RULES
    if not path.is_file():
        raise FileNotFoundError(f"门禁配置不存在: {path}")

    text = path.read_text(encoding="utf-8")
    if path.suffix in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - 依赖缺失路径
            raise RuntimeError(
                "读取 YAML 门禁配置需要 pyyaml，请 pip install pyyaml 或改用 JSON"
            ) from exc
        payload = yaml.safe_load(text)
    else:
        payload = json.loads(text)

    rules_raw = payload.get("fail_if") if isinstance(payload, dict) else payload
    if not isinstance(rules_raw, list):
        raise ValueError("门禁配置应为列表，或含 fail_if 列表的对象")
    return parse_rules(rules_raw)


def _cmd_compare(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    current_path = Path(args.current)

    try:
        baseline = load(baseline_path)
        current = load(current_path)
        rules = _load_rules(Path(args.rules) if args.rules else None)
    except (FileNotFoundError, SchemaVersionError, ValueError, RuntimeError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_ERROR

    # Judge 版本切换是破坏性变更：历史分数不可与新分数直接比。
    # 这里只警告不阻断 —— 强行阻断会让"就是要换 Judge"的场景无法推进。
    base_judges = judge_models_of(baseline_path)
    curr_judges = judge_models_of(current_path)
    if base_judges != curr_judges:
        print(
            f"⚠ Judge 模型不一致：baseline={base_judges or '无'} "
            f"current={curr_judges or '无'}\n"
            "  Judge 版本变更视为破坏性变更，两侧分数不可直接比较。"
            "建议用同一 Judge 重跑 baseline。",
            file=sys.stderr,
        )

    try:
        report = compare(
            baseline,
            current,
            rules=rules,
            extra_metrics=tuple(args.metric or ()),
            require_same_dataset=not args.allow_dataset_mismatch,
        )
    except DatasetMismatchError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(format_report(report))

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "passed": report.passed,
                    "exit_code": report.exit_code,
                    "dataset_ref": report.dataset_ref,
                    "stats": [
                        {
                            "metric": s.metric,
                            "baseline_mean": s.baseline_mean,
                            "current_mean": s.current_mean,
                            "delta_pct": s.delta_pct,
                            "direction": s.direction.value,
                            "significant": s.significant,
                            "ci": [s.ci_low, s.ci_high],
                        }
                        for s in report.stats
                    ],
                    "churn": report.churn,
                    "violations": [v.describe() for v in report.gate.violations],
                    "warnings": list(report.warnings),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    return report.exit_code


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        result = load(Path(args.result))
    except (FileNotFoundError, SchemaVersionError) as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return EXIT_ERROR

    metrics = result.metrics()
    print(f"实验     {result.experiment_id}")
    print(f"数据集   {result.dataset_ref}")
    print(f"配置     {result.config_label}")
    print(f"样本     {len(result.outcomes)} 条"
          f"（成功评测 {len(result.evaluated)}，"
          f"生成失败 {len(result.generation_failures)}）")
    print()
    print("指标")
    print(f"  {METRIC_COMPOSITE:24} {metrics[METRIC_COMPOSITE]:.4f}")
    print(f"  {METRIC_PASS_RATE:24} {metrics[METRIC_PASS_RATE]:.4f}")
    print(f"  {METRIC_COST:24} {metrics[METRIC_COST]:.8f}")

    breakdown = result.evaluator_metrics()
    if breakdown:
        print()
        print("各评估器均值")
        for name, value in breakdown.items():
            print(f"  {name:24} {value:.4f}")

    signatures = result.failure_signature_counts()
    if signatures:
        print()
        print("失败签名聚类（同签名说明是同一个问题）")
        for signature, count in list(signatures.items())[:10]:
            print(f"  {count:5}×  {signature}")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ariadne-eval", description="评测实验对比与回归门禁"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    compare_cmd = sub.add_parser("compare", help="对比两次实验并执行门禁")
    compare_cmd.add_argument("--baseline", required=True, help="baseline 结果 JSON")
    compare_cmd.add_argument("--current", required=True, help="current 结果 JSON")
    compare_cmd.add_argument("--rules", help="门禁配置（YAML/JSON），缺省用默认规则")
    compare_cmd.add_argument(
        "--metric", action="append", help="额外对比的指标名，可重复"
    )
    compare_cmd.add_argument("--json-out", help="把对比结果写入 JSON 文件")
    compare_cmd.add_argument(
        "--allow-dataset-mismatch",
        action="store_true",
        help="允许跨数据集对比（默认拒绝，因为在不同数据集上比均值无意义）",
    )
    compare_cmd.set_defaults(func=_cmd_compare)

    show_cmd = sub.add_parser("show", help="查看单次实验结果")
    show_cmd.add_argument("--result", required=True, help="结果 JSON")
    show_cmd.set_defaults(func=_cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging("WARNING", json_output=False)

    # Windows 控制台默认 GBK 编码，强制切换为 UTF-8 以支持 ✗/✓/⚠ 等符号
    if sys.platform == "win32":
        import io
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        else:
            sys.stdout = io.TextIOWrapper(
                sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
            )

    args = build_parser().parse_args(argv)
    exit_code: int = args.func(args)
    return exit_code


def run_eval() -> None:
    """pyproject 的 console_scripts 入口。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
