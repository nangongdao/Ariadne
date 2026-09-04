/**
 * 模型配置页 —— 用户自定义 LLM provider（名称 / model / api_key / base_url）。
 *
 * 这是「摆脱 .env 在 UI 里配模型」的核心入口：用户可命名多个模型端点
 * （工作用 GPT-4o、本地 Ollama、OneAPI 网关…），任选一个设为项目默认，
 * Loop Worker 会优先用默认配置装配 LLM（见后端 resolver）。
 *
 * 本文件只管数据流与编排：取列表、增删改、草稿状态、行级忙态归属。
 * 表单见 components/model-config/ModelConfigForm，卡片见同目录的
 * ModelCardGrid，provider 选择/复制/删除确认见 ModelConfigControls，
 * 载荷转换与掩码见 lib/model-config。
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus } from "lucide-react";
import type { FormEvent } from "react";
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { api } from "@/api/client";
import type { ModelConfig } from "@/api/types";
import { EmptyState, ModelCardGrid } from "@/components/model-config/ModelCardGrid";
import { ModelConfigForm } from "@/components/model-config/ModelConfigForm";
import { CardGridSkeleton } from "@/components/Skeleton";
import { toast } from "@/components/Toast";
import type { DraftConfig } from "@/lib/model-config";
import {
  draftFromConfig,
  EMPTY_DRAFT,
  errorMsg,
  toCreatePayload,
  toUpdatePayload,
} from "@/lib/model-config";
import { navigationGuard } from "@/lib/navigation-guard";

export function ModelConfigPage() {
  const queryClient = useQueryClient();
  const location = useLocation();
  const navigate = useNavigate();
  const [showForm, setShowForm] = useState(false);
  const [draft, setDraft] = useState<DraftConfig>(EMPTY_DRAFT);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formError, setFormError] = useState("");
  // 命令面板「新建模型配置」经 location.state.openForm 直达表单
  const [pendingOpen, setPendingOpen] = useState(false);

  const list = useQuery({
    queryKey: ["model-configs"],
    queryFn: api.listModelConfigs,
  });

  // 收到 openForm 信号：先清 state（防刷新/回退重复弹表单）
  useEffect(() => {
    if ((location.state as { openForm?: boolean } | null)?.openForm === true) {
      setPendingOpen(true);
      navigate(location.pathname, { replace: true, state: null });
    }
  }, [location.state, location.pathname, navigate]);

  // 信号就绪且列表加载完成：决定「设为默认」勾选并展开表单
  useEffect(() => {
    if (pendingOpen && list.isSuccess) {
      setDraft({ ...EMPTY_DRAFT, is_default: !list.data.models.length });
      setShowForm(true);
      setFormError("");
      setPendingOpen(false);
    }
  }, [pendingOpen, list.isSuccess, list.data]);

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["model-configs"] });

  // 草稿守卫：表单开着且填了实质内容时，侧栏/Ctrl+K/顶栏跳转会静默丢草稿。
  // 用 ref 镜像让守卫回调总能读到最新草稿（effect 只需注册一次）。
  // 画布级守卫由图编辑器注册，这里注册的是表单级；卸载时注销
  const formOpenRef = useRef(showForm);
  formOpenRef.current = showForm;
  const draftRef = useRef(draft);
  draftRef.current = draft;
  useEffect(() => {
    return navigationGuard.register(() => {
      if (!formOpenRef.current) return true;
      const d = draftRef.current;
      const hasContent =
        d.name.trim() !== "" ||
        d.model.trim() !== "" ||
        d.base_url.trim() !== "" ||
        d.degraded_model.trim() !== "" ||
        d.api_key.trim() !== "";
      if (!hasContent) return true;
      return window.confirm("表单里有未保存的内容，离开将丢弃。确定离开？");
    });
  }, []);

  const createMutation = useMutation({
    mutationFn: api.createModelConfig,
    onSuccess: (_res, variables) => {
      // 响应含一次性明文 api_key，只取 name 提示，不读也不存 _res
      toast.success(`已创建「${variables.name}」`);
      void invalidate();
      resetForm();
    },
    onError: (err) => setFormError(errorMsg(err)),
  });

  function resetForm() {
    setShowForm(false);
    setEditingId(null);
    setDraft(EMPTY_DRAFT);
    setFormError("");
    // 明文 api_key 还留在 mutation.data / variables 里，主动清掉
    createMutation.reset();
  }

  const updateMutation = useMutation({
    mutationFn: (args: { id: string; data: ReturnType<typeof toUpdatePayload> }) =>
      api.updateModelConfig(args.id, args.data),
    onSuccess: () => {
      toast.success("已保存修改");
      void invalidate();
      resetForm();
    },
    onError: (err) => setFormError(errorMsg(err)),
  });

  const deleteMutation = useMutation({
    mutationFn: api.deleteModelConfig,
    onSuccess: () => {
      toast.success("配置已删除");
      void invalidate();
    },
    onError: (err) => toast.error(`删除失败：${errorMsg(err)}`),
  });

  const setDefaultMutation = useMutation({
    mutationFn: (id: string) => api.updateModelConfig(id, { is_default: true }),
    onSuccess: () => {
      toast.success("已设为项目默认");
      void invalidate();
    },
    onError: (err) => toast.error(`操作失败：${errorMsg(err)}`),
  });

  const cryptoAvailable = list.data?.cryptography_available ?? true;
  const models = list.data?.models ?? [];
  const submitting = createMutation.isPending || updateMutation.isPending;
  // variables 就是 mutate 时传的 id，用它把忙态钉在具体那一行
  const deletingId = deleteMutation.isPending ? (deleteMutation.variables ?? null) : null;
  const defaultingId = setDefaultMutation.isPending
    ? (setDefaultMutation.variables ?? null)
    : null;

  function openCreate() {
    setEditingId(null);
    setDraft({ ...EMPTY_DRAFT, is_default: !models.length });
    setShowForm(true);
    setFormError("");
  }

  function startEdit(cfg: ModelConfig) {
    setEditingId(cfg.id);
    setShowForm(true);
    setDraft(draftFromConfig(cfg));
    setFormError("");
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    setFormError("");
    if (editingId) {
      updateMutation.mutate({ id: editingId, data: toUpdatePayload(draft) });
    } else {
      createMutation.mutate(toCreatePayload(draft));
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>模型配置</h1>
        <div className="models-header-actions">
          {!showForm && (
            <button type="button" className="btn btn-primary btn-tall" onClick={openCreate}>
              <Plus size={14} aria-hidden />
              新建配置
            </button>
          )}
        </div>
      </header>

      {!cryptoAvailable && (
        <div className="models-warn" role="status">
          <KeyRound size={14} aria-hidden />
          <span>
            后端未安装 <code>cryptography</code>，api_key 当前以明文存储。生产环境请
            安装该依赖以启用 Fernet 加密。
          </span>
        </div>
      )}

      {showForm && (
        <ModelConfigForm
          draft={draft}
          editing={editingId !== null}
          submitting={submitting}
          error={formError}
          onChange={(patch) => setDraft((d) => ({ ...d, ...patch }))}
          onSubmit={submit}
          onCancel={resetForm}
        />
      )}

      {list.isPending ? (
        <CardGridSkeleton count={3} />
      ) : list.error ? (
        <p className="model-form-error" role="alert">
          无法连接后端：{errorMsg(list.error)}
        </p>
      ) : !models.length && !showForm ? (
        <EmptyState onCreate={openCreate} />
      ) : (
        <ModelCardGrid
          configs={models}
          deletingId={deletingId}
          defaultingId={defaultingId}
          onEdit={startEdit}
          onDelete={(id) => deleteMutation.mutate(id)}
          onSetDefault={(id) => setDefaultMutation.mutate(id)}
        />
      )}
    </div>
  );
}
