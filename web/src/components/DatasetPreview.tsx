/**
 * 数据集样本预览 —— 版本行点开看前几条，确认内容对得上预期再导出。
 *
 * 不展示全部：数据集可能上千条，全量渲染列表既慢又没必要，
 * 这里只摘要 + 引导导出 JSONL 看完整内容。
 */

import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import { TableSkeleton } from "@/components/Skeleton";
import { ErrorBox } from "@/pages/TraceListPage";

const PREVIEW_ROWS = 5;

/** 单条样本的展示：input 在前、expected 在后，空 expected 不占格 */
function SampleRow({ input, expected }: { input: string; expected: string | null }) {
  return (
    <div className="sample-row">
      <span className="cell-wrap mono">{input}</span>
      {expected !== null && (
        <span className="cell-wrap mono sample-expected">{expected}</span>
      )}
    </div>
  );
}

export function DatasetPreview({ name, version }: { name: string; version: number }) {
  const { data, isPending, error } = useQuery({
    queryKey: ["dataset", name, version],
    queryFn: () => api.getDataset(name, version),
  });

  if (isPending) return <TableSkeleton cols={1} rows={3} />;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  const { items, item_count, content_hash } = data;
  const shown = items.slice(0, PREVIEW_ROWS);

  return (
    <div className="dataset-preview">
      <p className="hint">
        v{version} · {item_count} 条 · 哈希{" "}
        <code className="mono">{content_hash}</code> · 以下为前 {shown.length} 条
      </p>
      <div className="sample-list">
        {shown.map((item) => (
          <SampleRow
            key={item.item_id}
            input={item.input}
            expected={item.expected}
          />
        ))}
      </div>
    </div>
  );
}