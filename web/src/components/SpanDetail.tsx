/**
 * Span 详情抽屉。
 *
 * payload 分级（见 docs/06）意味着三种情况都要处理：
 * 内联全文 / zstd 压缩内联 / 外溢到对象存储只留预览。
 */

import { Copy, FlaskConical, X } from "lucide-react";
import { Link } from "react-router-dom";

import type { SpanNode } from "@/api/types";
import { KindBadge } from "@/components/KindBadge";
import { StatusDot } from "@/components/StatusDot";
import { toast } from "@/components/Toast";
import {
  formatAbsoluteTime,
  formatCost,
  formatDuration,
  formatTokens,
  isCompressedPreview,
  previewText,
} from "@/lib/format";
import { totalTokens } from "@/lib/tree";

interface Props {
  span: SpanNode | null;
  traceId: string;
  onClose: () => void;
}

/** id 是拿去别处搜的，不给复制按钮就只能手选长 uuid */
function CopyIdButton({ value, label }: { value: string; label: string }) {
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`${label}已复制`);
    } catch {
      // 非 https 源或权限被拒时 writeText 会 reject
      toast.error("复制失败，请手动选中");
    }
  };

  return (
    <button
      type="button"
      className="btn btn-icon"
      aria-label={`复制${label}`}
      title={`复制${label}`}
      onClick={() => void copy()}
    >
      <Copy size={12} aria-hidden />
    </button>
  );
}

