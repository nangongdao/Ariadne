/**
 * 开发态 API mock —— Vite 插件，仅当 `VITE_MOCK=1 npm run dev` 时启用。
 *
 * 目的：UI 开发/视觉验证不必起 Postgres + ClickHouse 全栈。在 Vite 内部
 * 中间件之前拦截 /v1/* 与 /health，返回与 src/api/types.ts 契约一致的
 * 确定性数据（种子随机，刷新不变），页面能以真实密度渲染。
 *
 * 命中不了的路由会 404 JSON，不会漏到后端代理。
 */

// 确定性伪随机（mulberry32）：同一 seed 每次刷新数据一致，截图可复现。
function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const pick = (r, arr) => arr[Math.floor(r() * arr.length)];
const float = (r, min, max, dp = 4) =>
  Number((min + r() * (max - min)).toFixed(dp));
const int = (r, min, max) => Math.floor(min + r() * (max - min + 1));

const NOW = Date.parse("2026-08-29T14:30:00+08:00");
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();
const H = 3600e3;
const M = 60e3;

const MODELS = [
  { provider: "openai", model: "gpt-4o-mini", in: 0.15, out: 0.6 },
  { provider: "openai", model: "gpt-4.1", in: 2.0, out: 8.0 },
  { provider: "anthropic", model: "claude-sonnet-4-5", in: 3.0, out: 15.0 },
  { provider: "openai_compatible", model: "deepseek-v3", in: 0.27, out: 1.1 },
];

const ROOT_NAMES = [
  "rag.answer", "agent.code-review", "loop.doc-quality", "agent.sql-assistant",
  "pipeline.ingest", "agent.support-triage", "loop.test-fix", "eval.batch-judge",
  "agent.report-writer", "rag.faq-router",
];

// ---------- Trace 列表 ----------

function traceSummaries() {
  const r = rng(20260829);
  const out = [];
  for (let i = 0; i < 50; i++) {
    const errCount = r() < 0.18 ? int(r, 1, 3) : 0;
    const spanCount = int(r, 6, 60);
    const tokens = int(r, 800, 90000);
    const m = pick(r, MODELS);
    out.push({
      trace_id: `tr-${(0x5f3a9c1 + i * 0x9e3779b).toString(16).padStart(12, "0").slice(0, 12)}`,
      root_name: pick(r, ROOT_NAMES),
      started_at: iso(i * 7.3 * M + int(r, 0, 300) * 1000),
      duration_ms: int(r, 400, 60000),
      span_count: spanCount,
      error_count: errCount,
      total_tokens: tokens,
      total_cost_usd: float(r, 0.002, 0.9),
      models: r() < 0.3 ? [m.model, pick(r, MODELS).model] : [m.model],
    });
  }
  return out;
}

// ---------- 单条 Trace 的 span 树 ----------

