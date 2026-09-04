/**
 * 与后端 Pydantic 模型一一对应的类型契约。
 *
 * 这些类型是手写的而非从 OpenAPI 生成：M1 端点少、字段稳定，
 * 生成器带来的构建复杂度不抵收益。字段变更时两侧一起改。
 */

export type SpanKind =
  | "llm"
  | "tool"
  | "rag"
  | "code"
  | "loop"
  | "harness"
  | "eval"
  | "internal";

export type SpanStatus = "ok" | "error" | "blocked";

/** 树节点。children 递归嵌套，self_ms 已由后端算好。 */
export interface SpanNode {
  span_id: string;
  parent_span_id: string;
  name: string;
  kind: SpanKind;
  operation: string;
  status: SpanStatus;
  error_type: string;
  provider: string;
  model_request: string;
  model_response: string;
  started_at: string;
  duration_ms: number;
  /** 排除子节点耗时后的自身耗时 —— 定位真实瓶颈用这个而非 duration_ms */
  self_ms: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  cost_usd: string;
  input_preview: string;
  output_preview: string;
  input_ref: string;
  output_ref: string;
  loop_id: string;
  iteration: number;
  attributes: Record<string, string>;
  tags: string[];
  children: SpanNode[];
}

export interface TraceSummary {
  trace_id: string;
  root_name: string;
  started_at: string;
  duration_ms: number;
  span_count: number;
  error_count: number;
  total_tokens: number;
  total_cost_usd: string;
  models: string[];
}

export interface TraceDetail {
  trace_id: string;
  span_count: number;
  total_tokens: number;
  total_cost_usd: string;
  duration_ms: number;
  roots: SpanNode[];
  /** 超过 5000 span 时后端会截断 */
  truncated: boolean;
}

export interface SpanListItem {
  trace_id: string;
  span_id: string;
  name: string;
  kind: SpanKind;
  status: SpanStatus;
  provider: string;
  model_request: string;
  started_at: string;
  duration_ms: number;
  total_tokens: number;
  cost_usd: string;
}

export interface CostBucket {
  key: Record<string, string>;
  span_count: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  cost_usd: string;
}

export interface CostSummary {
  from_: string;
  to: string;
  /** 窗口内全量总额，与 buckets 是否被截断无关 —— 不要用 buckets 求和代替 */
  total_cost_usd: string;
  /** input + output + cache_read + cache_write。reasoning 是 output 的子集，不并入 */
  total_tokens: number;
  span_count: number;
  /** 缓存命中率：高说明多轮场景实际计费远低于名义 Token */
  cache_hit_ratio: number;
  cache_write_tokens: number;
  reasoning_tokens: number;
  buckets: CostBucket[];
  /** buckets 被后端上限截断。真时图表只覆盖了总额的一部分，必须显式说明 */
  truncated: boolean;
}

export interface PipelineStats {
  queue_length: number;
  pending: number;
}

export interface HealthStatus {
  status: "ok" | "degraded";
  version: string;
  clickhouse: boolean;
  /** 图/Loop/模型配置都存在这里，且计入 status —— 漏掉它会让"降级"看起来毫无来由 */
  postgres: boolean;
  redis: boolean;
}

/** RFC 9457 Problem Details */
export interface ProblemDetail {
  type: string;
  title: string;
  status: number;
  detail: string;
  [key: string]: unknown;
}

export interface SpanFilters {
  kind?: SpanKind;
  provider?: string;
  model?: string;
  status?: SpanStatus;
  min_duration_ms?: number;
  search?: string;
  /** 只看最近 N 小时。成本页下钻靠它把时间窗带过来，上限 24*90 */
  since_hours?: number;
  limit?: number;
}

// ---------- M2：数据集与实验 ----------

export interface DatasetNameEntry {
  name: string;
  latest_version: number;
}

export interface DatasetVersionEntry {
  version: number;
  content_hash: string;
  item_count: number;
}

export interface DatasetItem {
  item_id: string;
  input: string;
  expected: string | null;
  metadata: Record<string, string>;
}

export interface DatasetDetail {
  name: string;
  version: number;
  content_hash: string;
  item_count: number;
  /** `name@vN#hash` —— 实验记录里引用数据集的规范形式 */
  ref: string;
  description: string;
  items: DatasetItem[];
}

