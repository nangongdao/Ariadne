/**
 * 断言规格字段 —— 只渲染当前断言类型需要的输入。
 *
 * 状态留在父组件而不是这里：用户在 regex 和 schema 之间来回切时，
 * 已经填过的内容必须还在。状态下沉到本组件会随卸载一起丢掉。
 */

import { METRIC_OPS } from "@/lib/loop-options";

export interface AssertionSpecFieldsProps {
  kind: string;
  pattern: string;
  onPattern: (v: string) => void;
  cmd: string;
  onCmd: (v: string) => void;
  schemaText: string;
  onSchemaText: (v: string) => void;
  metricName: string;
  onMetricName: (v: string) => void;
  metricOp: string;
  onMetricOp: (v: string) => void;
  metricValue: number;
  onMetricValue: (v: number) => void;
}

export function AssertionSpecFields(props: AssertionSpecFieldsProps) {
  const { kind } = props;

  if (kind === "regex") {
    return (
      <label className="field" htmlFor="loop-pattern">
        <span>达标正则（输出必须匹配）</span>
        <input
          id="loop-pattern"
          value={props.pattern}
          onChange={(e) => props.onPattern(e.target.value)}
          placeholder="如：def\s+\w+\s*\("
          className="mono"
        />
      </label>
    );
  }

  if (kind === "command") {
    return (
      <label className="field" htmlFor="loop-cmd">
        <span>命令（退出码 0 即通过）</span>
        <input
          id="loop-cmd"
          value={props.cmd}
          onChange={(e) => props.onCmd(e.target.value)}
          placeholder="如：pytest -q"
          className="mono"
        />
      </label>
    );
  }

  if (kind === "schema") {
    return (
      <label className="field" htmlFor="loop-schema">
        <span>JSON Schema</span>
        <input
          id="loop-schema"
          value={props.schemaText}
          onChange={(e) => props.onSchemaText(e.target.value)}
          placeholder='如：{"type":"object","required":["name"]}'
          className="mono"
        />
      </label>
    );
  }

  if (kind === "metric") {
    return (
      <>
        <label className="field" htmlFor="loop-metric-name">
          <span>指标名称</span>
          <input
            id="loop-metric-name"
            value={props.metricName}
            onChange={(e) => props.onMetricName(e.target.value)}
            placeholder="如：faithfulness"
            className="mono"
          />
        </label>
        <div className="field-row">
          <label className="field" htmlFor="loop-metric-op">
            <span>比较符</span>
            <select
              id="loop-metric-op"
              value={props.metricOp}
              onChange={(e) => props.onMetricOp(e.target.value)}
            >
              {METRIC_OPS.map((op) => (
                <option key={op} value={op}>
                  {op}
                </option>
              ))}
            </select>
          </label>
          <label className="field" htmlFor="loop-metric-value">
            <span>阈值</span>
            <input
              id="loop-metric-value"
              type="number"
              step={0.01}
              value={props.metricValue}
              onChange={(e) => props.onMetricValue(Number(e.target.value))}
            />
          </label>
        </div>
      </>
    );
  }

  if (kind === "human") {
    return (
      <p className="hint">人工审批断言无需规格，Loop 会在该断言处转入待人工审批。</p>
    );
  }

  return null;
}