function spanNode(r, depth, seq, ctx) {
  const kind = depth === 0 ? pick(r, ["loop", "agent", "rag", "code"]) === "agent" ? "llm" : pick(r, ["loop", "rag", "code"]) : pick(r, ["llm", "llm", "tool", "rag", "code", "harness", "eval", "internal"]);
  const isLlm = kind === "llm";
  const m = pick(r, MODELS);
  const error = r() < 0.07;
  const duration = int(r, 40, 12000);
  const self = Math.max(5, Math.floor(duration * (isLlm ? 0.9 : 0.3)));
  const inTok = isLlm ? int(r, 120, 9000) : 0;
  const outTok = isLlm ? int(r, 20, 2200) : 0;
  const cacheRead = isLlm && r() < 0.5 ? Math.floor(inTok * 0.6) : 0;
  const id = `sp-${ctx.base + seq}`;
  const children = [];
  let s = seq + 1;
  if (depth < 3 && r() < (depth === 0 ? 1 : 0.65)) {
    const n = int(r, 1, depth === 0 ? 6 : 3);
    for (let i = 0; i < n; i++) {
      const [child, ns] = spanNode(r, depth + 1, s, ctx);
      children.push(child);
      s = ns;
    }
  }
  const name = isLlm
    ? pick(r, ["chat.completions", "messages.create", "llm.generate"])
    : pick(r, ["retrieval.search", "tool.http_fetch", "code.exec", "harness.check", "eval.judge", "internal.serialize"]);
  return [{
    span_id: id,
    parent_span_id: ctx.parent,
    name,
    kind,
    operation: `${kind}.${name.split(".")[0]}`,
    status: error ? "error" : r() < 0.02 ? "blocked" : "ok",
    error_type: error ? pick(r, ["RateLimitError", "ValidationError", "TimeoutError", "AssertionError"]) : "",
    provider: isLlm ? m.provider : "",
    model_request: isLlm ? m.model : "",
    model_response: isLlm && r() < 0.9 ? m.model : "",
    started_at: iso(ctx.offset + int(r, 0, 20000)),
    duration_ms: duration,
    self_ms: self,
    input_tokens: inTok,
    output_tokens: outTok,
    cache_read_tokens: cacheRead,
    cache_write_tokens: 0,
    reasoning_tokens: isLlm && r() < 0.3 ? int(r, 50, 900) : 0,
    cost_usd: isLlm ? ((inTok * m.in + outTok * m.out) / 1e6).toFixed(6) : "0",
    input_preview: isLlm ? "你是一名资深工程师，请审查以下 diff…" : "SELECT project_id, count(*) …",
    output_preview: isLlm ? "整体实现正确，但第 3 处缺少对空列表的防御…" : "47 rows",
    input_ref: `clickhouse://spans/${id}/input.json`,
    output_ref: `clickhouse://spans/${id}/output.json`,
    loop_id: "",
    iteration: 0,
    attributes: { "gen_ai.system": m.provider, "ariadne.depth": String(depth) },
    tags: r() < 0.2 ? ["prod"] : [],
    children,
  }, s];
}

function traceDetail(traceId) {
  const r = rng(parseInt(traceId.replace(/\D/g, "").slice(0, 8), 10) || 42);
  const ctx = { base: int(r, 1, 900), parent: "", offset: 20 * M };
  const [root] = spanNode(r, 0, 0, ctx);
  root.parent_span_id = "";
  root.name = pick(r, ROOT_NAMES);
  root.kind = "loop";
  let count = 0;
  let tokens = 0;
  let cost = 0;
  const walk = (n) => {
    count++;
    tokens += n.input_tokens + n.output_tokens;
    cost += Number(n.cost_usd) || 0;
    n.children.forEach(walk);
  };
  walk(root);
  return {
    trace_id: traceId,
    span_count: count,
    total_tokens: tokens,
    total_cost_usd: cost.toFixed(6),
    duration_ms: root.duration_ms,
    roots: [root],
    truncated: false,
  };
}

// ---------- Spans 列表 ----------

function spanList(query) {
  const r = rng(777 + (query.get("search")?.length ?? 0));
  const limit = Math.min(Number(query.get("limit")) || 100, 200);
  const out = [];
  for (let i = 0; i < limit; i++) {
    const kind = pick(r, ["llm", "llm", "llm", "tool", "rag", "code", "harness", "eval", "internal"]);
    const isLlm = kind === "llm";
    const m = pick(r, MODELS);
    const inTok = isLlm ? int(r, 100, 8000) : 0;
    const outTok = isLlm ? int(r, 20, 2000) : 0;
    out.push({
      trace_id: `tr-${int(r, 0x100000, 0xffffff).toString(16)}`,
      span_id: `sp-${int(r, 0x100000, 0xffffff).toString(16)}`,
      name: isLlm ? pick(r, ["chat.completions", "messages.create"]) : pick(r, ["retrieval.search", "tool.http_fetch", "code.exec", "harness.check", "eval.judge"]),
      kind,
      status: r() < 0.06 ? "error" : r() < 0.01 ? "blocked" : "ok",
      provider: isLlm ? m.provider : "",
      model_request: isLlm ? m.model : "",
      started_at: iso(i * 3.1 * M),
      duration_ms: int(r, 30, 15000),
      total_tokens: inTok + outTok,
      cost_usd: isLlm ? ((inTok * m.in + outTok * m.out) / 1e6).toFixed(6) : "0",
    });
  }
  return out;
}

