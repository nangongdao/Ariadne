# 05 评测引擎与指标体系

## 1. 三类评估器

评估器按"可信度递减、覆盖面递增"排列。**设计原则：能用确定性评估器解决的，绝不用 LLM Judge。**

| 类型 | 实现 | 可信度 | 成本 | 延迟 |
|---|---|---|---|---|
| **Deterministic** 确定性 | 正则、JSON Schema、精确匹配、编译、测试、数值范围 | ★★★★★ | 极低 | ms |
| **Statistical** 统计型 | ROUGE、BLEU、编辑距离、嵌入余弦相似度、困惑度 | ★★★ | 低 | 10-100ms |
| **LLM-as-Judge** | 模型打分（事实性、指令遵循、有用性、安全性） | ★★ | 高 | s 级 |

### 1.1 Deterministic

```python
@register_evaluator("command")
class CommandEvaluator(BaseEvaluator):
    """在沙箱中执行命令，以退出码作为评估结果。"""

    def evaluate(self, ctx: EvalContext) -> EvalResult:
        result = self._sandbox.run(
            cmd=self._cfg.cmd,
            timeout_s=self._cfg.timeout_s,
            workdir=ctx.artifact_path,
        )
        return EvalResult(
            name=self._cfg.name,
            value=1.0 if result.exit_code == 0 else 0.0,
            passed=result.exit_code == 0,
            evidence=truncate(result.stderr or result.stdout, head=20, tail=5),
        )
```

覆盖场景：pytest/jest 通过率、tsc/mypy 无错误、ruff/eslint 无 error、JSON 可解析且符合 schema、必带字段存在、字数区间、禁用词不出现。

### 1.2 Statistical

用于有参考答案（golden answer）的回归测试。核心指标：

| 指标 | 用途 | 注意 |
|---|---|---|
| 嵌入余弦相似度 | 语义相似度，最常用 | 需固定嵌入模型版本，换模型会导致历史分数不可比 |
| ROUGE-L | 摘要任务重合度 | 对语序敏感，中文需先分词 |
| 编辑距离 | 结构化输出的微小偏差 | 归一化到 0-1 |
| 检索命中率 / MRR | RAG 检索质量 | 需要标注的相关文档集 |

### 1.3 LLM-as-Judge

覆盖无法用规则表达的维度：事实性（Factuality）、指令遵循率（IFR）、有用性、安全性、语气一致性。这类评估器**可信度最低但覆盖面最广**，因此需要专门的可信度工程（见第 2 节）。

## 2. Judge 可信度工程

LLM-as-Judge 的分数在 Ariadne 里会被用作**收敛断言的依据**，因此不能是"随手调一次模型打个分"。六条强制措施：

### 2.1 版本锁定与确定性

```python
JUDGE_CONFIG = ModelConfig(
    model="claude-sonnet-5",      # 精确版本，禁止用 latest 别名
    temperature=0.0,
    seed=42,                       # provider 支持时传入
    response_format="json_schema", # 强制结构化输出
)
```

Judge 模型版本变更必须视为**破坏性变更**：历史分数不可与新分数直接比较，需要重跑基线。前端在趋势图上用竖线标注 Judge 版本切换点。

### 2.2 结构化输出而非自由文本打分

```json
{
  "score": 87,
  "reasoning": "引用完整，但第 3 段存在无来源断言",
  "violations": [
    {"dimension": "factuality", "span": "第3段第2句", "severity": "medium"}
  ]
}
```

`violations` 中的 `span` 定位是关键 —— 它直接喂给 Critique Synthesizer 生成定向修正指令。只给一个总分的 Judge 对 Loop 毫无帮助。

### 2.3 消除位置偏差

成对比较（A/B 评测）时，模型系统性偏好靠前的选项。做法：**双向投票** —— 同时评 (A,B) 和 (B,A)，只有两次结论一致才采信；不一致标记为 `tie`。

### 2.4 与人工标注对齐

每个 Judge 上线前必须在人工标注集上算 **Cohen's κ**：

| κ 区间 | 结论 |
|---|---|
| ≥ 0.8 | 高度一致，可直接作为断言依据 |
| 0.6 – 0.8 | 可用，但建议只做非 blocking 断言 |
| 0.4 – 0.6 | 仅作参考指标，禁止用于收敛判定 |
| < 0.4 | 不可用，需重写 Judge prompt 或换维度定义 |

标注集要求：每个维度 ≥ 100 条样本，覆盖好/中/差三档，由 ≥ 2 名标注者独立标注。

### 2.5 元评测（评估评估器）

Judge 自身要被评测。做法是维护一个**已知答案的对抗集**：

- 故意插入事实错误的样本 → Judge 应给低事实性分。
- 完全遵循指令的样本 → Judge 应给满分 IFR。
- 表面华丽但空洞的样本 → 检验 Judge 是否被文采迷惑。

元评测在 CI 中定期运行，Judge 在对抗集上的准确率下降即告警。

### 2.6 生成模型与 Judge 模型强制隔离

配置层面校验：`judge.model != generation.model`（同族不同尺寸也算违规，因为共享偏好）。自评会系统性高估，这是 Ralph 原则在评测层的延伸。

## 3. 数据集与实验管理

### 3.1 数据集版本化

```python
@dataclass(frozen=True)
class Dataset:
    id: str
    name: str
    version: int              # 每次变更自增，不可覆盖
    content_hash: str         # SHA256(排序后的全部样本)，用于验证复现
    item_count: int
    created_at: datetime
```

