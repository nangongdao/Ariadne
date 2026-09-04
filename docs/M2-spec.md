# M2 实施规格：评测引擎

> 前置：[M1 可观测骨架](M1-spec.md) 已完成。总体设计见 [05 评测引擎](05-evaluation.md)。

## 1. 交付定义

能回答"这次输出好不好"，且答案可信、可复现、可用于阻断合并。

M2 完成的判定标准：Judge 在人工标注集上 Cohen's κ ≥ 0.6；CI 回归门禁能在 PR 上阻断质量下降。

**为什么 M2 排在 M3（Loop）之前**：Loop 的收敛判定依赖评测断言。先做 Loop 会得到一个"用不可靠信号驱动迭代"的系统，比不做更糟。

## 2. 范围边界

### 做

| 项 | 内容 |
|---|---|
| 三类评估器 | Deterministic / Statistical / LLM-as-Judge，registry + factory 可插拔 |
| Judge 可信度工程 | 版本锁定、结构化输出、双向投票、κ 对齐、元评测、生成/评判模型强制隔离 |
| 数据集版本化 | 不可变版本 + `content_hash` 复现锚点 |
| 批量实验 | Dataset × Config × Evaluators，逐样本结果 + 聚合指标 |
| A/B 对比 | 均值差、分布差、bootstrap 置信区间、样本级 diff、失败聚类 |
| 7 项 SLI | 定义 + 物化视图 + 查询 API |
| CI 回归门禁 | `ariadne eval compare` 退出码阻断合并 |
| **PostgreSQL 引入** | 事务型数据（dataset、experiment、prompt 版本）落 Postgres |
| Prompt 版本管理 | 最小集：可复现实验所需的版本 + label |

### 不做

Loop 相关（M3）、Harness 规则引擎（M4）、在线评测的实时采样触发（M3 起随 Loop 一起做）、多模型 Judge 投票（先单模型，κ 不达标再上，见 D5）、评测结果的告警推送（M6 随 SLO 一起做）。

## 3. 引入 PostgreSQL 的理由

M1 只有 ClickHouse + Redis。M2 必须引入 Postgres，因为评测数据的访问模式与 span 完全不同：

| | span（ClickHouse） | dataset / experiment（Postgres） |
|---|---|---|
| 写入模式 | append-only 高吞吐 | 低频但需事务 |
| 更新 | 几乎不 | 频繁（实验状态流转） |
| 查询 | 按时间聚合 | 关系连接 + 外键约束 |
| 一致性 | 最终一致可接受 | 必须强一致（版本号不能重复） |

在 ClickHouse 里做实验状态流转是灾难（无事务、UPDATE 昂贵）。反之在 Postgres 里存 eval 明细也是灾难（量大且只做聚合查询）。

**分工**：dataset/experiment/prompt 的元数据与状态进 Postgres；逐样本的 eval 明细进 ClickHouse（`eval_results` 表，M1 DDL 已预留）。

## 4. 核心契约

```python
class EvalContext(BaseModel):
    """评估器的输入。"""
    model_config = ConfigDict(frozen=True)

    item_id: str
    input: str                      # 原始输入
    output: str                     # 待评测的输出
    expected: str | None = None     # 参考答案（有则可算相似度）
    metadata: dict[str, str] = {}   # 分片标签（难度、领域）
    artifact_path: Path | None = None   # COMMAND 类评估器的工作目录


class EvalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    value: float                    # 归一化到 0-100 或 0-1，由评估器声明
    passed: bool                    # 是否满足阈值
    evidence: str = ""              # 截断后的证据（失败原因定位用）
    # Judge 输出的违规定位，直接喂给 M3 的 Critique Synthesizer
    violations: tuple[Violation, ...] = ()
    judge_model: str = ""           # 非 Judge 类留空
    duration_ms: int = 0
    cost_usd: Decimal = Decimal("0")


class Violation(BaseModel):
    """Judge 定位到的具体问题。span 字段是关键 ——
    只给总分的 Judge 对 Loop 毫无帮助。"""
    model_config = ConfigDict(frozen=True)

    dimension: str      # factuality / ifr / safety / ...
    span: str           # 定位描述，如"第3段第2句"
    severity: Literal["low", "medium", "high"]
    detail: str = ""
```

`BaseEvaluator` 抽象与 M1 的 `BaseAdapter` 同构：

```python
class BaseEvaluator(ABC):
    """所有评估器的基类。

    契约：evaluate() 不得抛异常 —— 单个评估器失败不能让整个实验中断，
    失败时返回 passed=False 且 evidence 说明原因。
    """

    kind: ClassVar[EvaluatorKind]

    @abstractmethod
    def evaluate(self, ctx: EvalContext) -> EvalResult: ...
```