// ---------- 成本 ----------

function costSummary(query) {
  const r = rng(31415);
  const days = Math.max(1, Math.round((Number(query.get("hours")) || 24) / 24));
  const groupBy = query.get("group_by") || "model_request";
  // 桶键必须与请求的 group_by 一致：按模型/Provider 返回少量离散桶（饼图），
  // 按小时/天返回时间序列（折线），与真实后端聚合行为对齐
  const isTime = groupBy === "hour" || groupBy === "day";
  const modelShare = [0.42, 0.31, 0.18, 0.09];
  const buckets = [];
  let total = 0;
  let tokens = 0;
  let spans = 0;

  const groups = isTime
    ? Array.from({ length: Math.min(days, 30) }, (_, i) =>
        new Date(NOW - (Math.min(days, 30) - 1 - i) * 24 * H).toISOString().slice(0, 10))
    : groupBy === "provider"
      ? ["openai", "anthropic", "openai_compatible"]
      : MODELS.map((m) => m.model);

  groups.forEach((g, gi) => {
    const share = isTime ? 1 : (modelShare[gi] ?? 0.1);
    const inTok = Math.floor(int(r, 30000, 220000) * share * 4);
    const outTok = Math.floor(inTok * float(r, 0.1, 0.35, 2));
    const cache = Math.floor(inTok * float(r, 0.15, 0.6, 2));
    const m = MODELS[gi % MODELS.length];
    const cost = isTime
      ? (inTok * m.in + outTok * m.out) / 1e6
      : (inTok * m.in + outTok * m.out + cache * m.in * 0.25) / 1e6;
    total += cost;
    tokens += inTok + outTok + cache;
    const spanCount = int(r, 60, 900);
    spans += spanCount;
    buckets.push({
      key: { [groupBy]: g },
      span_count: spanCount,
      input_tokens: inTok,
      output_tokens: outTok,
      cache_read_tokens: cache,
      cost_usd: cost.toFixed(4),
    });
  });

  if (!isTime) buckets.sort((a, b) => Number(b.cost_usd) - Number(a.cost_usd));

  return {
    from_: iso(days * 24 * H),
    to: new Date(NOW).toISOString(),
    total_cost_usd: total.toFixed(4),
    total_tokens: tokens,
    span_count: spans,
    cache_hit_ratio: 0.42,
    buckets,
  };
}

// ---------- Loop ----------

function loopSummaries() {
  const r = rng(9001);
  const states = ["CONVERGED", "EXECUTING", "STALLED", "MAX_ITERATIONS", "HUMAN_PENDING", "BUDGET_EXCEEDED", "CONVERGED", "CANCELLED"];
  const modes = ["verify_execute", "quality", "retry", "human_in_loop"];
  const tasks = [
    "生成一篇关于 Loop Engineering 的技术博客，质量分 ≥ 85",
    "修复 tests/test_parser.py 中全部 6 个失败用例",
    "为 openapi.yaml 补全缺失的 schema 描述",
    "把 README 翻译为英文并通过术语一致性检查",
    "重构 retry.py，使 flake8 与 mypy --strict 全绿",
  ];
  return states.map((state, i) => ({
    id: `loop-${(1000 + i).toString(16)}`,
    project_id: "p_default",
    mode: modes[i % modes.length],
    state,
    iteration: state === "CONVERGED" ? int(r, 2, 5) : state === "EXECUTING" ? int(r, 1, 3) : int(r, 3, 10),
    cumulative_tokens: int(r, 12000, 900000),
    cumulative_cost_usd: float(r, 0.05, 4.2),
    final_state: state === "EXECUTING" ? null : state,
    worker_id: state === "EXECUTING" ? "w-7f3a" : null,
    goal: {
      task: tasks[i % tasks.length],
      assertions: [
        { id: "a1", kind: "command", spec: { command: "pytest -q", expect_exit: 0 }, weight: 1, blocking: true, hint: "全部测试通过" },
        { id: "a2", kind: "metric", spec: { metric: "quality_score", gte: 0.85 }, weight: 0.6, blocking: true, hint: "质量分 ≥ 0.85" },
      ],
      budget: { max_iterations: 10, max_total_tokens: 800000, max_cost_usd: 5, max_tokens_per_iteration: 30000, max_wall_clock_seconds: 1800 },
      mode: modes[i % modes.length],
      stall_threshold: 0.02,
      stall_patience: 3,
    },
    error: "",
    created_at: iso((i + 2) * 5 * H),
    finished_at: state === "EXECUTING" ? null : iso(i * 4 * H + 1200e3),
  }));
}

