/**
 * Playground 页 —— 从 span 复现 → 多配置并排对比 → 固化为 spec。
 *
 * 调试闭环的核心。API 不直接调 LLM（不持有密钥），返回的是组装好的请求，
 * 真实输出要接客户端的 LLM 调用层 —— 页面顶部明说，不装成真结果。
 *
 * 支持深链 /playground?trace_id=…&span_id=…（Span 详情抽屉的「复现」入口），
 * 落地即自动提取，不必手抄两个 uuid。
 */

import { useMutation, useQuery } from "@tanstack/react-query";
import { Info, Play, Plus } from "lucide-react";
import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "@/api/client";
import type { ConfigResult, LLMConfig, ReproduceResponse } from "@/api/types";
import {
  ConfigEditor,
  OriginalOutput,
  ResultCard,
} from "@/components/playground/PlaygroundPanels";
import { FreezePanel, ReproducePanel } from "@/components/playground/PlaygroundSidebar";
import { toast } from "@/components/Toast";
import { inspectSpec, type SpecInspection } from "@/lib/spec-guard";
import { ErrorBox } from "@/pages/TraceListPage";

/** 与后端 MAX_COMPARE_CONFIGS 对齐，多传会被拒 */
const MAX_CONFIGS = 8;
/** 后端 prompt 的 max_length（playground.py:46/67），越界会 422 */
const MAX_PROMPT = 50_000;

/** 画布上的配置带稳定 id：用数组下标做 key 时删中间一项会让后面的草稿串位 */
interface DraftConfig {
  id: number;
  config: LLMConfig;
}

function blankConfig(model: string): LLMConfig {
  return { model, temperature: 0.7, max_tokens: 4096, system_prompt: "" };
}