## 5. 关键实现决策

### 5.1 Judge 的确定性

```python
JUDGE_DEFAULTS = ModelConfig(
    temperature=0.0,
    seed=42,                        # provider 支持时传
    response_format="json_schema",  # 强制结构化，不解析自由文本
)
```

Judge 模型版本变更视为**破坏性变更**：写入 `judge_model` 字段并在趋势图上画竖线标注切换点。历史分数不与新分数直接比较。

配置加载时强制校验 `judge.model != generation.model`（同族不同尺寸也算违规，因为共享偏好）。这是 Ralph 原则在评测层的延伸。

### 5.2 双向投票消除位置偏差

成对比较时模型系统性偏好靠前选项。做法：同时评 `(A,B)` 与 `(B,A)`，两次结论一致才采信，不一致标 `tie`。这让 A/B 评测的调用成本翻倍，因此**只在成对比较模式下启用**，单样本打分不需要。

### 5.3 κ 计算与门禁

```python
def cohens_kappa(human: Sequence[int], judge: Sequence[int]) -> float:
    """加权 kappa（分数是有序等级，不是名义分类）。"""
```

κ 门禁在**配置加载时**生效：κ < 0.6 的 Judge 若被声明为 `blocking=True` 的断言依据，直接拒绝启动并说明原因。不允许"先跑起来再说"。

### 5.4 成本必须是门禁维度

质量提升 1% 但成本翻倍通常不是好交易。`regression_gate` 同时检查质量退化与成本上涨。

## 6. 模块清单

```
src/ariadne/
├── eval_module/
│   ├── __init__.py               # EvaluatorFactory + register_evaluator
│   ├── base.py                   # BaseEvaluator / EvalContext / EvalResult
│   ├── deterministic/
│   │   ├── regex.py              # 正则必含/必不含
│   │   ├── schema.py             # JSON Schema / Pydantic 校验
│   │   ├── command.py            # 沙箱执行命令取退出码（M4 前用受限子进程）
│   │   └── numeric.py            # 字数区间、数值范围
│   ├── statistical/
│   │   ├── similarity.py         # 嵌入余弦相似度
│   │   ├── overlap.py            # ROUGE-L / 编辑距离
│   │   └── retrieval.py          # 命中率 / MRR
│   ├── judge/
│   │   ├── runner.py             # Judge 调用 + 结构化输出解析
│   │   ├── prompts.py            # 各维度的 Judge prompt 模板
│   │   ├── pairwise.py           # 双向投票
│   │   ├── kappa.py              # 与人工标注对齐
│   │   └── meta.py               # 元评测（对抗集）
│   ├── composite.py              # 加权复合分
│   └── sli.py                    # 7 项 SLI 计算
├── experiment/
│   ├── runner.py                 # Dataset × Config 批量执行
│   ├── compare.py                # A/B 对比 + bootstrap 置信区间
│   └── cluster.py                # 失败聚类
├── storage/
│   └── postgres/
│       ├── engine.py             # async SQLAlchemy 引擎与会话
│       ├── models.py             # ORM 模型
│       └── repositories/         # dataset / experiment / prompt
└── api/routers/
    ├── evaluations.py            # POST /v1/evaluations
    ├── experiments.py            # POST /v1/experiments, GET .../compare
    ├── datasets.py
    └── prompts.py
```

前端新增：实验列表页、实验详情（聚合指标 + 逐样本表）、A/B 对比页（并排指标 + 样本级 diff）、数据集管理页。

## 7. 技术栈增量

| 选择 | 用途 | 理由 | 被否方案 |
|---|---|---|---|
| **PostgreSQL 16** | 事务型数据 | 见第 3 节 | 复用 ClickHouse：无事务，实验状态流转会失控 |
| **SQLAlchemy 2.x async + Alembic** | ORM 与迁移 | 迁移脚本入库版本控制，禁止手改线上 schema | 裸 SQL：关系查询与迁移管理会失控 |
| **`asyncpg`** | Postgres 驱动 | async 原生，性能优于 psycopg 的 async 模式 | psycopg3 |
| **`sentence-transformers`（可选）** | 本地嵌入 | 相似度评测不必每次调 API，本地模型省成本 | 只用 provider API：批量实验成本高 |
| **`rapidfuzz`** | 编辑距离 | C 实现，比纯 Python 快一个数量级 | `difflib`：批量场景太慢 |
| **`scipy.stats`** | 显著性检验 | bootstrap 置信区间、配对 t 检验 | 手写：数值稳定性风险 |
| **`jsonschema`** | Schema 校验 | 独立于 Pydantic，用户可直接提供 JSON Schema | 只用 Pydantic：用户得写 Python |

