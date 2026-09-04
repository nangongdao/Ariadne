import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { NavLink } from "react-router-dom";

import {
  api,
  ApiError,
  getApiBase,
  getApiKey,
  isTauri,
  setApiBase,
  setApiKey,
} from "@/api/client";

// 3s 两条查询并行轮询对一个设置页来说过密（每分钟 40 次请求）。
// 健康/管道状态都不是秒级信号，降到 10-15s 足够；
// react-query 的 refetchIntervalInBackground 默认 false，标签页不可见时自动停。
const HEALTH_REFRESH_MS = 10_000;
const STATS_REFRESH_MS = 15_000;

/**
 * key 保存后的校验状态。
 *
 * 只显示"已保存"是在骗人：/health 与 /v1/stats 都不带鉴权，key 填错照样 200，
 * 于是设置页一片绿而所有业务页 401。存完必须打一个真需要鉴权的接口。
 */
type KeyCheck =
  | { phase: "idle" }
  | { phase: "checking" }
  | { phase: "ok" }
  | { phase: "failed"; message: string };

export function SettingsPage() {
  const [keyInput, setKeyInput] = useState(getApiKey());
  const [baseInput, setBaseInput] = useState(getApiBase());
  const [keyCheck, setKeyCheck] = useState<KeyCheck>({ phase: "idle" });
  const [baseSaved, setBaseSaved] = useState(false);

  const health = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
    refetchInterval: HEALTH_REFRESH_MS,
  });

  const stats = useQuery({
    queryKey: ["pipeline-stats"],
    queryFn: api.stats,
    refetchInterval: STATS_REFRESH_MS,
  });

  return (
    <div className="page">
      <header className="page-header">
        <h1>设置</h1>
      </header>

      <section className="detail-section">
        <h3>模型配置</h3>
        <p className="hint">
          自定义 LLM 的名称、模型、API Key 与 Base URL（支持 OpenAI /
          Anthropic / Ollama 等兼容网关），并设为项目默认供 Loop Worker 使用。
        </p>
        <p className="hint">
          <NavLink to="/models" className="settings-link">
            前往模型配置页 →
          </NavLink>
        </p>
      </section>

      <section className="detail-section">
        <h3>API Key</h3>
        <p className="hint">
          M1 是单 project + 静态 key 模型，值来自服务端的
          <code> ARIADNE_API_STATIC_API_KEY</code>。
        </p>
        <form
          className="key-form"
          onSubmit={(event) => {
            event.preventDefault();
            const next = keyInput.trim();
            // 空值存进去只会让每个业务页 401，而这里照样显示"已保存"
            if (!next) {
              setKeyCheck({ phase: "failed", message: "API Key 不能为空" });
              return;
            }
            setApiKey(next);
            setKeyCheck({ phase: "checking" });
            // listGraphs 需要 Permission.READ，是真会因 key 不对而 401 的接口
            void api
              .listGraphs()
              .then(() => {
                setKeyCheck({ phase: "ok" });
                void health.refetch();
                void stats.refetch();
              })
              .catch((err: unknown) => {
                const status = err instanceof ApiError ? err.status : 0;
                setKeyCheck({
                  phase: "failed",
                  message:
                    status === 401
                      ? "这个 key 被服务端拒了（401），检查是否与 ARIADNE_API_STATIC_API_KEY 一致"
                      : `已存下，但校验请求没通过：${err instanceof Error ? err.message : String(err)}`,
                });
              });
          }}
        >
          <label htmlFor="settings-api-key">API Key</label>
          <input
            id="settings-api-key"
            type="password"
            value={keyInput}
            onChange={(event) => {
              setKeyInput(event.target.value);
              setKeyCheck({ phase: "idle" });
            }}
            placeholder="ak_local_dev_key"
            autoComplete="off"
            aria-invalid={keyCheck.phase === "failed"}
          />
          <button type="submit" disabled={keyCheck.phase === "checking"}>
            {keyCheck.phase === "checking" ? "校验中…" : "保存并校验"}
          </button>
          {keyCheck.phase === "ok" && <span className="badge-good">已保存并验证</span>}
        </form>
        {keyCheck.phase === "failed" && (
          <p className="form-error" role="alert">
            {keyCheck.message}
          </p>
        )}
      </section>

      {isTauri() && (
        <section className="detail-section">
          <h3>后端地址</h3>
          <p className="hint">
            桌面端独立于浏览器运行，无法走 vite 代理——在此填写 Ariadne 后端
            （FastAPI）的地址，默认为本机 8000 端口。
          </p>
          <form
            className="key-form"
            onSubmit={(event) => {
              event.preventDefault();
              setApiBase(baseInput.trim());
              setBaseSaved(true);
              void health.refetch();
              void stats.refetch();
            }}
          >
            <label htmlFor="settings-api-base">后端地址</label>
            <input
              id="settings-api-base"
              type="text"
              value={baseInput}
              onChange={(event) => {
                setBaseInput(event.target.value);
                setBaseSaved(false);
              }}
              placeholder="http://127.0.0.1:8000"
              spellCheck={false}
            />
            <button type="submit">保存</button>
            {baseSaved && <span className="badge-good">已保存</span>}
          </form>
        </section>
      )}

      <section className="detail-section">
        <h3>服务状态</h3>
        {health.isPending ? (
          <p className="hint">检查中…</p>
        ) : health.error ? (
          <p className="error-text">无法连接后端：{String(health.error)}</p>
        ) : (
          health.data && (
            <dl className="kv">
              <dt>整体</dt>
              <dd>
                <span
                  className={
                    health.data.status === "ok" ? "badge-good" : "badge-warn"
                  }
                >
                  {health.data.status === "ok" ? "正常" : "降级"}
                </span>
              </dd>
              <dt>版本</dt>
              <dd className="mono">{health.data.version}</dd>
              <dt>ClickHouse</dt>
              <dd>{health.data.clickhouse ? "已连接" : "不可用"}</dd>
              <dt>Postgres</dt>
              <dd>{health.data.postgres ? "已连接" : "不可用"}</dd>
              <dt>Redis</dt>
              <dd>{health.data.redis ? "已连接" : "不可用"}</dd>
            </dl>
          )
        )}
      </section>

      <section className="detail-section">
        <h3>采集管道</h3>
        {stats.isPending ? (
          <p className="hint">读取中…</p>
        ) : stats.error ? (
          /* 失败与「真的没数据」得分开：都显示「—」用户无从判断该改 key 还是等 worker */
          <p className="form-error" role="alert">
            读取管道状态失败：{String(stats.error)}
          </p>
        ) : stats.data ? (
          <dl className="kv">
            <dt>队列长度</dt>
            <dd>{stats.data.queue_length}</dd>
            <dt>待确认消息</dt>
            <dd title="已被 Worker 取走但未 ACK。持续偏高说明消费出了问题。">
              {stats.data.pending}
              {stats.data.pending > 100 && (
                <span className="badge-warn">积压</span>
              )}
            </dd>
          </dl>
        ) : (
          <p className="hint">—</p>
        )}
      </section>
    </div>
  );
}
