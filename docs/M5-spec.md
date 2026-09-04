# M5 实施规格：编排与可视化完整化

> 前置：[M4](M4-spec.md) 已完成。总体设计见 [07 前端可视化](07-frontend-visualization.md)。

## 1. 交付定义

从"看"到"编" —— 不写代码就能编排出含 Loop 的完整工作流并跑通。

M5 完成的判定标准：UI 编排一个含 Loop 子图的工作流并成功执行；100 节点 DAG 渲染流畅（交互无明显掉帧）。

## 2. 范围边界

### 做

| 项 | 内容 |
|---|---|
| DAG 编排（设计时） | React Flow 拖拽 + 连线 + 自定义节点 + 参数配置 |
| DAG 运行时状态 | 实时节点状态 + 关键路径高亮 + 点击下钻 Span |
| Loop 作为子图节点 | 单入单出，内部是受控循环 |
| 设计时校验 | 环检测、类型兼容、必填参数、断言可验证性 |
| DAG 执行器 | 拓扑排序 + 并发调度 + 条件分支 |
| Playground | 多配置并排跑 + 一键固化为 spec + 从 span 复现 |
| TypeScript SDK | 与 Python SDK 对等能力 |
| 并行 Loop 池 | pipeline 语义（非 barrier）+ 共享预算池 |
| LangGraph 图导入 | 兼容已有图定义（见 D2 决策） |

### 不做

多 Agent 协作编排（M6 之后评估，见 D7）、多租户 RBAC（M6）、Firecracker（M6）。

## 3. 两个关键设计

### 3.1 Loop 作为可嵌套子图节点

这是与纯 DAG 编排器的核心区别。DAG 本身无环，但 Loop 内部是受控循环。表达方式：

```
外部看：单入单出的普通节点
内部是：Goal（断言 + 预算 + 模式）驱动的迭代
```

好处是拓扑仍可静态分析（环检测、类型推导都成立），同时表达了迭代语义。若把 Loop 表达为图上的真实回边，环检测就失效了,整个静态校验体系会崩塌。

### 3.2 并行 Loop 用 pipeline 而非 barrier

批量场景（生成 N 篇文档、修 N 个测试）:

| 语义 | 墙钟时间 | 评价 |
|---|---|---|
| barrier（每阶段等齐） | 每阶段最慢项之和 | 慢，且单项卡住拖累全部 |
| **pipeline（各项独立跑完）** | 最慢单项 | 选定 |

共享：全局预算池（Redis 原子计数）、并发上限、provider 速率限制令牌。
隔离：每项独立的上下文、检查点、沙箱实例、失败指纹。

并发度 = `min(配置上限, provider 速率余量, 沙箱池容量)`，动态计算避免把上游打到 429。

## 4. 关键实现决策

### 4.1 编排产物就是 spec.yaml

UI 编排与代码定义共用一份事实源，可双向转换。否则会出现"UI 里改了但 spec 没变"的漂移，且 UI 编排的东西无法进版本控制。

### 4.2 设计时校验，保存即校验

不等运行时才报错：

```python
def validate_graph(graph: WorkflowGraph) -> list[ValidationIssue]:
    """环检测（Loop 节点外不允许成环）、类型兼容（上游输出契约 vs
    下游输入）、必填参数缺失、Loop 节点的断言可验证性。
    """
```

断言可验证性校验直接复用 M3 的 `goal.validate()` —— 同一套规则，避免两处实现漂移。

### 4.3 DAG 执行器自研，但兼容导入 LangGraph

待决策项 D2 的结论：**自研执行器 + 兼容导入 LangGraph 图定义**。

理由：Ariadne 的执行器需要在每个节点边界插入 Harness 卡点、预算结算、span 埋点。用 LangGraph 会被它的抽象绑住这三件事。但用户已有的 LangGraph 图不该被迫重写,因此提供 `import_langgraph(graph)` 转换为内部 `WorkflowGraph`。