export type ExperimentStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export interface ExperimentSummary {
  id: string;
  dataset_ref: string;
  config_label: string;
  status: ExperimentStatus;
  item_count: number;
  failed_count: number;
  total_cost_usd: string;
  metrics: Record<string, unknown>;
  /** Judge 版本切换是破坏性变更，两侧分数不可直接比较 */
  judge_models: string[];
  created_at: string;
  finished_at: string | null;
  error: string;
}

export type CompareDirection =
  | "improved"
  | "regressed"
  | "unchanged"
  | "inconclusive";

export interface CompareStat {
  metric: string;
  baseline_mean: number;
  current_mean: number;
  delta: number;
  delta_pct: number;
  ci_low: number;
  ci_high: number;
  direction: CompareDirection;
  /** 置信区间跨 0 时为 false —— 差异不显著 */
  significant: boolean;
  sample_count: number;
}

export interface CompareResponse {
  passed: boolean;
  /** 0 通过 / 1 有退化 / 2 配置或数据问题 */
  exit_code: number;
  dataset_ref: string;
  stats: CompareStat[];
  churn: Record<string, number>;
  violations: string[];
  warnings: string[];
  flipped_to_fail: string[];
  report_text: string;
}

// ---------- Loop（M3） ----------

export interface LoopAssertion {
  id: string;
  kind: "command" | "schema" | "regex" | "metric" | "human";
  spec: Record<string, unknown>;
  weight: number;
  blocking: boolean;
  hint: string;
}

export interface LoopBudget {
  max_iterations: number;
  max_total_tokens: number;
  max_cost_usd: number;
  max_tokens_per_iteration: number;
  max_wall_clock_seconds: number;
}

export interface LoopGoal {
  task: string;
  assertions: LoopAssertion[];
  budget: LoopBudget;
  mode: string;
  stall_threshold: number;
  stall_patience: number;
}

export type LoopState =
  | "CREATED"
  | "VALIDATE"
  | "PLANNING"
  | "PRECHECK"
  | "EXECUTING"
  | "EVALUATING"
  | "JUDGING"
  | "REVISING"
  | "HUMAN_PENDING"
  | "CONVERGED"
  | "REJECTED"
  | "BLOCKED"
  | "BUDGET_EXCEEDED"
  | "MAX_ITERATIONS"
  | "STALLED"
  | "FAILED"
  | "CANCELLED";

export interface LoopSummary {
  id: string;
  project_id: string;
  mode: string;
  state: string;
  iteration: number;
  cumulative_tokens: number;
  cumulative_cost_usd: number;
  final_state: string | null;
  worker_id: string | null;
  goal: LoopGoal;
  error: string;
  created_at: string | null;
  finished_at: string | null;
}

export interface LoopIteration {
  iteration: number;
  state: string;
  output_fp: string;
  failure_fp: string;
  cumulative_tokens: number;
  cumulative_cost_usd: number;
  verdict: {
    converged: boolean;
    passed: string[];
    failed: Array<{
      assertion_id: string;
      kind: string;
      passed: boolean;
      value: number;
      evidence: string;
      pending_human: boolean;
      errored: boolean;
      duration_ms: number;
    }>;
    score: number;
    claimed_done: boolean;
    false_completion: boolean;
    pending_human: string[];
    errored: string[];
  } | null;
  critique: {
    failures: string[];
    evidence: string[];
    directives: string[];
    forbidden: string[];
    escalation: string;
  } | null;
  created_at: string | null;
}

export interface LoopEvent {
  event: string;
  loop_id: string;
  ts: string;
}

/** 终态诊断提示（后端 TERMINAL_DIAGNOSIS 的中文文案） */
export const TERMINAL_DIAGNOSIS: Record<string, string> = {
  CONVERGED: "全部阻塞性断言通过",
  REJECTED: "目标不可验证，或人工拒绝",
  BLOCKED: "Harness 硬约束拦截，不建议重试",
  BUDGET_EXCEEDED: "预算耗尽。得分上升中则提高预算，趋势平坦则先修断言设计",
  MAX_ITERATIONS: "轮次耗尽但仍在改善，可提高 max_iterations",
  STALLED: "反馈信号无效，原地打转。建议补充 hint，或换用判定更明确的断言（如 JSON Schema）",
  FAILED: "内部错误，非任务本身的问题",
  CANCELLED: "被主动取消",
};