嵌入模型必须**固定版本**并记入实验元数据 —— 换模型会导致历史相似度分数不可比。

## 8. 数据库 Schema

Postgres 表（完整 DDL 见 [08 数据模型](08-data-model.md#3-postgresql)）：

`organizations`、`projects`、`api_keys`、`users`、`memberships`、`datasets`、`dataset_items`、`experiments`、`experiment_items`、`prompt_versions`、`model_pricing`、`judge_calibrations`（κ 记录）。

M1 的静态单 project 模型在此替换为真实表，但**RBAC 仍不做**（M6）—— `api_keys` 表先只用 `scopes` 字段，不接角色系统。

ClickHouse 新增：`eval_results`（M1 DDL 已建）+ `mv_eval_score_dist` 物化视图。

## 9. 验收清单

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | 三类评估器均可通过 registry 取用 | 单元测试遍历 registry |
| 2 | 评估器失败不中断实验 | 注入抛异常的评估器，实验仍完成 |
| 3 | Judge κ ≥ 0.6 | 在 100 条人工标注集上计算 |
| 4 | κ < 0.6 的 Judge 不能作 blocking 断言 | 配置校验拒绝启动 |
| 5 | 生成模型 == Judge 模型时拒绝启动 | 配置校验 |
| 6 | 双向投票消除位置偏差 | 构造已知偏好样本，验证 tie 判定 |
| 7 | 数据集 `content_hash` 可复现 | 同样本集不同顺序 → 同 hash |
| 8 | A/B 对比给出置信区间 | 已知分布的合成数据验证 |
| 9 | CI 门禁能阻断 | 故意退化的分支，`eval compare` 退出码非 0 |
| 10 | 成本上涨触发门禁 | 质量持平但成本 +30% → 阻断 |
| 11 | Postgres 迁移幂等 | Alembic 重复执行不报错 |

## 10. 工期与顺序

原计划 4 周：

1. **第 1 周**：Postgres 引入 + Alembic + dataset/prompt 的 CRUD
2. **第 2 周**：Deterministic + Statistical 评估器 + composite
3. **第 3 周**：Judge 全套可信度工程（这周最重，κ 对齐要人工标注数据）
4. **第 4 周**：实验编排 + A/B 对比 + SLI + CI 门禁 + 前端页面

**实际调整**：因本机 Docker daemon 未启动，Postgres 相关工作无法验证，故把第 1 周与第 2-4 周对调 —— 先做无需容器即可完整测试的纯函数层。这个顺序同样合理：评估器契约是 Postgres schema 的输入（表结构要存什么由评估器产出决定），先定契约再建表比反过来好。

第 3 周需要**人工标注集**作为外部依赖：每维度 ≥ 100 条、覆盖好/中/差三档、≥ 2 名标注者。这是唯一无法靠代码解决的阻塞项，应提前准备。

## 11. 实施进度

### 已完成（纯函数层，无需容器）

| 模块 | 内容 | 测试 |
|---|---|---|
| `eval_module/base.py` | EvalContext / EvalResult / Violation / BaseEvaluator 异常兜底 | — |
| `eval_module/deterministic/` | regex、citation_count、markdown_structure、json_schema、required_fields、word_count、numeric_range、forbidden_terms、exact_match | 36 |
| `eval_module/statistical/` | rouge_l、token_f1、edit_distance（含滚动数组 LCS） | 24 |
| `eval_module/composite.py` | 加权复合分 + 量纲归一化 + errored 排除 | 22 |
| `eval_module/judge/` | 六条可信度措施全部落地 | 42 |
| `eval_module/judge/meta.py` | 元评测 + 8 条内置对抗集 | 11 |
| `experiment/stats.py` | bootstrap CI、样本级 diff、churn、方差警告 | 39 |
| `experiment/dataset.py` | 版本化 + content_hash（顺序无关）+ JSONL | 23 |
| `experiment/runner.py` | 批量执行 + 失败隔离 + 失败签名聚类 | （同上） |
| `experiment/compare.py` | 对比报告 + 数据集一致性校验 | 16 |
| `experiment/gate.py` | 回归门禁 + 三档退出码 | （含 stats） |
| `experiment/persist.py` + `eval_cli.py` | JSON 持久化 + `ariadne-eval compare/show` | 19 |

累计 **317 项测试通过**，ruff + mypy strict 全绿。

开发中被测试抓到并修掉的三个真 bug：

1. **`flipped_to_fail` 恒为 0**。翻转检测基于复合分（0-100），而 100→10 两边都 > 0。修正为用通过率（0/1）算翻转、用复合分算幅度排序 —— 两者回答不同问题。
2. **基线为 0 时满幅变化被判"无变化"**。`delta_pct` 在基线为 0 时返回 0.0，被 `min_effect_pct` 过滤掉。通过率从 0% 涨到 100% 是最大可能的改善，绝不是噪声。
3. **registry 漏注册**（M1 同类问题的复现）。`_ensure_loaded` 若用"字典非空"判断已加载，任何直接 import 子模块的代码都会让其余实现永远注册不上。已统一改用独立布尔标志。

### 已完成（Postgres 层）

| 模块 | 内容 | 测试 |
|---|---|---|
| `storage/postgres/models.py` | 9 张表的 ORM 模型（含跨方言 `UuidType`） | — |
| `storage/postgres/engine.py` | async 引擎 + 会话（自动提交/回滚） | — |
| `repositories/datasets.py` | 版本不可变 + hash 校验 + 租户隔离 | 12 |
| `repositories/experiments.py` | 状态机转移 + 基线查找 + 成本汇总 | 8 |
| `repositories/prompts.py` | 版本 + label 唯一性 + 渲染校验 | 6 |
| `deploy/alembic/` | 初始迁移，已验证幂等 / 可回滚 / 可反复迁移 | — |

**测试策略**：用 aiosqlite 跑**真实 SQL** 而非 mock 数据库 —— mock 掉的 SQL 语法错误、约束冲突、事务行为只有真引擎能发现。Postgres 特有行为（JSONB 操作符、RLS）留给 `-m integration` 的真容器测试。

这一轮又抓到三个真问题：

4. **`with_variant` 不改 Python 侧绑定**。`PG_UUID().with_variant(String(36), "sqlite")` 只改 DDL 类型，SQLite 驱动收到 `UUID` 对象直接报 `type 'UUID' is not supported`。必须写 `TypeDecorator` 做双向转换。
5. **`alembic.ini` 的中文注释在 GBK locale 下崩**。Alembic 用 `encoding="locale"` 读配置且无法覆盖，配置文件必须保持纯 ASCII，说明移到 `env.py`。
6. **缺 `py.typed`**（PEP 561）。下游消费者对 SDK 做类型检查时 mypy 会整包跳过，SDK 的类型标注对用户毫无价值。

### 已完成（API 与前端）

| 模块 | 内容 | 测试 |
|---|---|---|
| `api/routers/datasets.py` | 6 端点：创建 / 列表 / 版本 / 详情 / 导出 / 导入 | 14 |
| `api/routers/experiments.py` | 6 端点：创建 / 列表 / 详情 / 回传结果 / 快照 / 对比 | 12 |
| `web/pages/ExperimentListPage` | 实验列表 + 勾选对比（限同数据集） | — |
| `web/components/ComparePanel` | 门禁横幅 + 指标表 + churn 网格 + 翻转清单 | — |
| `web/pages/DatasetListPage` | 数据集列表 + 版本 + JSONL 导出 | — |

两个刻意的接口设计：

- **数据集没有 PUT/PATCH**。改内容只能 POST 新版本 —— 允许原地改会让历史实验的 `content_hash` 失效。有测试断言 OpenAPI 里不存在这两个方法。
- **实验由客户端执行、服务端只存结果**。理由：实验要调用用户自己的 provider 密钥，服务端代跑意味着要托管密钥（见 [10 安全](10-security.md#4-密钥管理) 的默认不存决策）。M5 引入编排能力时才会有服务端执行。

前端对比面板把 **churn 放在显眼位置**：均值持平却双向变化时给出明确警告，因为那通常意味着引入了新的失败模式，而只看均值的用户会漏掉。

### 待完成

| 项 | 阻塞原因 |
|---|---|
| 7 项 SLI 计算与物化视图 | 需 ClickHouse 容器 |
| 嵌入相似度评估器 | 需确定嵌入模型选型（换模型会让历史分数不可比） |
| `command` 类评估器 | 依赖 M3 的受限子进程或 M4 沙箱 |
| 逐样本明细写 ClickHouse | 现用 `config._result_snapshot` 过渡，需容器后迁移 |
| Prompt API 路由 | 仓储已就绪，路由待补 |
| Postgres 集成测试（真容器） | 需 Docker |