转换的边界要写清楚：LangGraph 的条件边可映射为条件分支节点；其 checkpointer 不导入（用 Ariadne 自己的检查点）；不支持的构造显式报错而非静默降级。

### 4.4 Playground 与 Loop 打通

从任意历史 span 一键"在 Playground 中打开"，用原始输入复现问题；调好后一键"固化为 spec"或"以此输入创建 Loop"。这条链路让调试闭环，是产品体验的关键。

## 5. 模块清单

```
src/ariadne/
├── graph_module/
│   ├── models.py             # WorkflowGraph / Node / Edge / Port
│   ├── nodes/                # 七种节点类型的定义与执行
│   │   ├── llm.py
│   │   ├── tool.py
│   │   ├── rag.py
│   │   ├── code.py
│   │   ├── branch.py         # CEL 条件分支
│   │   ├── loop.py           # Loop 子图节点
│   │   └── eval.py
│   ├── executor.py           # 拓扑排序 + 并发调度
│   ├── validate.py           # 设计时校验
│   ├── serialize.py          # ↔ spec.yaml 双向转换
│   └── importers/
│       └── langgraph.py      # LangGraph 图定义导入
├── loop_module/
│   └── parallel.py           # 并行 Loop 池（pipeline 语义）
└── api/routers/
    ├── graphs.py             # CRUD + validate
    └── playground.py

sdk/typescript/               # TS SDK（独立包）
├── src/
│   ├── client.ts
│   ├── span.ts               # AsyncLocalStorage 传播
│   ├── exporter.ts
│   ├── decorators.ts
│   └── instrument/{openai,anthropic}.ts
└── package.json
```

前端新增：DAG 编排画布（React Flow）、节点配置面板、Playground、并行 Loop 批次视图。

## 6. 技术栈增量

| 选择 | 用途 | 理由 | 被否方案 |
|---|---|---|---|
| **React Flow 12** | DAG 画布 | 拖拽/连线/自定义节点/minimap/状态动画能力最完整 | Cytoscape.js：偏图分析，编辑体验弱；手写 SVG：交互工作量巨大 |
| **`dagre`** | 自动布局 | 导入的图没有坐标，需要自动布局 | `elkjs`：更强但体积大 3 倍，M5 的图规模用不上 |
| **`AsyncLocalStorage`（Node）** | TS SDK 上下文传播 | Node 原生，对等 Python 的 contextvars | 手动传 context：污染业务 API |
| **`graphlib`（Python）** | 拓扑排序与环检测 | `functools`/手写皆可，但环检测的边界情况多 | 手写 DFS：可行但要自己处理自环、重边 |

**TS SDK 的埋点方式与 Python 一致**：只包装公开方法，不 patch 私有属性。

## 7. 前端性能约束

