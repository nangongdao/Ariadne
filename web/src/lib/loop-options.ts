/**
 * Loop 表单与筛选器共用的选项表。
 *
 * 每项都带 desc：模式与断言类型的差别不是从名字能看出来的，
 * 选中后就地解释比让人去翻文档强。
 */

/**
 * 模式选项。value 必须与后端 LoopMode 字面量一致
 * （src/ariadne/loop_module/goal.py 的 LoopMode 与 modes 注册表）。
 */
export const MODE_OPTIONS: ReadonlyArray<{
  value: string;
  label: string;
  desc: string;
}> = [
  {
    value: "quality",
    label: "质量",
    desc: "通用迭代改进：每轮按断言打分并定向修正。默认选择。",
  },
  {
    value: "retry",
    label: "重试",
    desc: "只应对暂时性故障，几轮不成即说明非偶发问题，轮次建议 ≤ 5。",
  },
  {
    value: "verify_execute",
    label: "验证执行",
    desc:
      "用命令退出码作反馈信号 —— 代码生成场景的首选。" +
      "配合 command 断言与工作目录文件使用。",
  },
  {
    value: "hitl",
    label: "人工协同",
    desc: "关键节点转人工审批，必须包含至少一条 human 断言。",
  },
];

/**
 * 断言类型。信号强度递减：command / schema 二值无歧义，metric 有噪声。
 *
 * unavailable 有值时表示后端当前跑不了这类断言，选了必然失败。文案说明原因，
 * 表单据此禁用该项 —— 而不是让人填完命令再吃一个 422。
 */
export const ASSERTION_KINDS: ReadonlyArray<{
  value: string;
  label: string;
  desc: string;
  unavailable?: string;
}> = [
  { value: "regex", label: "正则匹配", desc: "输出必须匹配给定正则。" },
  {
    value: "command",
    label: "命令退出码",
    desc:
      "跑一条命令，退出码 0 即通过。信号最强（二值无歧义），" +
      "需在下方「工作目录文件」里提供被验证的文件。",
  },
  { value: "schema", label: "JSON Schema", desc: "输出须为符合该 Schema 的 JSON。" },
  { value: "metric", label: "指标阈值", desc: "评估指标与阈值比较，依赖评估器配置。" },
  { value: "human", label: "人工审批", desc: "转人工判定，无需填写规格。" },
];

export const METRIC_OPS: ReadonlyArray<string> = [">=", ">", "<=", "<", "==", "!="];

/** 运行中（非终态）的 Loop 状态。到终态后列表停止轮询。 */
export const ACTIVE_LOOP_STATES: ReadonlySet<string> = new Set([
  "CREATED",
  "VALIDATE",
  "PLANNING",
  "PRECHECK",
  "EXECUTING",
  "EVALUATING",
  "JUDGING",
  "REVISING",
  "HUMAN_PENDING",
]);