export const LOOP_STATE_LABELS: Record<string, string> = {
  CREATED: "已创建",
  VALIDATE: "校验中",
  PLANNING: "规划中",
  PRECHECK: "预检",
  EXECUTING: "执行中",
  EVALUATING: "评估中",
  JUDGING: "判定中",
  REVISING: "修正中",
  HUMAN_PENDING: "待人工审批",
  CONVERGED: "已达标",
  REJECTED: "已拒绝",
  BLOCKED: "被拦截",
  BUDGET_EXCEEDED: "预算耗尽",
  MAX_ITERATIONS: "轮次耗尽",
  STALLED: "原地打转",
  FAILED: "内部错误",
  CANCELLED: "已取消",
};

/** Loop 模式的中文标签。键必须与后端 LoopMode 字面量一致，见 loop_module/goal.py */
export const LOOP_MODE_LABELS: Record<string, string> = {
  retry: "重试",
  quality: "质量",
  verify_execute: "验证执行",
  hitl: "人工协同",
};

// ---------- M5：编排与可视化 ----------

export interface GraphPort {
  name: string;
  kind: string; // text / documents / json / artifact / any
  required: boolean;
}

export interface GraphNodeData {
  id: string;
  kind: string; // llm / tool / rag / code / branch / loop / eval
  inputs: GraphPort[];
  outputs: GraphPort[];
  params: Record<string, unknown>;
}

export interface GraphEdgeData {
  source: string;
  source_port: string;
  target: string;
  target_port: string;
}

export interface WorkflowGraphData {
  version: string;
  graph: {
    version: string;
    nodes: GraphNodeData[];
    edges: GraphEdgeData[];
  };
}

export interface GraphResponse {
  id: string;
  project_id: string;
  name: string;
  version: number;
  graph: Record<string, unknown>;
  validation_errors: GraphValidationError[];
  is_active: boolean;
  description: string;
}

export interface GraphValidationError {
  field: string;
  message: string;
  severity: string;
}

export interface GraphValidateResponse {
  ok: boolean;
  errors: GraphValidationError[];
  warnings: GraphValidationError[];
}

export const NODE_KIND_LABELS: Record<string, string> = {
  llm: "LLM",
  tool: "工具",
  rag: "RAG",
  code: "代码",
  branch: "分支",
  loop: "循环",
  eval: "评估",
  subgraph: "子图编排",
};

export const NODE_KIND_COLORS: Record<string, string> = {
  llm: "#3b82f6",
  tool: "#10b981",
  rag: "#f59e0b",
  code: "#8b5cf6",
  branch: "#ef4444",
  loop: "#ec4899",
  eval: "#6366f1",
  subgraph: "#14b8a6",
};

// ---------- M5 Week 4：Playground ----------

export interface LLMConfig {
  model: string;
  temperature: number;
  max_tokens: number;
  system_prompt: string;
}

export interface PlaygroundRunResponse {
  request_id: string;
  prompt: string;
  config: LLMConfig;
  estimated_cost_usd: number;
}

export interface ConfigResult {
  config: LLMConfig;
  output: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  error: string;
}

export interface PlaygroundCompareResponse {
  request_id: string;
  prompt: string;
  results: ConfigResult[];
}

export interface FreezeSpecResponse {
  spec_yaml: string;
  spec_dict: Record<string, unknown>;
}

export interface ReproduceResponse {
  trace_id: string;
  span_id: string;
  prompt: string;
  config: LLMConfig;
  original_output: string;
  original_cost_usd: number;
}

// ---------- 模型配置（自定义 provider / model / key / base_url） ----------

export type ModelProvider = "anthropic" | "openai" | "openai_compatible";

/** 列表/详情项。api_key 仅返回前缀，不含明文。 */
export interface ModelConfig {
  id: string;
  name: string;
  provider: ModelProvider;
  model: string;
  api_key_prefix: string;
  base_url: string;
  degraded_model: string;
  is_default: boolean;
  is_active: boolean;
  sort_order: number;
  last_used_at: string | null;
  created_at: string;
}

export interface ModelConfigList {
  models: ModelConfig[];
  cryptography_available: boolean;
}

export interface ModelConfigCreateResponse extends ModelConfig {
  /** 明文 api_key —— 仅创建时返回一次 */
  api_key: string;
}

export interface DefaultModelConfig {
  id: string;
  name: string;
  provider: ModelProvider;
  model: string;
  base_url: string;
  degraded_model: string;
  found: boolean;
}
