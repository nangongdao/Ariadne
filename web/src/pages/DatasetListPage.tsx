import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Fragment, useState } from "react";

import { ApiError, api, getApiBase, getApiKey } from "@/api/client";
import type { ProblemDetail } from "@/api/types";
import { DatasetPreview } from "@/components/DatasetPreview";
import { TableSkeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import { ErrorBox } from "@/pages/TraceListPage";

const COLUMN_COUNT = 3;

export function DatasetListPage() {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);

  const { data, isPending, error, refetch, isFetching } = useQuery({
    queryKey: ["datasets"],
    queryFn: api.listDatasets,
  });

  const datasets = data ?? [];

  /** version 省略即最新版。exporting 存的是按钮标识，历史版本各自独立置忙。 */
  async function handleExport(name: string, version?: number): Promise<void> {
    const key = version === undefined ? name : `${name}@${version}`;
    setExporting(key);
    try {
      await downloadJsonl(name, version);
      toast.success(
        version === undefined ? `已导出「${name}」` : `已导出「${name}」v${version}`,
      );
    } catch (err) {
      toast.error(`导出失败：${errorMsg(err)}`);
    } finally {
      setExporting(null);
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>数据集</h1>
        <span className="hint">版本不可变 —— 改内容只能创建新版本</span>
      </header>

      {error && <ErrorBox error={error} />}

      {isPending ? (
        <TableSkeleton cols={COLUMN_COUNT} rows={6} />
      ) : datasets.length === 0 ? (
        <div className="empty-state">
          <p>还没有数据集。</p>
          <p className="hint">
            用 <code>POST /v1/datasets</code> 或
            <code> POST /v1/datasets/import</code> 创建，创建后回到这里刷新。
          </p>
          <button
            type="button"
            className="btn small"
            onClick={() => void refetch()}
            disabled={isFetching}
          >
            {isFetching ? "刷新中…" : "刷新列表"}
          </button>
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">名称</th>
              <th scope="col" className="num">
                最新版本
              </th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((entry) => {
              const isOpen = expanded === entry.name;
              const panelId = `dataset-versions-${encodeURIComponent(entry.name)}`;
              return (
                <Fragment key={entry.name}>
                  <tr>
                    <td>
                      {/* 名称本身没有详情页，做成展开开关而不是假链接 */}
                      <button
                        type="button"
                        className="btn-ghost"
                        aria-expanded={isOpen}
                        aria-controls={panelId}
                        onClick={() => setExpanded(isOpen ? null : entry.name)}
                      >
                        {isOpen ? (
                          <ChevronDown size={14} aria-hidden />
                        ) : (
                          <ChevronRight size={14} aria-hidden />
                        )}
                        {entry.name}
                      </button>
                    </td>
                    <td className="num">v{entry.latest_version}</td>
                    <td>
                      <button
                        type="button"
                        className="btn small"
                        onClick={() => void handleExport(entry.name)}
                        disabled={exporting === entry.name}
                      >
                        {exporting === entry.name ? "导出中…" : "导出最新版"}
                      </button>
                    </td>
                  </tr>
                  {isOpen && (
                    <tr>
                      <td colSpan={COLUMN_COUNT} id={panelId}>
                        <VersionList
                          name={entry.name}
                          exporting={exporting}
                          onExport={handleExport}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

interface VersionListProps {
  name: string;
  /** 正在导出的按钮标识，形如 `name` 或 `name@version` */
  exporting: string | null;
  onExport: (name: string, version?: number) => Promise<void>;
}

function VersionList({ name, exporting, onExport }: VersionListProps) {
  const { data, isPending, error } = useQuery({
    queryKey: ["dataset-versions", name],
    queryFn: () => api.datasetVersions(name),
  });

  // 预览只开一个版本：`name@vN`，null 即全部收起
  const [previewing, setPreviewing] = useState<string | null>(null);

  if (isPending) return <p className="hint">加载版本…</p>;
  if (error) return <ErrorBox error={error} />;

  const versions = data ?? [];
  if (versions.length === 0) {
    return <p className="hint">该数据集还没有版本。</p>;
  }

  return (
    <table className="data-table">
      <thead>
        <tr>
          <th scope="col">版本</th>
          <th scope="col" className="num">
            样本数
          </th>
          <th scope="col">内容哈希</th>
          <th scope="col">操作</th>
        </tr>
      </thead>
      <tbody>
        {versions.map((version) => {
          const busy = exporting === `${name}@${version.version}`;
          const isPreviewing = previewing === `${name}@${version.version}`;
          const panelId = `dataset-preview-${encodeURIComponent(name)}-${version.version}`;
          return (
            <Fragment key={version.version}>
              <tr>
                <td>v{version.version}</td>
                <td className="num">{version.item_count}</td>
                <td
                  className="mono"
                  title="实验记录里存这个值，任何人都能据此确认数据集一致"
                >
                  {version.content_hash}
                </td>
                <td>
                  {/* 版本不可变，历史版本能单独导出才有意义 —— 复现实验要的正是当时那一版 */}
                  <button
                    type="button"
                    className="btn small"
                    onClick={() => void onExport(name, version.version)}
                    disabled={busy}
                  >
                    {busy ? "导出中…" : "导出此版"}
                  </button>
                  <button
                    type="button"
                    className="btn small"
                    aria-expanded={isPreviewing}
                    aria-controls={panelId}
                    onClick={() =>
                      setPreviewing(isPreviewing ? null : `${name}@${version.version}`)
                    }
                  >
                    {isPreviewing ? "收起" : "预览样本"}
                  </button>
                </td>
              </tr>
              {isPreviewing && (
                <tr>
                  <td colSpan={4} id={panelId}>
                    <DatasetPreview name={name} version={version.version} />
                  </td>
                </tr>
              )}
            </Fragment>
          );
        })}
      </tbody>
    </table>
  );
}

/** 从 Content-Disposition 取文件名。后端给的是 `{name}-v{version}.jsonl`。 */
function filenameFromResponse(response: Response, fallback: string): string {
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return match?.[1] ?? fallback;
}

/**
 * 导出端点要 API Key，浏览器直接跳转带不上头部，所以 fetch 取回再触发本地下载。
 * 地址必须走 getApiBase()：桌面端（Tauri）origin 是 tauri://localhost，
 * 相对路径 `/v1/...` 解析不到后端，必然 404。
 *
 * version 省略即导出最新版。文件名取后端的 Content-Disposition —— 那里带着
 * 版本号，自己拼 `${name}.jsonl` 会让下载下来的三个版本文件同名。
 */
async function downloadJsonl(name: string, version?: number): Promise<void> {
  const url = new URL(
    `/v1/datasets/${encodeURIComponent(name)}/export`,
    getApiBase(),
  );
  if (version !== undefined) url.searchParams.set("version", String(version));
  const response = await fetch(url, {
    headers: { "X-Ariadne-Key": getApiKey() },
  });

  if (!response.ok) {
    // 错误形状与 client.ts 一致，好让 ErrorBox / errorMsg 一视同仁地处理
    let problem: ProblemDetail | null = null;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      // 非 JSON 错误响应（网关 502 等）
    }
    throw new ApiError(
      response.status,
      problem,
      problem?.detail ?? `请求失败 (${response.status})`,
    );
  }

  const fallback = version === undefined ? `${name}.jsonl` : `${name}-v${version}.jsonl`;
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filenameFromResponse(response, fallback);
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // 立刻 revoke 会让部分 WebView 拿不到数据，留一拍再回收
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

function errorMsg(err: unknown): string {
  if (err instanceof ApiError) return err.problem?.detail ?? err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}