export function PlaygroundPage() {
  const [searchParams] = useSearchParams();
  const modelListId = useId();
  const promptInputId = useId();

  const [prompt, setPrompt] = useState("");
  const [drafts, setDrafts] = useState<DraftConfig[]>([]);
  const [results, setResults] = useState<ConfigResult[]>([]);
  const [freezeTask, setFreezeTask] = useState("");
  // 固化哪一组配置由用户选，而不是闷着头取第一组：
  // 对比三组参数之后，"第一组"通常不是那个跑赢的
  const [freezeId, setFreezeId] = useState<number | null>(null);
  const [specYaml, setSpecYaml] = useState("");
  // 检查结论而非 spec_dict：只在生成时算一次，且不给后续渲染留下重算的机会
  const [specCheck, setSpecCheck] = useState<SpecInspection | null>(null);
  const [traceId, setTraceId] = useState("");
  const [spanId, setSpanId] = useState("");
  const [source, setSource] = useState<ReproduceResponse | null>(null);

  const nextIdRef = useRef(0);
  const seededRef = useRef(false);
  const autoRunRef = useRef(false);

  const makeDraft = useCallback(
    (model: string): DraftConfig => ({ id: ++nextIdRef.current, config: blankConfig(model) }),
    [],
  );

  // 候选模型取用户自己配好的那些，而不是写死一张厂商清单
  const modelsQuery = useQuery({
    queryKey: ["model-configs"],
    queryFn: api.listModelConfigs,
    staleTime: 30_000,
  });

  const modelOptions = useMemo(() => {
    const active = (modelsQuery.data?.models ?? []).filter((m) => m.is_active);
    const ordered = [...active].sort((a, b) => Number(b.is_default) - Number(a.is_default));
    return [...new Set(ordered.map((m) => m.model))];
  }, [modelsQuery.data]);

  // 初始配置也从已配置模型来：默认模型排头，没配就留一条空的等用户填
  useEffect(() => {
    if (seededRef.current || modelsQuery.isPending) return;
    seededRef.current = true;
    setDrafts(
      modelOptions.length > 0
        ? modelOptions.slice(0, 2).map((m) => makeDraft(m))
        : [makeDraft("")],
    );
  }, [modelsQuery.isPending, modelOptions, makeDraft]);

  // 结果对应的是"当时那份输入"。改了参数不清空的话，屏幕上会同时摆着
  // 新参数和旧数字，用户以为在比 A 和 B，其实在比 A 和上一次的 B。
  const inputSignature = useMemo(
    () => JSON.stringify([prompt, drafts.map((d) => d.config)]),
    [prompt, drafts],
  );
  const [resultSignature, setResultSignature] = useState("");
  const stale = results.length > 0 && resultSignature !== inputSignature;

  const compareMutation = useMutation({
    mutationFn: (payload: { prompt: string; configs: LLMConfig[] }) =>
      api.playgroundCompare(payload.prompt, payload.configs),
    onSuccess: (data, variables) => {
      setResults(data.results);
      setResultSignature(JSON.stringify([variables.prompt, variables.configs]));
    },
  });

  const freezeMutation = useMutation({
    mutationFn: (payload: { task: string; model: string; temperature: number }) =>
      api.playgroundFreeze(payload),
    onSuccess: (data) => {
      setSpecYaml(data.spec_yaml);
      // 从返回的 spec_dict 现算，不写死"固化必有占位断言"：
      // 后端哪天支持真实断言了，警告会自己消失，不用回来改这里
      const check = inspectSpec(data.spec_dict);
      setSpecCheck(check);
      // 不报 success：请求确实成功了，但拿到一份什么都不校验的 spec
      // 不是用户想要的结果，绿色对勾会让人直接拿去用
      if (check.alwaysPasses) {
        toast.error("spec 已生成，但它不校验任何输出");
      } else {
        toast.success("已生成 spec.yaml");
      }
    },
  });

  const reproduceMutation = useMutation({
    mutationFn: (payload: { traceId: string; spanId: string }) =>
      api.playgroundReproduce(payload.traceId, payload.spanId),
    onSuccess: (data) => {
      seededRef.current = true;
      setPrompt(data.prompt);
      setDrafts([{ id: ++nextIdRef.current, config: data.config }]);
      setSource(data);
      setResults([]);
      toast.success("已载入原始输入与配置");
    },
  });

  // 深链落地：自动填两个 id 并立即提取
  useEffect(() => {
    if (autoRunRef.current) return;
    const t = searchParams.get("trace_id");
    const s = searchParams.get("span_id");
    if (!t || !s) return;
    autoRunRef.current = true;
    setTraceId(t);
    setSpanId(s);
    reproduceMutation.mutate({ traceId: t, spanId: s });
  }, [searchParams, reproduceMutation]);

  const updateConfig = useCallback((index: number, config: LLMConfig) => {
    setDrafts((prev) => {
      const target = prev[index];
      if (!target) return prev;
      const next = [...prev];
      next[index] = { ...target, config };
      return next;
    });
  }, []);

  const removeConfig = useCallback((index: number) => {
    setDrafts((prev) => prev.filter((_, i) => i !== index));
  }, []);

  const addConfig = () => {
    if (drafts.length >= MAX_CONFIGS) return;
    const model = modelOptions[drafts.length % Math.max(modelOptions.length, 1)] ?? "";
    setDrafts((prev) => [...prev, makeDraft(model)]);
  };

  const copySpec = async () => {
    try {
      await navigator.clipboard.writeText(specYaml);
      toast.success("spec.yaml 已复制");
    } catch {
      // 非 https 源或权限被拒时 writeText 会 reject，静默失败会让人以为复制成功
      toast.error("复制失败，请手动选中下方内容");
    }
  };

  const missingModel = drafts.some((d) => !d.config.model.trim());
  const canCompare = prompt.trim().length > 0 && drafts.length > 0 && !missingModel;
  const error = compareMutation.error ?? freezeMutation.error ?? reproduceMutation.error;

  // 选中的那组可能已被删掉，兜回第一组而不是留一个悬空引用
  const freezeTarget = drafts.find((d) => d.id === freezeId) ?? drafts[0];
  const freezeIndex = freezeTarget ? drafts.indexOf(freezeTarget) : -1;
  // 后端 model 是 min_length=1，空模型发过去就是 422，不如在这儿就拦住
  const canFreeze =
    !!freezeTarget?.config.model.trim() && (!!freezeTask.trim() || !!prompt.trim());

  return (
    <div className="page playground-page">
      <div className="page-header">
        <h1>Playground</h1>
        <p className="hint">从 Span 复现 · 多配置并排对比 · 固化为 spec</p>
      </div>

      <div className="callout callout-info">
        <Info size={14} aria-hidden />
        <div>
          <strong>此页不直接调用模型</strong>
          <p>
            服务端不持有密钥，「并排对比」返回的是组装好的请求与成本估算，输出需由客户端的
            LLM 调用层填充。用它比参数与成本，不要当成真实回答。
          </p>
        </div>
      </div>

      {error && <ErrorBox error={error as Error} />}

      <datalist id={modelListId}>
        {modelOptions.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>

      <div className="playground-layout">
        <div className="playground-main">
          <div className="playground-section">
            <div className="section-header">
              <h3>
                <label htmlFor={promptInputId}>Prompt</label>
              </h3>
            </div>
            <textarea
              id={promptInputId}
              className="prompt-input"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              onPaste={(e) => {
                // maxLength 会静默截断：粘一份超长 prompt 时用户不知道自己
                // 丢了多少字符，对比结果与预期对不上也只能干瞪眼。在这里
                // 点破 —— 截断是后端硬限制，不截断发过去就是 422。
                const el = e.currentTarget as HTMLTextAreaElement;
                // onclick 时刻选区还完好：选中的部分会被粘贴内容替换
                const replaced = el.selectionEnd - el.selectionStart;
                const room = MAX_PROMPT - (prompt.length - replaced);
                const text = e.clipboardData.getData("text");
                if (text.length > room) {
                  toast.error(
                    `Prompt 超长：已截断到 ${MAX_PROMPT.toLocaleString()} 字符` +
                      `（本次粘贴 ${text.length.toLocaleString()} 字符，仅保留前 ${Math.max(room, 0).toLocaleString()}）`,
                  );
                }
              }}
              rows={6}
              maxLength={MAX_PROMPT}
              placeholder="输入要测试的 prompt…"
            />
          </div>

          {source && <OriginalOutput source={source} />}

          <div className="playground-section">
            <div className="section-header">
              <h3>配置对比（{drafts.length}）</h3>
              <button
                type="button"
                className="btn btn-sm"
                onClick={addConfig}
                disabled={drafts.length >= MAX_CONFIGS}
                title={drafts.length >= MAX_CONFIGS ? `最多 ${MAX_CONFIGS} 组` : undefined}
              >
                <Plus size={12} aria-hidden />
                添加配置
              </button>
            </div>

            {modelOptions.length === 0 && !modelsQuery.isPending && (
              <p className="hint">
                还没有可用的模型配置。<Link to="/models">去添加</Link>
                后这里会给出候选，也可以直接手输模型名。
              </p>
            )}

            <div className="configs-grid">
              {drafts.map((draft, i) => (
                <ConfigEditor
                  key={draft.id}
                  config={draft.config}
                  index={i}
                  modelListId={modelListId}
                  onChange={updateConfig}
                  onRemove={removeConfig}
                />
              ))}
            </div>
          </div>

          <div className="playground-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() =>
                compareMutation.mutate({
                  prompt,
                  configs: drafts.map((d) => d.config),
                })
              }
              disabled={!canCompare || compareMutation.isPending}
            >
              <Play size={13} aria-hidden />
              {compareMutation.isPending ? "对比中…" : "并排对比"}
            </button>
            {!canCompare && (
              <span className="hint">
                {prompt.trim() ? "每组配置都要填模型名" : "先填 Prompt"}
              </span>
            )}
          </div>

          {results.length > 0 && (
            <div className={`playground-section${stale ? " is-stale" : ""}`}>
              <div className="section-header">
                <h3>对比结果（{results.length}）</h3>
                {stale && (
                  <span className="stale-badge">
                    <Info size={12} aria-hidden />
                    参数已改动，下方是上一次的结果
                  </span>
                )}
              </div>
              <div className="results-grid">
                {results.map((result, i) => (
                  <ResultCard key={`${result.config.model}-${i}`} result={result} />
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="playground-sidebar">
          <ReproducePanel
            traceId={traceId}
            spanId={spanId}
            isPending={reproduceMutation.isPending}
            onTraceIdChange={setTraceId}
            onSpanIdChange={setSpanId}
            onReproduce={() => reproduceMutation.mutate({ traceId, spanId })}
          />

          <FreezePanel
            task={freezeTask}
            drafts={drafts}
            targetId={freezeTarget?.id}
            targetIndex={freezeIndex}
            targetModel={freezeTarget?.config.model ?? ""}
            canFreeze={canFreeze}
            isPending={freezeMutation.isPending}
            specYaml={specYaml}
            specCheck={specCheck}
            onTaskChange={setFreezeTask}
            onTargetChange={setFreezeId}
            onFreeze={() => {
              if (!freezeTarget) return;
              freezeMutation.mutate({
                task: freezeTask.trim() || prompt.slice(0, 100),
                model: freezeTarget.config.model,
                temperature: freezeTarget.config.temperature,
              });
            }}
            onCopySpec={() => void copySpec()}
          />
        </div>
      </div>
    </div>
  );
}
