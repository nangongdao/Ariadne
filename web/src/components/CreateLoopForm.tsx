/**
 * 创建 Loop 表单 —— 目标 + 模式 + 断言 + 预算。
 *
 * 断言规格随类型切换，只显示当前类型需要的字段；预算三项默认值按
 * "跑得完一个小任务"给，不是按上限给，避免第一次用就烧掉配额。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import { AssertionSpecFields } from "@/components/AssertionSpecFields";
import {
  validateWorkspacePath,
  WorkspaceFilesField,
  type WorkspaceFile,
} from "@/components/WorkspaceFilesField";
import { ASSERTION_KINDS, MODE_OPTIONS } from "@/lib/loop-options";

/**
 * 后端 BudgetPayload.max_tokens_per_iteration 的默认值（loops.py）。
 * 表单不暴露这个字段，但总量校验要用到它。
 */
const TOKENS_PER_ITERATION = 32_000;

/**
 * retry 模式的建议轮次上限（goal_validation._check_mode_fit）。
 * 超了只是 warning，后端照样收 —— 但那条 warning 谁也看不到，
 * 所以得在表单里就地说出来。
 */
const RETRY_ITERATIONS_ADVICE = 5;

interface CreateLoopFormProps {
  onCreated: (loopId: string) => void;
}