export function SpanDetail({ span, traceId, onClose }: Props) {
  if (!span) {
    return (
      <aside className="span-detail empty">
        <p className="hint">点击左侧任意节点查看详情</p>
      </aside>
    );
  }

  const tokens = totalTokens(span);
  const attributeEntries = Object.entries(span.attributes).sort(([a], [b]) =>
    a.localeCompare(b),
  );

  // 用量区不能只看 kind === "llm"：树上任何 span 都会显示 token 与成本，
  // 非 llm 的 span（tool 包了一层调用、harness 汇总）也可能带上非零用量。
  // 只按 kind 判断的话，用户在树里看到成本、点进来却整块消失
  const isLlm = span.kind === "llm";
  const hasUsage =
    tokens > 0 || span.reasoning_tokens > 0 || Number.parseFloat(span.cost_usd) > 0;

  return (
    <aside className="span-detail" aria-label="Span 详情">
      <header className="detail-header">
        <div className="detail-title">
          <StatusDot status={span.status} showLabel />
          <KindBadge kind={span.kind} />
          <h2>{span.name}</h2>
        </div>
        <button type="button" className="close-btn" onClick={onClose} aria-label="关闭详情">
          <X size={14} aria-hidden />
        </button>
      </header>

      {span.kind === "llm" && (
        <div className="detail-actions">
          {/* 复现闭环入口：带上两个 id，Playground 落地即自动提取原始输入 */}
          <Link
            className="btn btn-sm"
            to={`/playground?trace_id=${encodeURIComponent(traceId)}&span_id=${encodeURIComponent(span.span_id)}`}
          >
            <FlaskConical size={13} aria-hidden />
            在 Playground 复现
          </Link>
        </div>
      )}

      <section className="detail-section">
        <h3>概要</h3>
        <dl className="kv">
          <dt>Span ID</dt>
          <dd className="mono dd-copy">
            <span className="ellipsis" title={span.span_id}>
              {span.span_id}
            </span>
            <CopyIdButton value={span.span_id} label="Span ID" />
          </dd>
          <dt>父 Span</dt>
          <dd className="mono">{span.parent_span_id || "（根节点）"}</dd>
          <dt>开始时间</dt>
          <dd>{formatAbsoluteTime(span.started_at)}</dd>
          <dt>总耗时</dt>
          <dd>{formatDuration(span.duration_ms)}</dd>
          <dt>自身耗时</dt>
          <dd title="排除子节点后的耗时，定位真实瓶颈用这个">
            {formatDuration(span.self_ms)}
          </dd>
          {span.operation && (
            <>
              <dt>操作</dt>
              <dd>{span.operation}</dd>
            </>
          )}
          {span.error_type && (
            <>
              <dt>错误类型</dt>
              <dd className="error-text">{span.error_type}</dd>
            </>
          )}
        </dl>
      </section>

      {(isLlm || hasUsage) && (
        <section className="detail-section">
          <h3>{isLlm ? "模型与用量" : "用量"}</h3>
          <dl className="kv">
            {/* 模型三项只对 llm 有意义：非 llm span 上它们必然是空，
                摆三行"—"只是让人怀疑数据丢了 */}
            {isLlm && (
              <>
                <dt>Provider</dt>
                <dd>{span.provider || "—"}</dd>
                <dt>请求模型</dt>
                <dd className="mono">{span.model_request || "—"}</dd>
                <dt>响应模型</dt>
                <dd
                  className="mono"
                  title="provider 侧的别名解析结果，与请求模型不同时影响可复现性"
                >
                  {span.model_response || "—"}
                  {span.model_response &&
                    span.model_request &&
                    span.model_response !== span.model_request && (
                      <span className="badge-warn tag-sm">版本已解析</span>
                    )}
                </dd>
              </>
            )}
            <dt>输入 Token</dt>
            <dd>{formatTokens(span.input_tokens)}</dd>
            <dt>输出 Token</dt>
            <dd>{formatTokens(span.output_tokens)}</dd>
            {span.cache_read_tokens > 0 && (
              <>
                <dt>缓存读取</dt>
                <dd title="按折扣价计费">
                  {formatTokens(span.cache_read_tokens)}
                  <span className="badge-good tag-sm">折扣计费</span>
                </dd>
              </>
            )}
            {span.cache_write_tokens > 0 && (
              <>
                <dt>缓存写入</dt>
                <dd>{formatTokens(span.cache_write_tokens)}</dd>
              </>
            )}
            {span.reasoning_tokens > 0 && (
              <>
                <dt>推理 Token</dt>
                {/* 推理 token 已经算在输出里了，再加一次是重复计费。
                    但它就贴在「合计」上面，不标一句，看的人会去竖着加一遍
                    然后发现加不出合计 */}
                <dd title="已包含在输出 Token 内，不重复计入合计">
                  {formatTokens(span.reasoning_tokens)}
                  {/* .badge 自带中性配色，不需要额外变体 */}
                  <span className="badge tag-sm">含在输出内</span>
                </dd>
              </>
            )}
            <dt>合计</dt>
            <dd title="输入 + 输出 + 缓存读写；推理 Token 已在输出内，不另计">
              {formatTokens(tokens)}
            </dd>
            <dt>成本</dt>
            <dd className="cost-value">{formatCost(span.cost_usd)}</dd>
          </dl>
        </section>
      )}

      <PayloadBlock
        title="输入"
        preview={span.input_preview}
        objectRef={span.input_ref}
      />
      <PayloadBlock
        title="输出"
        preview={span.output_preview}
        objectRef={span.output_ref}
      />

      {attributeEntries.length > 0 && (
        <section className="detail-section">
          <h3>属性 ({attributeEntries.length})</h3>
          <dl className="kv kv-dense">
            {attributeEntries.map(([key, value]) => (
              <div key={key} className="kv-row">
                <dt className="mono">{key}</dt>
                <dd className="mono">{value}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}

      {span.loop_id && (
        <section className="detail-section">
          <h3>Loop 关联</h3>
          <dl className="kv">
            <dt>Loop ID</dt>
            <dd className="mono">{span.loop_id}</dd>
            <dt>轮次</dt>
            <dd>{span.iteration}</dd>
          </dl>
        </section>
      )}

      {span.tags.length > 0 && (
        <section className="detail-section">
          <h3>标签</h3>
          <div className="tag-list">
            {span.tags.map((tag) => (
              <span key={tag} className="tag">
                {tag}
              </span>
            ))}
          </div>
        </section>
      )}
    </aside>
  );
}

function PayloadBlock({
  title,
  preview,
  objectRef,
}: {
  title: string;
  preview: string;
  objectRef: string;
}) {
  if (!preview && !objectRef) return null;

  const spilled = Boolean(objectRef);
  const compressed = isCompressedPreview(preview);

  return (
    <section className="detail-section">
      <h3>
        {title}
        {spilled && <span className="badge-info">已外溢</span>}
        {compressed && <span className="badge-info">已压缩</span>}
      </h3>
      <pre className="payload">{previewText(preview, objectRef)}</pre>
      {spilled && (
        <p className="hint mono" title={objectRef}>
          对象键：{objectRef}
        </p>
      )}
    </section>
  );
}