function loopIterations(loopId, summary) {
  const r = rng(parseInt(loopId.replace(/\D/g, "") || "7", 10) || 7);
  const iters = [];
  let cumTok = 0;
  let cumCost = 0;
  const scores = [0.62, 0.71, 0.78, 0.74, 0.86, 0.9, 0.93, 0.95, 0.96, 0.97];
  for (let i = 1; i <= Math.max(summary.iteration, 4); i++) {
    cumTok += int(r, 8000, 60000);
    cumCost += float(r, 0.02, 0.4);
    const score = scores[(i - 1) % scores.length];
    const converged = i === summary.iteration && summary.state === "CONVERGED";
    iters.push({
      iteration: i,
      state: "DONE",
      output_fp: `fp-${int(r, 0x10000, 0xfffff).toString(16)}`,
      failure_fp: converged ? "" : `fp-${int(r, 0x10000, 0xfffff).toString(16)}`,
      cumulative_tokens: cumTok,
      cumulative_cost_usd: Number(cumCost.toFixed(4)),
      verdict: {
        converged,
        passed: converged ? ["a1", "a2"] : ["a1"],
        failed: converged ? [] : [{
          assertion_id: "a2", kind: "metric", passed: false, value: score,
          evidence: `质量分 ${score.toFixed(2)} < 0.85`,
          pending_human: false, errored: false, duration_ms: int(r, 200, 4000),
        }],
        score,
        claimed_done: i === summary.iteration,
        false_completion: i === 3 && r() < 0.3,
        pending_human: summary.state === "HUMAN_PENDING" && i === summary.iteration ? ["a3"] : [],
        errored: [],
      },
      critique: converged ? null : {
        failures: ["引用来源不足", "第 2 节缺少代码示例"],
        evidence: ["断言 a2 评分 0.71 < 0.85", "正则检查未匹配 `\\[\\d+\\]`"],
        directives: ["补充至少 3 个可验证的引用来源", "在关键论点后附上可运行的示例代码"],
        forbidden: ["不要使用占位符文本", "不要编造数据来源"],
        escalation: "",
      },
      created_at: iso((10 - i) * 3 * H),
    });
  }
  return iters;
}

// ---------- 实验 / 数据集 ----------

function experiments() {
  const r = rng(5150);
  const statuses = ["completed", "completed", "completed", "running", "failed", "completed"];
  return statuses.map((status, i) => ({
    id: `exp-${(700 + i).toString(16)}`,
    dataset_ref: `qa-support@v${int(r, 1, 4)}#${int(r, 0x10000, 0xfffff).toString(16)}`,
    config_label: pick(r, ["gpt-4o-mini@t0.2", "claude-sonnet-4-5@t0.0", "deepseek-v3@t0.3", "gpt-4.1@t0.2"]),
    status,
    item_count: int(r, 40, 300),
    failed_count: status === "failed" ? int(r, 1, 8) : 0,
    total_cost_usd: float(r, 0.1, 6),
    metrics: { factuality: float(r, 0.7, 0.95, 3), ifr: float(r, 0.8, 0.99, 3), similarity: float(r, 0.75, 0.94, 3) },
    judge_models: ["gpt-4.1"],
    created_at: iso((i + 1) * 9 * H),
    finished_at: status === "running" ? null : iso(i * 8 * H),
    error: status === "failed" ? "judge 超时：3 次重试后仍 504" : "",
  }));
}