export function CreateLoopForm({ onCreated }: CreateLoopFormProps) {
  const [task, setTask] = useState("");
  const [mode, setMode] = useState("quality");
  const [kind, setKind] = useState("regex");
  const [pattern, setPattern] = useState("");
  const [cmd, setCmd] = useState("");
  const [schemaText, setSchemaText] = useState("");
  const [metricName, setMetricName] = useState("");
  const [metricOp, setMetricOp] = useState(">=");
  const [metricValue, setMetricValue] = useState(0.8);
  const [hint, setHint] = useState("");
  const [workspaceFiles, setWorkspaceFiles] = useState<WorkspaceFile[]>([]);
  const [maxIterations, setMaxIterations] = useState(10);
  const [maxTotalTokens, setMaxTotalTokens] = useState(50_000);
  const [maxCostUsd, setMaxCostUsd] = useState(1.0);
  const [error, setError] = useState<string | null>(null);

  // 前置检查：Loop 由 Worker 用「项目默认模型配置」装配 LLM。缺配置时后端会
  // 回退到环境变量（resolver.py），所以这是提示而非硬阻断。
  const models = useQuery({
    queryKey: ["model-configs"],
    queryFn: api.listModelConfigs,
    staleTime: 30_000,
  });
  // 未加载完时不误报（后端 get_default 同时要求 is_default 与 is_active）
  const hasDefaultModel = models.data
    ? models.data.models.some((m) => m.is_default && m.is_active)
    : true;

  const activeKind = ASSERTION_KINDS.find((k) => k.value === kind);
  const activeMode = MODE_OPTIONS.find((m) => m.value === mode);

  // 这两条 submit 时都会拦，但选完模式的那一刻就已经能判定了。
  // 等到点「创建 Loop」才说，用户已经把断言和预算全填完了
  const hitlNeedsHuman = mode === "hitl" && kind !== "human";
  const retryTooManyIterations =
    mode === "retry" &&
    Number.isFinite(maxIterations) &&
    maxIterations > RETRY_ITERATIONS_ADVICE;

  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (spec: Record<string, unknown>) =>
      api.createLoop({
        task,
        mode,
        assertions: [{ id: "check", kind, spec, blocking: true, hint }],
        budget: {
          max_iterations: maxIterations,
          max_total_tokens: maxTotalTokens,
          max_cost_usd: maxCostUsd,
        },
        workspace: Object.fromEntries(
          workspaceFiles
            .filter((f) => f.path.trim())
            .map((f) => [f.path.trim(), f.content]),
        ),
      }),
    onSuccess: (created) => {
      setTask("");
      setPattern("");
      setCmd("");
      setSchemaText("");
      setMetricName("");
      setHint("");
      setWorkspaceFiles([]);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["loops"] });
      onCreated(created.loop_id);
    },
    onError: (e: unknown) => {
      setError(e instanceof ApiError ? e.message : "创建失败");
    },
  });

  /** 按断言类型组装 spec（键名与后端 verifier 读取的一致）。 */
  function buildSpec(): Record<string, unknown> | string {
    switch (kind) {
      case "regex":
        if (!pattern.trim()) return "请填写达标正则（输出的达标条件）";
        try {
          new RegExp(pattern);
        } catch {
          return "正则表达式语法无效";
        }
        return { pattern };
      case "command":
        if (!cmd.trim()) return "请填写要执行的命令";
        return { cmd };
      case "schema": {
        if (!schemaText.trim()) return "请填写 JSON Schema";
        try {
          return { schema: JSON.parse(schemaText) as unknown };
        } catch {
          return "JSON Schema 不是合法 JSON";
        }
      }
      case "metric":
        if (!metricName.trim()) return "请填写指标名称";
        if (!Number.isFinite(metricValue)) return "指标阈值必须是数字";
        return { name: metricName, op: metricOp, value: metricValue };
      case "human":
        return {};
      default:
        return "未知断言类型";
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!task.trim()) {
      setError("请填写任务目标");
      return;
    }
    // 后端 _check_mode_fit：hitl 模式没有 human 断言会直接判失败
    if (mode === "hitl" && kind !== "human") {
      setError("人工协同模式需要至少一条 human（人工审批）断言");
      return;
    }
    // 后端 max_tokens_per_iteration 默认 32000，且校验总量不得小于单轮上限
    // （goal_validation._check_budget）。不在这儿拦的话，填 20000 会吃一个
    // 提到"单轮上限"的 422 —— 而表单里根本没有这个字段。
    if (!Number.isFinite(maxTotalTokens) || maxTotalTokens < TOKENS_PER_ITERATION) {
      setError(
        `Token 预算不能低于单轮上限 ${TOKENS_PER_ITERATION.toLocaleString()}，` +
          "否则第一轮就会触发熔断",
      );
      return;
    }
    if (!Number.isInteger(maxIterations) || maxIterations < 1 || maxIterations > 50) {
      setError("最大轮次需为 1–50 的整数");
      return;
    }
    const unavailable = ASSERTION_KINDS.find((k) => k.value === kind)?.unavailable;
    if (unavailable) {
      setError(`「${activeKind?.label}」断言当前不可用：${unavailable}`);
      return;
    }
    if (!Number.isFinite(maxCostUsd) || maxCostUsd <= 0) {
      setError("预算上限必须大于 0");
      return;
    }
    // 路径穿越后端会再挡一次，但那时用户已经把文件内容全填完了
    const named = workspaceFiles.filter((f) => f.path.trim());
    for (const file of named) {
      const pathError = validateWorkspacePath(file.path);
      if (pathError) {
        setError(`工作目录文件「${file.path}」路径无效：${pathError}`);
        return;
      }
    }
    if (new Set(named.map((f) => f.path.trim())).size !== named.length) {
      setError("工作目录文件路径重复");
      return;
    }
    // command 断言验证的是磁盘文件；一个都不给，跑的就是个空目录
    if (kind === "command" && named.length === 0) {
      setError("命令断言需要至少一个工作目录文件作为验证对象");
      return;
    }
    const spec = buildSpec();
    if (typeof spec === "string") {
      setError(spec);
      return;
    }
    mutation.mutate(spec);
  }

  return (
    <form className="loop-create-form" onSubmit={submit}>
      {!hasDefaultModel && (
        <div className="callout callout-warn" role="alert">
          <strong>
            <TriangleAlert size={13} aria-hidden="true" /> 未设置默认模型配置
          </strong>
          <p>
            Loop 由 Worker 按项目默认模型装配；当前无默认配置，将回退到服务端环境变量。
            建议先去{" "}
            <Link to="/models" className="settings-link">
              模型配置
            </Link>{" "}
            指定一个默认模型。
          </p>
        </div>
      )}

      <label className="field" htmlFor="loop-task">
        <span>任务目标</span>
        <input
          id="loop-task"
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder="如：写一个返回两数之和的 Python 函数"
        />
      </label>

      <label className="field" htmlFor="loop-mode">
        <span>模式</span>
        <select
          id="loop-mode"
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          aria-describedby="loop-mode-desc"
        >
          {MODE_OPTIONS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
      </label>
      {activeMode && (
        <p className="hint" id="loop-mode-desc">
          {activeMode.desc}
        </p>
      )}

      <label className="field" htmlFor="loop-kind">
        <span>断言类型（达标判定方式）</span>
        <select
          id="loop-kind"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
          aria-describedby="loop-kind-desc"
        >
          {ASSERTION_KINDS.map((k) => (
            <option key={k.value} value={k.value} disabled={!!k.unavailable}>
              {k.label}
              {k.unavailable ? "（不可用）" : ""}
            </option>
          ))}
        </select>
      </label>
      {activeKind && (
        <p className="hint" id="loop-kind-desc">
          {activeKind.desc}
        </p>
      )}

      {/* 提供改法而不只是报冲突：这里唯一的解就是换断言类型，
          与其让用户自己去下拉里猜哪个才算 human，不如给一下 */}
      {hitlNeedsHuman && (
        <div className="callout callout-warn" role="alert">
          <strong>
            <TriangleAlert size={13} aria-hidden="true" /> 人工协同模式需要人工审批断言
          </strong>
          <p>
            当前断言是「{activeKind?.label}」，这样创建会被拒。
            <button type="button" className="btn btn-sm" onClick={() => setKind("human")}>
              改为人工审批
            </button>
          </p>
        </div>
      )}

      <AssertionSpecFields
        kind={kind}
        pattern={pattern}
        onPattern={setPattern}
        cmd={cmd}
        onCmd={setCmd}
        schemaText={schemaText}
        onSchemaText={setSchemaText}
        metricName={metricName}
        onMetricName={setMetricName}
        metricOp={metricOp}
        onMetricOp={setMetricOp}
        metricValue={metricValue}
        onMetricValue={setMetricValue}
      />

      <label className="field" htmlFor="loop-hint">
        <span>失败提示（可选，给模型的定向 hint）</span>
        <input
          id="loop-hint"
          value={hint}
          onChange={(e) => setHint(e.target.value)}
          placeholder="如：输出必须包含一个函数定义"
        />
      </label>

      {/* command 断言必须有验证对象；其余类型只看输出字符串，文件是多余的 */}
      {kind === "command" && (
        <WorkspaceFilesField files={workspaceFiles} onChange={setWorkspaceFiles} />
      )}

      <div className="field-row">
        <label className="field" htmlFor="loop-max-iterations">
          <span>最大轮次</span>
          <input
            id="loop-max-iterations"
            type="number"
            min={1}
            max={50}
            value={maxIterations}
            onChange={(e) => setMaxIterations(Number(e.target.value))}
            {...(retryTooManyIterations
              ? { "aria-describedby": "loop-iterations-advice" }
              : {})}
          />
        </label>
        <label className="field" htmlFor="loop-max-cost">
          <span>预算上限（$）</span>
          <input
            id="loop-max-cost"
            type="number"
            min={0.01}
            step={0.01}
            value={maxCostUsd}
            onChange={(e) => setMaxCostUsd(Number(e.target.value))}
          />
        </label>
      </div>

      {/* 只是建议，后端不拦，所以不做成 error 也不自动改小 ——
          表单默认给的就是 10，用户没做错任何事，不该被当成填错了 */}
      {retryTooManyIterations && (
        <p className="hint" id="loop-iterations-advice">
          重试模式跑 {RETRY_ITERATIONS_ADVICE} 轮还不成，基本说明不是暂时性故障，
          再加轮次只是多花钱。仍可创建。
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => setMaxIterations(RETRY_ITERATIONS_ADVICE)}
          >
            改为 {RETRY_ITERATIONS_ADVICE} 轮
          </button>
        </p>
      )}

      <label className="field" htmlFor="loop-max-tokens">
        <span>Token 预算（累计上限）</span>
        <input
          id="loop-max-tokens"
          type="number"
          min={TOKENS_PER_ITERATION}
          step={1000}
          value={maxTotalTokens}
          onChange={(e) => setMaxTotalTokens(Number(e.target.value))}
          aria-describedby="loop-max-tokens-desc"
        />
      </label>
      <p className="hint" id="loop-max-tokens-desc">
        不能低于单轮上限 {TOKENS_PER_ITERATION.toLocaleString()}
        ，否则第一轮就会触发熔断。
      </p>

      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" disabled={mutation.isPending} className="btn btn-primary">
        {mutation.isPending ? "创建中…" : "创建 Loop"}
      </button>
    </form>
  );
}