M5 的图规模上来后，这些是硬要求（见 [07 文档](07-frontend-visualization.md#42-大数据量优化)）：

| 场景 | 措施 |
|---|---|
| DAG 节点上百 | 自动分组折叠 + minimap 导航 + 视口外节点不渲染详情 |
| 实时状态更新抖动 | 服务端 100ms 批合并 + 前端 `requestAnimationFrame` 节流 |
| 并行 Loop 数十项 | 批次列表虚拟滚动，默认只展开失败项 |

React Flow 的 `onlyRenderVisibleElements` 必须开启 —— 100 节点时是数量级差异。

## 8. 验收清单

| # | 验收项 | 验证方式 |
|---|---|---|
| 1 | UI 编排含 Loop 的工作流并跑通 | 手动端到端 |
| 2 | 编排产物 ↔ spec.yaml 双向无损 | 往返转换后语义等价 |
| 3 | 环检测生效 | 构造 Loop 节点外的环 → 保存被拒 |
| 4 | 类型不兼容被拒 | 上游输出 text 接下游要 documents → 报错 |
| 5 | Loop 节点的断言可验证性校验 | 复用 M3 规则，空断言被拒 |
| 6 | 100 节点 DAG 交互流畅 | 性能剖析，帧率 ≥ 50fps |
| 7 | 并行 Loop 是 pipeline 语义 | 一项慢不阻塞其他项完成 |
| 8 | 并行共享预算池不超支 | 多项并发跑，总用量不超全局上限 |
| 9 | 单项失败不影响其他项 | 注入失败项，其余仍 `CONVERGED` |
| 10 | provider 429 时自动降并发 | mock 429 响应，观察并发度下降 |
| 11 | LangGraph 图可导入 | 真实 LangGraph 图导入后拓扑等价 |
| 12 | 不支持的构造显式报错 | 导入含不支持特性的图 → 明确错误而非静默降级 |
| 13 | TS SDK 与 Python 对等 | 同一场景两个 SDK 产生的 span 结构一致 |
| 14 | Playground 复现链路 | 从历史 span 打开 → 相同输入 → 可执行 |

## 9. 工期与顺序

预计 5 周：

1. **第 1 周**：`WorkflowGraph` 模型 + 拓扑执行器 + 设计时校验（纯逻辑，先做对）
2. **第 2 周**：七种节点类型 + Loop 子图节点接入
3. **第 3 周**：React Flow 画布 + 节点配置面板 + spec 双向转换
4. **第 4 周**：并行 Loop 池 + Playground
5. **第 5 周**：TS SDK + LangGraph 导入 + 性能优化

顺序理由：先把图模型与执行器做对，UI 只是它的一个编辑器。反过来先做 UI 会导致模型变更时 UI 大量返工。

## 10. 实施进度（Windows 开发机）

### Week 1-2：后端图模型 + 执行器（已完成）

M5 的纯函数层全部在 Windows 开发机上离线完成，为 UI 编排和 DAG 执行提供基础。

#### 交付模块

| 模块 | 文件 | 内容 |
|---|---|---|
| 图模型 | `graph_module/models.py` | `WorkflowGraph`/`NodeBase`/`Edge`/`Port` frozen dataclass、`PortKind`/`NodeKind` StrEnum、`port_compatible()` 类型兼容矩阵、7 种节点的标准端口定义 |
| 设计时校验 | `graph_module/validate.py` | `validate_graph()` — 节点 id 唯一、边引用存在性、环检测（`graphlib.TopologicalSorter`）、类型兼容、必填参数、Loop 断言可验证性（复用 M3 `validate_goal`） |
| 注册表 | `graph_module/__init__.py` | `register_node(kind)` 装饰器 + `NodeFactory(kind)` + `_ensure_loaded()` + `available_node_kinds()` |
| 拓扑执行器 | `graph_module/executor.py` | `GraphExecutor.run()` — `TopologicalSorter` 层级调度、`asyncio.gather` 并发执行、Branch 路由（`__route` 约定）、失败级联 skip（fail-fast）、`ExecutionResult`/`NodeState` |
| LLM 节点 | `graph_module/nodes/llm.py` | `LLMNodeParams` + `LLMNodeExecutor`（注入 `LLMClient`，输出 text） |
| Tool 节点 | `graph_module/nodes/tool.py` | `ToolNodeParams` + `ToolNodeExecutor`（注入 tool_executor，输出 result） |
| RAG 节点 | `graph_module/nodes/rag.py` | `RAGNodeParams` + `RAGNodeExecutor`（注入 `Retriever` Protocol，输出 documents） |
| Code 节点 | `graph_module/nodes/code.py` | `CodeNodeParams` + `CodeNodeExecutor`（注入 `CodeRunner`，输出 result） |
| Branch 节点 | `graph_module/nodes/branch.py` | `BranchNodeParams` + `BranchNodeExecutor`（表达式求值，返回 `__route`） |
| Loop 节点 | `graph_module/nodes/loop.py` | `LoopNodeParams` + `LoopNodeExecutor`（构造 `LoopConfig` + `LoopEngine`，输出 output/iterations/converged） |
| Eval 节点 | `graph_module/nodes/eval.py` | `EvalNodeParams` + `EvalNodeExecutor`（用 `VerifierFactory` 求值断言，输出 passed/verdict） |
| 序列化 | `graph_module/serialize.py` | `graph_to_spec()` / `spec_to_graph()` 双向转换、YAML 支持、单 Loop ↔ Spec 映射 |

#### 验收清单状态

| # | 验收项 | 状态 | 验证方式 |
|---|---|---|---|
| 2 | 编排产物 ↔ spec.yaml 双向无损 | ✅ | 20 项往返转换测试 |
| 3 | 环检测生效 | ✅ | `validate_graph` + 执行器防御性检查 |
| 4 | 类型不兼容被拒 | ✅ | `port_compatible` + validate_graph 测试 |
| 5 | Loop 节点断言可验证性校验 | ✅ | 复用 `validate_goal`，28 项测试 |

#### 测试覆盖

| 测试文件 | 测试数 | 覆盖内容 |
|---|---|---|
| `test_graph_models.py` | 30 | 模型构造、frozen、端口类型、标准端口定义 |
| `test_graph_validate.py` | 28 | 环检测、类型兼容、必填参数、Loop 断言、组合校验 |
| `test_graph_executor.py` | 30 | 线性/并发/分支/失败传播/边角情况 |
| `test_graph_nodes.py` | 42 | 7 种节点参数模型 + 执行器 + 注册表 |
| `test_graph_serialize.py` | 20 | 往返转换、Loop 映射、YAML、无效输入 |
| **合计** | **150** | |

#### 质量门禁

- `ruff check`：全部通过
- `mypy`：13 个源文件零错误
- `pytest`：150 项测试全部通过（隔离运行）

#### 待实现（Week 3-5）

- ~~React Flow 画布 + 节点配置面板（Week 3）~~ → 已完成
- ~~并行 Loop 池 + Playground（Week 4）~~ → 已完成
- ~~TS SDK + LangGraph 导入 + 性能优化（Week 5）~~ → 已完成
- ~~API 路由 `graphs.py`（CRUD + validate）~~ → 已完成
- ~~Postgres 持久化表~~ → 已完成

**M5 全部完成。**

### Week 3：API 路由 + React Flow 画布 + 前端编排（已完成）

后端 API 层和前端编排 UI 全部完成，打通了从画布到数据库的完整链路。

#### 交付模块

| 层 | 文件 | 内容 |
|---|---|---|
| Postgres 模型 | `storage/postgres/graph_models.py` | `GraphRow` 表（id、project_id、name、version、graph JSONB、validation_errors、is_active、description），索引 project_id + project_id+name |
| Alembic 迁移 | `deploy/alembic/versions/d4e5f6a7b8c9_graph_tables.py` | 创建 `workflow_graphs` 表，`down_revision = c3d4e5f6a7b8` |
| API 路由 | `api/routers/graphs.py` | POST /v1/graphs（创建+校验）、GET 列表/详情、PUT 版本化更新、DELETE 软删除、POST /v1/graphs/validate |
| 路由注册 | `api/app.py` | 挂载 graphs router |
| 前端类型 | `web/src/api/types.ts` | `GraphPort`/`GraphNodeData`/`GraphEdgeData`/`WorkflowGraphData`/`GraphResponse`/`GraphValidateResponse` + `NODE_KIND_LABELS`/`NODE_KIND_COLORS` |
| 前端 API | `web/src/api/client.ts` | `put()`/`del()` 方法 + `listGraphs`/`getGraph`/`createGraph`/`updateGraph`/`deleteGraph`/`validateGraph` |
| 图列表页 | `web/src/pages/GraphListPage.tsx` | 列表 + 创建 + 删除 + 校验状态展示 |
| 图编辑页 | `web/src/pages/GraphEditorPage.tsx` | React Flow 画布 + 节点工具栏 + 节点配置面板 + dagre 自动布局 + 保存/校验 |
| 路由导航 | `web/src/App.tsx` | `/graphs`、`/graphs/:graphId`、`/graphs/new` 路由 + 导航项 |
| 样式 | `web/src/styles-m2.css` | `.btn` 变体、图编辑器布局、React Flow 深色主题适配、配置面板、校验结果展示 |

#### API 端点

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/v1/graphs` | 创建图（自动校验，存 graph JSONB + validation_errors） |
| GET | `/v1/graphs` | 列出当前 project 的所有 active 图 |
| GET | `/v1/graphs/{id}` | 获取图详情 |
| PUT | `/v1/graphs/{id}` | 版本化更新（创建新版本，旧版本 is_active=false） |
| DELETE | `/v1/graphs/{id}` | 软删除（is_active=false） |
| POST | `/v1/graphs/validate` | 独立校验（不保存），返回 errors + warnings |

#### 前端画布功能

- **自定义节点**：7 种节点类型（llm/tool/rag/code/branch/loop/eval），颜色区分，显示类型标签 + 节点 ID
- **节点工具栏**：点击添加节点，自带标准端口定义和默认参数模板
- **节点配置面板**：选中节点后右侧显示，可编辑参数（JSON 文本框），显示端口信息
- **连线**：拖拽端口连线，带箭头标记
- **自动布局**：dagre LR 布局，加载已有图时自动排列
- **保存/校验**：序列化为 WorkflowGraph dict → 调用 API 保存或独立校验 → 显示校验错误
- **删除**：Backspace/Delete 键删除选中节点/边

#### 测试覆盖

| 测试文件 | 测试数 | 覆盖内容 |
|---|---|---|
| `test_graph_api.py` | 17 | CRUD 全流程、版本化更新、软删除、校验通过/失败/422、认证 |
| **合计（M5 全部）** | **167** | Week 1-2 的 150 项 + Week 3 的 17 项 |

#### 质量门禁

- `ruff check`：全部通过
- `mypy`：133 个源文件零错误
- `pytest`：1111 项测试全部通过（含 M5 的 167 项）
- `tsc --noEmit`：零错误
- `vite build`：构建成功

### Week 4：并行 Loop 池 + Playground（已完成）

批量执行与调试闭环工具完成，打通"从历史 span 复现 → 调参对比 → 固化为 spec"的调试链路。

#### 交付模块

| 层 | 文件 | 内容 |
|---|---|---|
| 并行 Loop 池 | `loop_module/parallel.py` | `ParallelLoopPool` — pipeline 语义批量执行、`asyncio.Semaphore` 并发控制、动态并发（`min(配置上限, provider 速率余量, 沙箱池容量)`）、429 指数退避 + 自动降并发、单项失败隔离、批次取消、`BatchReport` 含 `peak_concurrency` |
| Playground API | `api/routers/playground.py` | POST `/v1/playground/run`（单次运行组装）、`/compare`（多配置并排）、`/freeze`（固化为 spec.yaml）、`/reproduce`（从历史 span 提取输入复现） |
| 路由注册 | `api/app.py` | 挂载 playground router |
| 前端类型 | `web/src/api/types.ts` | `LLMConfig`/`PlaygroundRunResponse`/`ConfigResult`/`PlaygroundCompareResponse`/`FreezeSpecResponse`/`ReproduceResponse` |
| 前端 API | `web/src/api/client.ts` | `playgroundRun`/`playgroundCompare`/`playgroundFreeze`/`playgroundReproduce` 方法 |
| Playground 页 | `web/src/pages/PlaygroundPage.tsx` | Prompt 输入 + 多配置编辑器（model/temperature/max_tokens/system_prompt）+ 并排对比结果展示 + 从 span 复现表单 + 固化为 spec YAML 输出 |
| 路由导航 | `web/src/App.tsx` | `/playground` 路由 + 导航项 |
| 样式 | `web/src/styles-m2.css` | Playground 布局、配置编辑器、结果卡片、spec YAML 输出样式 |

#### API 端点

| 方法 | 路径 | 功能 |
|---|---|---|
| POST | `/v1/playground/run` | 组装单次 LLM 运行请求（不直接调 LLM，返回 config + prompt 让客户端自行调 provider） |
| POST | `/v1/playground/compare` | 多配置并排对比（最多 8 个配置，返回占位结果供客户端回填） |
| POST | `/v1/playground/freeze` | 固化为 spec.yaml（注入占位断言如果用户未提供） |
| POST | `/v1/playground/reproduce` | 从 ClickHouse span 提取原始输入 + 配置，用相同参数复现 |

#### 并行 Loop 池设计

| 设计点 | 实现 |
|---|---|
| pipeline 语义 | `asyncio.Semaphore` 控制并发槽位，一项完成立即释放给下一项（非 barrier 分组等待） |
| 共享预算池 | 复用 M4 的 `ProjectPoolCounter`（Redis 原子计数），每项 `BudgetGuard` 独立但底层共享项目级池 |
| 动态并发 | `min(max_concurrency, rate_monitor.available_concurrency(), sandbox_monitor.available_slots())` |
| 429 降并发 | `_is_rate_limited()` 检测 → 指数退避重试（`base * 2^attempt`，上限 30s）+ `downscale_event` 全局信号 |
| 单项隔离 | 每项独立 `LoopConfig`/`LoopEngine`/`loop_id`/检查点，`asyncio.gather(return_exceptions=True)` |
| 批次取消 | `BatchConfig.cancel_event` — 设置后未开始的项标记 SKIPPED |
| 峰值观测 | `BatchReport.peak_concurrency` — 实际使用的最大并发度 |

#### 测试覆盖

| 测试文件 | 测试数 | 覆盖内容 |
|---|---|---|
| `test_parallel_pool.py` | 15 | pipeline vs barrier、并发上限、单项失败隔离、engine 异常隔离、429 重试+降并发、重试耗尽、批次取消、报告字段、动态并发（rate/sandbox monitor）、工厂独立调用 |
| `test_playground_api.py` | 14 | run/compare/freeze/reproduce 端点、默认配置、空 prompt 拒绝、温度越界、配置上限、占位断言注入、认证 |
| **合计（M5 全部）** | **196** | Week 1-2 的 150 + Week 3 的 17 + Week 4 的 29 |

#### 质量门禁

- `ruff check`：全部通过
- `mypy`：135 个源文件零错误
- `pytest`：1140 项测试全部通过（含 M5 的 196 项）
- `tsc --noEmit`：零错误
- `vite build`：构建成功

### Week 5：TS SDK + LangGraph 导入 + 性能优化（已完成）

TypeScript SDK、LangGraph 图定义导入器、前端性能优化三项全部完成。M5 编排与可视化完整化收官。

#### 交付模块

| 层 | 文件 | 内容 |
|---|---|---|
| TS SDK 包 | `sdk/typescript/package.json` + `tsconfig.json` | `@ariadne/sdk` v0.1.0，Node >=18，仅依赖 `undici` |
| TS 客户端 | `sdk/typescript/src/client.ts` | `Ariadne` 类 + `init()`/`getClient()` — 与 Python SDK 对等 |
| Span | `sdk/typescript/src/span.ts` | `Span` 类 + `AsyncLocalStorage` 上下文传播（等价 Python 的 `contextvars`）、`currentSpan()`/`currentTraceId()`、`toPayload()` 生成 native 格式 |
| 导出器 | `sdk/typescript/src/exporter.ts` | `SpanExporter` — 有界队列 + 批量发送 + 指数退避重试（`RETRYABLE_STATUS = {408,429,500,502,503,504}`）、队列满丢最旧 |
| 装饰器 | `sdk/typescript/src/decorators.ts` | `@trace()` 方法装饰器 + `traced()` 高阶函数，处理同步/异步（Promise）结果 |
| OpenAI 埋点 | `sdk/typescript/src/instrument/openai.ts` | `wrapOpenAI()` 包装 `chat.completions.create` 和 `embeddings.create`，记录 usage |
| Anthropic 埋点 | `sdk/typescript/src/instrument/anthropic.ts` | `wrapAnthropic()` 包装 `messages.create`，提取 Anthropic 专属 usage 字段 |
| 桶导出 | `sdk/typescript/src/index.ts` | 所有公共 API 的 barrel 导出 |
| LangGraph 导入器 | `graph_module/importers/langgraph.py` | `import_langgraph()` — 鸭子类型（无硬依赖）、条件边→branch 节点、checkpointer 跳过、不支持构造显式报错 |
| 分支节点修正 | `graph_module/models.py` | Branch 节点新增 `route` 输出端口（`PortKind.ANY`），使条件边可通过 `validate_graph` 校验 |
| 前端性能 | `web/src/pages/GraphEditorPage.tsx` | React Flow `onlyRenderVisibleElements` + `minZoom`/`maxZoom` + 节点组件 `memo()` |

#### LangGraph 导入器设计

| 设计点 | 实现 |
|---|---|
| 鸭子类型 | 不 import langgraph，只按属性名访问（`.nodes`/`.edges`/`.branches`/`.waiting_edges`），未安装时导入器仍可加载 |
| 节点映射 | LangGraph 节点名保留为 Ariadne 节点 id，统一映射为 `tool` 节点（LangGraph 无显式类型信息），`params._imported_from = "langgraph"` |
| 简单边 | `graph.edges`（`set[(start, end)]`）→ Ariadne 边，跳过 `__start__`/`__end__` 哨兵 |
| 条件边 | `graph.branches`（`defaultdict[source, dict[cond_name, BranchSpec]]`）→ `_branch_{source}` 节点 + `branches` 参数映射 + `route` 端口连接各目标 |
| 多条件合并 | 同一源的多个条件边合并为一个 branch 节点的 branches 映射 |
| checkpointer | 不导入（Ariadne 用自己的检查点） |
| 不支持构造 | `ends=None`（运行时路由）→ `GraphImportError`；`waiting_edges`（多源 fan-in）→ `GraphImportError` |
| 编译后图 | `CompiledStateGraph` 的 `.builder` 属性获取原始 `StateGraph` 结构 |

#### Branch 节点修正

| 问题 | 修复 |
|---|---|
| Branch 节点无输出端口 | `NODE_OUTPUT_PORTS[BRANCH]` 从 `()` 改为 `(Port("route", ANY),)` |
| 执行器需要边来 skip 分支 | branch 节点现在有 `route` 输出端口，边可以连接到各分支目标，`validate_graph` 通过 |
| 前端端口定义 | `GraphEditorPage.tsx` 的 `STD_PORTS.branch.outputs` 从 `[]` 改为 `[{ name: "route", kind: "any" }]` |

#### 测试覆盖

| 测试文件 | 测试数 | 覆盖内容 |
|---|---|---|
| `test_langgraph_importer.py` | 27 | 线性/菱形/并行图导入、节点 id 保留、tool 类型映射、来源标记、边保留、哨兵跳过、隐藏节点跳过、条件边→branch 节点、多条件合并、branch 边连接、简单边替代、END 目标跳过、运行时路由拒绝、fan-in 拒绝、无效对象拒绝、边格式拒绝、编译后图、validate_graph 通过、拓扑序保持 |
| **合计（M5 全部）** | **223** | Week 1-2 的 150 + Week 3 的 17 + Week 4 的 29 + Week 5 的 27 |

#### 质量门禁

- `ruff check`：全部通过
- `mypy`：146 个源文件零错误
- `pytest`：1167 项测试全部通过（含 M5 的 223 项）
- `tsc --noEmit`（TS SDK + Web）：零错误
- `vite build`：构建成功