function datasetDetail(name) {
  const r = rng(name.length * 31 + 7);
  const items = Array.from({ length: 8 }, (_, i) => ({
    item_id: `it-${i.toString().padStart(3, "0")}`,
    input: pick(r, ["订单 8842 为什么还没发货？", "如何申请发票？", "退款到账时间是多久？", "能修改收货地址吗？", "会员积分怎么兑换？"]),
    expected: r() < 0.8 ? "您好，订单已在拣货中，预计 24 小时内发出…" : null,
    metadata: { source: "客服后台", lang: "zh" },
  }));
  return {
    name,
    version: 3,
    content_hash: int(r, 0x100000, 0xffffff).toString(16),
    item_count: 214,
    ref: `${name}@v3#${int(r, 0x100000, 0xffffff).toString(16)}`,
    description: "客服 FAQ 质量回归集",
    items,
  };
}

// ---------- Graph ----------

function graphResponse(i) {
  const defs = [
    {
      name: "rag-doc-quality",
      desc: "检索增强问答 + 质量评估",
      nodes: [
        ["retrieve", "rag"], ["draft", "llm"], ["check", "branch"],
        ["judge", "eval"], ["revise", "llm"], ["publish", "tool"],
      ],
      edges: [
        ["retrieve", "output", "draft", "input"], ["draft", "output", "check", "input"],
        ["check", "route", "judge", "input"], ["check", "route", "revise", "input"],
        ["judge", "output", "publish", "input"],
      ],
    },
    {
      name: "batch-translate",
      desc: "并行翻译 + 术语校验",
      nodes: [
        ["split", "code"], ["translate", "llm"], ["verify", "tool"], ["merge", "code"],
      ],
      edges: [
        ["split", "output", "translate", "input"], ["translate", "output", "verify", "input"],
        ["verify", "output", "merge", "input"],
      ],
    },
  ];
  const def = defs[i % defs.length];
  return {
    id: `g-${(100 + i).toString(16)}`,
    project_id: "p_default",
    name: def.name,
    version: 3,
    graph: {
      version: "1",
      graph: {
        version: "1",
      nodes: def.nodes.map(([id, kind]) => ({
        id, kind,
        inputs: [{ name: "input", kind: "text", required: true }],
        outputs: [kind === "branch"
          ? { name: "route", kind: "any", required: false }
          : { name: "output", kind: "text", required: false }],
        params: kind === "llm" ? { model: "gpt-4o-mini", temperature: 0.3, system_prompt: "你是严谨的技术作者" } : {},
      })),
        edges: def.edges.map(([s, sp, t, tp]) => ({ source: s, source_port: sp, target: t, target_port: tp })),
      },
    },
    validation_errors: i === 0 ? [] : [{ field: "graph.edges[2]", message: "端口类型 text→json 不兼容", severity: "error" }],
    is_active: i === 0,
    description: def.desc,
  };
}

// ---------- 模型配置 ----------

function modelConfigs() {
  return {
    models: [
      { id: "mc-01", name: "主力 Sonnet", provider: "anthropic", model: "claude-sonnet-4-5", api_key_prefix: "sk-ant-…f4a2", base_url: "https://api.anthropic.com", degraded_model: "claude-haiku-4-5", is_default: true, is_active: true, sort_order: 0, last_used_at: iso(20 * M), created_at: iso(30 * 24 * H) },
      { id: "mc-02", name: "便宜批量", provider: "openai_compatible", model: "deepseek-v3", api_key_prefix: "sk-…9c1d", base_url: "https://api.deepseek.com/v1", degraded_model: "", is_default: false, is_active: true, sort_order: 1, last_used_at: iso(3 * H), created_at: iso(20 * 24 * H) },
      { id: "mc-03", name: "GPT 备选", provider: "openai", model: "gpt-4.1", api_key_prefix: "sk-…77ba", base_url: "https://api.openai.com/v1", degraded_model: "gpt-4o-mini", is_default: false, is_active: true, sort_order: 2, last_used_at: null, created_at: iso(10 * 24 * H) },
    ],
    cryptography_available: true,
  };
}

// ---------- SSE ----------

