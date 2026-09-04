"""Prometheus 指标定义。

所有指标在此文件集中声明，命名遵循 Prometheus 约定（snake_case + ariadne_ 前缀）。
调用方只需 import 对应的 Counter/Histogram/Gauge 并调用 .labels().inc()/.observe()。

指标清单对齐 docs/06-observability.md §6。
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---- Loop 指标 ----

loop_iterations = Histogram(
    "ariadne_loop_iterations",
    "Loop 迭代轮次分布",
    ["project", "mode", "final_state"],
    buckets=(1, 2, 3, 5, 8, 13, 21, 50, 100),
)

loop_duration_seconds = Histogram(
    "ariadne_loop_duration_seconds",
    "Loop 总耗时分布",
    ["project", "mode"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)

loop_cost_usd = Histogram(
    "ariadne_loop_cost_usd",
    "Loop 总成本分布（美元）",
    ["project", "mode", "model"],
    buckets=(0.001, 0.01, 0.1, 0.5, 1, 5, 10, 50, 100),
)

loop_terminal_total = Counter(
    "ariadne_loop_terminal_total",
    "Loop 终态计数",
    ["project", "final_state"],
)

false_completion_total = Counter(
    "ariadne_false_completion_total",
    "假完成拦截计数",
    ["project", "model"],
)

# ---- Harness 指标 ----

harness_action_total = Counter(
    "ariadne_harness_action_total",
    "Harness 动作计数",
    ["project", "rule_id", "action"],
)

harness_eval_duration_seconds = Histogram(
    "ariadne_harness_eval_duration_seconds",
    "规则求值耗时",
    ["project"],
    buckets=(0.001, 0.002, 0.005, 0.01, 0.05, 0.1),
)

# ---- Graph 指标（阶段 3-3）----

graph_duration_seconds = Histogram(
    "ariadne_graph_duration_seconds",
    "Graph 执行总耗时分布",
    ["project"],
    buckets=(0.5, 1, 5, 10, 30, 60, 120, 300, 600, 1800),
)

graph_terminal_total = Counter(
    "ariadne_graph_terminal_total",
    "Graph 终态计数",
    ["project", "final_state"],
)

graph_node_duration_seconds = Histogram(
    "ariadne_graph_node_duration_seconds",
    "Graph 单节点平均耗时分布",
    ["project", "node_type"],
    buckets=(0.05, 0.1, 0.5, 1, 5, 10, 30, 60),
)

# ---- 评测指标 ----

eval_score = Histogram(
    "ariadne_eval_score",
    "评测分数分布",
    ["project", "evaluator"],
    buckets=(0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100),
)

# ---- LLM 指标 ----

llm_errors_total = Counter(
    "ariadne_llm_errors_total",
    "LLM 调用错误计数",
    ["provider", "model", "error_code"],
)

llm_request_duration_seconds = Histogram(
    "ariadne_llm_request_duration_seconds",
    "LLM 请求耗时分布",
    ["provider", "model"],
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
)

# ---- 采集管道指标 ----

collector_lag_seconds = Gauge(
    "ariadne_collector_lag_seconds",
    "采集器消费延迟（秒）—— 自监控核心指标",
)

collector_consumed_total = Counter(
    "ariadne_collector_consumed_total",
    "已消费 span 消息数",
)

collector_written_total = Counter(
    "ariadne_collector_written_total",
    "已写入 ClickHouse 的 span 数",
)

collector_adapt_errors_total = Counter(
    "ariadne_collector_adapt_errors_total",
    "适配器错误数",
)

collector_sampled_out_total = Counter(
    "ariadne_collector_sampled_out_total",
    "被采样丢弃的 span 数",
)

queue_depth = Gauge(
    "ariadne_queue_depth",
    "Redis 队列深度（待消费消息数）",
    ["stream"],
)

queue_pending = Gauge(
    "ariadne_queue_pending",
    "Redis 队列 pending 数（已消费未 ACK）",
    ["stream"],
)

# ---- 沙箱指标 ----

sandbox_pool_available = Gauge(
    "ariadne_sandbox_pool_available",
    "沙箱预热池可用实例数",
    ["profile"],
)

sandbox_exec_total = Counter(
    "ariadne_sandbox_exec_total",
    "沙箱执行计数",
    ["profile", "status"],
)

# ---- API 指标 ----

api_request_duration_seconds = Histogram(
    "ariadne_api_request_duration_seconds",
    "API 请求耗时分布",
    ["method", "path", "status"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 10),
)

# ---- GDPR 删除指标 ----

gdpr_deletion_total = Counter(
    "ariadne_gdpr_deletion_total",
    "GDPR 删除任务计数",
    ["status"],
)


__all__ = [
    "api_request_duration_seconds",
    "collector_adapt_errors_total",
    "collector_consumed_total",
    "collector_lag_seconds",
    "collector_sampled_out_total",
    "collector_written_total",
    "eval_score",
    "false_completion_total",
    "gdpr_deletion_total",
    "graph_duration_seconds",
    "graph_node_duration_seconds",
    "graph_terminal_total",
    "harness_action_total",
    "harness_eval_duration_seconds",
    "llm_errors_total",
    "llm_request_duration_seconds",
    "loop_cost_usd",
    "loop_duration_seconds",
    "loop_iterations",
    "loop_terminal_total",
    "queue_depth",
    "queue_pending",
    "sandbox_exec_total",
    "sandbox_pool_available",
]