样本结构：`input`（输入）、`expected`（可选参考答案）、`metadata`（分片标签，如难度、领域）。

`content_hash` 是复现的锚点：实验记录里存的是 `dataset_id + version + content_hash`，任何人拿到这三个值都能确认自己的数据集与原实验完全一致。

### 3.2 批量实验（Experiment Run）

```
Experiment = Dataset(版本) × Config(prompt/model/参数) × Evaluators
```

产出：逐样本结果 + 聚合指标 + 成本汇总。支持：

- **A/B 对比**：两个 Experiment 并排，展示每个指标的均值差、分布差、显著性（配对 t 检验或 bootstrap 置信区间）。
- **样本级 diff**：定位"哪些样本变好了、哪些变差了"。这比看均值有用得多 —— 均值持平可能掩盖了"一半变好一半变差"。
- **失败聚类**：把失败样本按失败签名聚类，找出系统性问题而非逐条看。

### 3.3 CI 回归门禁

```yaml
# .github/workflows/eval.yml 中的门禁配置
regression_gate:
  dataset: core-regression@v7
  baseline: main
  fail_if:
    - metric: composite_quality
      degradation_pct: 3       # 均值下降超 3% 阻断合并
    - metric: assertion_pass_rate
      degradation_pct: 0       # 断言通过率不允许任何下降
    - metric: cost_per_item
      increase_pct: 20         # 成本上涨超 20% 需人工确认
```

门禁在 PR 上运行，结果作为 check 回写。**成本也是门禁维度** —— 质量提升 1% 但成本翻倍通常不是好交易。

## 4. 成本归因

### 4.1 计价模型

```python
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0      # 缓存命中，折扣价
    cache_write_tokens: int = 0     # 缓存写入，可能溢价
    reasoning_tokens: int = 0       # 推理模型的思考 Token，单独计量
```

计价表按 `(provider, model, version)` 三元组维护，**带生效时间区间**：provider 调价后，历史记录仍用当时价格计算，保证历史成本报表不会因调价而改变。

缓存 Token 必须单独计量。多轮 Loop 场景下缓存命中率很高，若按标准价计算会大幅高估成本，导致预算熔断误触发。

### 4.2 归因维度

成本沿调用树自底向上汇总，支持按以下维度切分：

`project` / `loop_id` / `iteration` / `node`（DAG 节点）/ `model` / `user` / `tag`

关键报表：**单次达标成本**（`CONVERGED` 的 Loop 的总成本）与**单次直出成本**的比值。这个比值超过 2.5× 就要重新审视 Loop 是否经济。

## 5. 七项 SLI

| # | SLI | 定义 / 公式 | 目标 SLO |
|---|---|---|---|
| 1 | **任务成功率** | `CONVERGED / 全部终态` | ≥ 95% |
| 2 | **首轮达标率** | `iteration==1 就 CONVERGED / 全部 Loop` | ≥ 40%（用于衡量基线 prompt 质量） |
| 3 | **平均达标轮次** | `mean(CONVERGED 的 iteration)` | ≤ 3 |
| 4 | **复合质量分** | `Σ(维度分 × 权重)`，权重按项目配置 | ≥ 85 |
| 5 | **端到端时延 P95** | Loop 创建到终态的墙钟时间 | ≤ 60s（quality 模式） |
| 6 | **单次达标成本** | `总成本 / CONVERGED 数量` | ≤ 预算的 60% |
| 7 | **失败率与错误预算** | `(FAILED + BUDGET_EXCEEDED + STALLED) / 全部` | ≤ 5% |

补充观测指标（不设 SLO 但必须可查）：假完成率、Harness 拦截率、Judge κ 漂移、provider 错误率（按错误码细分 429/5xx/超时）、缓存命中率。

### 5.1 错误预算与告警

以 SLI #7 为例，月度错误预算 = 5% × 月度 Loop 总数。告警分级：

| 消耗速率 | 含义 | 动作 |
|---|---|---|
| 1 小时内消耗 > 2% 预算 | 快速燃烧 | P1 立即告警 |
| 6 小时内消耗 > 5% | 中速燃烧 | P2 告警 |
| 3 天内消耗 > 10% | 慢速燃烧 | P3 工单 |

用多窗口燃烧率（multi-window burn rate）而非固定阈值，避免"偶发抖动触发告警"和"缓慢恶化不告警"两类误判。

告警必须携带**归因信息**：哪个 project、哪个 model、哪类断言失败最多。只报"失败率高了"的告警没有可操作性。

### 5.2 SLI 计算实现

SLI 由 ClickHouse 物化视图预聚合（按 1 分钟粒度），查询时二次聚合到所需窗口。原因：SLI 面板是最高频查询，实时扫原始 span 表在亿级数据量下无法满足 500ms 的 p95 目标。

```sql
-- 示例：Loop 终态分布物化视图
CREATE MATERIALIZED VIEW mv_loop_outcomes
ENGINE = SummingMergeTree()
ORDER BY (project_id, minute, final_state)
AS SELECT
    project_id,
    toStartOfMinute(finished_at) AS minute,
    final_state,
    count()                       AS cnt,
    sum(total_cost_usd)           AS cost,
    sum(iteration_count)          AS iterations
FROM loop_outcomes
GROUP BY project_id, minute, final_state;
```