function sseEvents(loopId) {
  const states = ["EXECUTING", "EVALUATING", "JUDGING", "REVISING", "EXECUTING"];
  let i = 0;
  return {
    // 每次连接从当前状态推进 3 个事件后结束（client 用 onmessage，必须未命名事件）
    next() {
      if (i >= states.length) return null;
      const ev = { event: "state_changed", loop_id: loopId, ts: new Date().toISOString(), state: states[i++] };
      return `data: ${JSON.stringify(ev)}\n\n`;
    },
  };
}

// ---------- 路由表 ----------

const traces = traceSummaries();

function route(method, path, query, body) {
  const seg = path.split("?")[0].split("/").filter(Boolean); // ["v1", ...]
  const json = (data, status = 200) => ({ status, data });

  if (path === "/health") {
    return json({ status: "ok", version: "0.6.0", clickhouse: true, redis: true });
  }
  if (path === "/v1/stats") return json({ queue_length: 3, pending: 12 });
  if (path === "/v1/traces" && method === "GET") {
    let list = traces;
    if (query.get("only_errors") === "true") list = list.filter((t) => t.error_count > 0);
    const before = query.get("before");
    if (before) {
      const idx = list.findIndex((t) => t.trace_id === before);
      if (idx >= 0) list = list.slice(idx + 1);
    }
    return json(list.slice(0, Number(query.get("limit")) || 50));
  }
  if (seg[0] === "v1" && seg[1] === "traces" && seg[2] && method === "GET") {
    return json(traceDetail(seg[2]));
  }
  if (path === "/v1/spans") return json(spanList(query));
  if (path === "/v1/costs") return json(costSummary(query));

  if (path === "/v1/datasets" && method === "GET") {
    return json([
      { name: "qa-support", latest_version: 3 },
      { name: "code-review-gold", latest_version: 1 },
      { name: "blog-quality", latest_version: 2 },
    ]);
  }
  if (seg[1] === "datasets" && seg[3] === "versions") {
    return json([{ version: 3, content_hash: "a91f2c", item_count: 214 }, { version: 2, content_hash: "77bd01", item_count: 198 }, { version: 1, content_hash: "5e009a", item_count: 150 }]);
  }
  if (seg[1] === "datasets" && seg[2]) return json(datasetDetail(decodeURIComponent(seg[2])));

  if (path === "/v1/experiments") return json(experiments());
  if (seg[1] === "experiments" && seg[2] && !seg[3]) {
    return json(experiments().find((e) => e.id === seg[2]) ?? experiments()[0]);
  }

  if (path === "/v1/loops" && method === "GET") return json(loopSummaries());
  if (path === "/v1/loops" && method === "POST") {
    const id = `loop-${Date.now().toString(16)}`;
    return json({ loop_id: id, state: "EXECUTING", stream_url: `/v1/loops/${id}/stream` });
  }
  if (seg[1] === "loops" && seg[2] && seg[3] === "iterations") {
    const summary = loopSummaries().find((l) => l.id === seg[2]) ?? loopSummaries()[0];
    return json({ iterations: loopIterations(seg[2], summary) });
  }
  if (seg[1] === "loops" && seg[2] && seg[3] === "stream") return { sse: seg[2] };
  if (seg[1] === "loops" && seg[2] && !seg[3]) {
    return json(loopSummaries().find((l) => l.id === seg[2]) ?? loopSummaries()[0]);
  }

  if (path === "/v1/graphs" && method === "GET") return json([graphResponse(0), graphResponse(1)]);
  if (path === "/v1/graphs" && method === "POST") return json(graphResponse(0));
  if (path === "/v1/graphs/validate" && method === "POST") {
    return json(body?.graph?.nodes?.length > 5
      ? { ok: true, errors: [], warnings: [] }
      : { ok: false, errors: [{ field: "graph.edges", message: "存在 1 条端口类型不兼容的连线", severity: "error" }], warnings: [] });
  }
  if (seg[1] === "graphs" && seg[2] && method === "GET") {
    return json(graphResponse(seg[2] === "g-new" ? 0 : Number.parseInt(seg[2].replace(/\D/g, "") || "0", 10)));
  }
  if (seg[1] === "graphs" && seg[2] && method === "PUT") {
    return json({ ...graphResponse(0), ...body, version: 4 });
  }

  if (path === "/v1/models" && method === "GET") return json(modelConfigs());
  if (path === "/v1/models" && method === "POST") {
    const cfg = modelConfigs().models[0];
    return json({ ...cfg, ...body, id: `mc-${Date.now().toString(16)}`, api_key: body?.api_key ?? "sk-new", api_key_prefix: `${(body?.api_key ?? "sk").slice(0, 6)}…last4`, created_at: new Date().toISOString() });
  }
  if (seg[1] === "models" && seg[2] && method === "PUT") return json({ ...modelConfigs().models[0], ...body });
  if (seg[1] === "models" && seg[2] && method === "DELETE") return json(null, 204);

  if (path === "/v1/playground/compare" && method === "POST") {
    const prompt = body?.prompt ?? "";
    return json({
      request_id: `pg-${Date.now().toString(16)}`,
      prompt,
      results: [
        { config: body?.configs?.[0] ?? { model: "gpt-4o-mini", temperature: 0.2, max_tokens: 1024, system_prompt: "" }, output: "Loop Engineering 的本质是把「一次交互」升级为「带反馈闭环的系统工程」：目标可验证、每轮有检查、状态可恢复。\n\n它与传统 Prompt Engineering 的区别不在模型，而在可靠性来源——工程机制而非模型能力。", input_tokens: 312, output_tokens: 486, cost_usd: 0.0009, error: "" },
        { config: body?.configs?.[1] ?? { model: "claude-sonnet-4-5", temperature: 0.7, max_tokens: 2048, system_prompt: "" }, output: "如果说 Prompt Engineering 回答的是「怎么让模型第一次就答对」，那么 Loop Engineering 回答的是「怎么让系统在模型答错时自己改到对」。\n\n三个支点：可验证的停止条件、外部强制的完成判定（Ralph Loop）、以及轮次间的状态收敛。", input_tokens: 312, output_tokens: 522, cost_usd: 0.0042, error: "" },
      ],
    });
  }
  if (path === "/v1/playground/freeze" && method === "POST") {
    return json({ spec_yaml: "goal:\n  task: 对比两种模型风格\n  assertions:\n    - kind: regex\n      spec: { pattern: '^.{50,}' }\n", spec_dict: {} });
  }
  if (path === "/v1/playground/reproduce" && method === "POST") {
    return json({ trace_id: "tr-5f3a9c1", span_id: "sp-901", prompt: body?.prompt ?? "原始 prompt", config: { model: "gpt-4o-mini", temperature: 0.3, max_tokens: 1024, system_prompt: "" }, original_output: "当时的原始输出…", original_cost_usd: 0.0012 });
  }

  return json({ type: "about:blank", title: "Not Found", status: 404, detail: `mock 未覆盖: ${method} ${path}` }, 404);
}

/** 挂到 Vite dev server：在内部中间件（含 proxy）之前拦截。 */
export function mockApiPlugin() {
  return {
    name: "ariadne-mock-api",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const url = req.url ?? "";
        if (!url.startsWith("/v1/") && url !== "/health") return next();
        const u = new URL(url, "http://mock.local");
        const chunks = [];
        req.on("data", (c) => chunks.push(c));
        req.on("end", () => {
          let body;
          try {
            body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
          } catch {
            body = undefined;
          }
          const out = route(req.method ?? "GET", u.pathname, u.searchParams, body);
          if (out.sse) {
            res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
            const gen = sseEvents(out.sse);
            const timer = setInterval(() => {
              const chunk = gen.next();
              if (chunk === null) {
                clearInterval(timer);
                res.end();
              } else {
                res.write(chunk);
              }
            }, 1500);
            req.on("close", () => clearInterval(timer));
            return;
          }
          res.writeHead(out.status, { "Content-Type": "application/json; charset=utf-8" });
          res.end(out.data === null ? "" : JSON.stringify(out.data));
        });
      });
    },
  };
}
