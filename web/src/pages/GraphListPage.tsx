/**
 * 图编排列表页 —— 列出所有工作流图，新建入口。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useId, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api, ApiError } from "@/api/client";
import type { GraphResponse } from "@/api/types";
import { TableSkeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import { ErrorBox } from "@/pages/TraceListPage";

const QUERY_KEY = ["graphs"] as const;

export function GraphListPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const nameInputId = useId();
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("new-graph");
  const [nameError, setNameError] = useState<string | null>(null);

  const { data, isPending, error } = useQuery({
    queryKey: QUERY_KEY,
    queryFn: api.listGraphs,
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteGraph(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      toast.success("已删除");
    },
  });

  const createMutation = useMutation({
    mutationFn: (data: { name: string; graph: Record<string, unknown> }) =>
      api.createGraph(data),
    onSuccess: (graph: GraphResponse) => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEY });
      navigate(`/graphs/${graph.id}`);
    },
  });

  // 命名改为页面内行内表单，不再 window.prompt
  const submitCreate = () => {
    const name = newName.trim();
    if (!name) {
      setNameError("请填写图名称");
      return;
    }
    setNameError(null);
    createMutation.mutate({
      name,
      graph: { version: "1", graph: { version: "1", nodes: [], edges: [] } },
    });
  };

  const graphs = data ?? [];

  // 新建入口：错误态/空态下都必须在，否则用户无路可走
  const createControls = creating ? (
    <div className="page-actions">
      <label htmlFor={nameInputId} className="config-label">
        图名称
      </label>
      <input
        id={nameInputId}
        type="text"
        value={newName}
        onChange={(e) => {
          setNewName(e.target.value);
          if (nameError) setNameError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") submitCreate();
          if (e.key === "Escape") setCreating(false);
        }}
        placeholder="图名称"
        /* 后端 max_length=100（graphs.py:39）。不挡的话超长名字要等提交才 422 */
        maxLength={100}
        aria-invalid={nameError !== null}
        autoFocus
      />
      <button
        type="button"
        className="btn btn-primary"
        onClick={submitCreate}
        disabled={createMutation.isPending}
      >
        {createMutation.isPending ? "创建中…" : "创建"}
      </button>
      <button
        type="button"
        className="btn"
        onClick={() => {
          setCreating(false);
          setNameError(null);
        }}
        disabled={createMutation.isPending}
      >
        取消
      </button>
    </div>
  ) : (
    <button type="button" className="btn btn-primary" onClick={() => setCreating(true)}>
      <Plus size={13} aria-hidden />
      新建图
    </button>
  );

  return (
    <div className="page">
      <div className="page-header">
        <h1>工作流编排</h1>
        {createControls}
      </div>

      {nameError !== null && (
        <p className="form-error" role="alert">
          {nameError}
        </p>
      )}

      {/* 列表取数失败也保留新建入口，不再整页早退 */}
      {error && <ErrorBox error={error} />}

      {createMutation.error && (
        <p className="form-error" role="alert">
          创建失败：{(createMutation.error as ApiError).message}
        </p>
      )}

      {deleteMutation.error && (
        <p className="form-error" role="alert">
          删除失败：{(deleteMutation.error as ApiError).message}
        </p>
      )}

      {isPending ? (
        <TableSkeleton cols={5} rows={6} />
      ) : graphs.length === 0 ? (
        <div className="empty-state">
          <p>暂无工作流图。</p>
          {!creating && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => setCreating(true)}
            >
              <Plus size={13} aria-hidden />
              新建图
            </button>
          )}
        </div>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">名称</th>
              <th scope="col">版本</th>
              <th scope="col">描述</th>
              <th scope="col">校验</th>
              <th scope="col">操作</th>
            </tr>
          </thead>
          <tbody>
            {graphs.map((g) => (
              <tr key={g.id}>
                <td>
                  <Link to={`/graphs/${g.id}`}>{g.name}</Link>
                </td>
                <td className="num">v{g.version}</td>
                <td>{g.description || "—"}</td>
                <td>
                  {g.validation_errors.length > 0 ? (
                    <span className="badge warn">
                      {g.validation_errors.length} 个问题
                    </span>
                  ) : (
                    <span className="badge ok">通过</span>
                  )}
                </td>
                <td>
                  <button
                    type="button"
                    className="btn small btn-danger"
                    aria-label={`删除图 ${g.name}`}
                    disabled={
                      deleteMutation.isPending && deleteMutation.variables === g.id
                    }
                    onClick={() => {
                      if (window.confirm(`删除图 ${g.name}？`)) {
                        deleteMutation.mutate(g.id);
                      }
                    }}
                  >
                    <Trash2 size={13} aria-hidden />
                    {deleteMutation.isPending && deleteMutation.variables === g.id
                      ? "删除中…"
                      : "删除"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
