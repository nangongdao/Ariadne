# Changelog

本文档记录 Ariadne 项目的重要变更和里程碑。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 当前状态（2026-09-03）

项目已完成所有计划内的开发工作（M1-M6 里程碑），达到**开发版/单机自托管版**的交付标准。

**里程碑完成情况**：
- ✅ M1: 可观测性基础设施（Trace/Span、ClickHouse、时序聚合）
- ✅ M2: 评测与实验对比（数据集版本、批量评测、指标对比）
- ✅ M3: Loop 闭环引擎（目标校验、外部验证、预算熔断、产出物落盘）
- ✅ M4: Harness 与沙箱（PII/注入检测、规则引擎、Windows 受限子进程）
- ✅ M5: 编排与可视化（React Flow 图编辑器、LangGraph 导入、前端工作台）
- ✅ M6: 生产化模块（RBAC、租户隔离、S3 对象存储、Prometheus 指标）

**关键缺陷修复**（P0-P2，共 13 项）：
- ✅ P0-1: config.env 从未被读（JWT 用公开占位符）
- ✅ P0-2: 评估器注册表短路（参数静默丢弃）
- ✅ P0-3: Loop 入队失败静默丢失（补偿扫描）
- ✅ P0-4: tool_executor 未接线（工作区工具执行器）
- ✅ P0-5: npx/npm 供应链缺口（参数级白名单）
- ✅ P1-6: Playground 定位不明（明确为客户端请求组装器）
- ✅ P1-7: Graph 执行同步阻塞（异步 Job 模型 MVP）
- ✅ P1-8: 删除 Worker RLS 致盲（租约机制 + 租户会话）
- ✅ P1-9: 成本 token 语义不完整（cache_write/reasoning 字段）
- ✅ P2-10: Fernet 密钥无轮换支持（多密钥并存）
- ✅ P2-11: 健康端点无认证保护（可选 Bearer 认证）
- ✅ P2-12: Harness 卡点未接线（post_tool/pre_persist）
- ✅ P2-13: ECharts chunk 体积过大（按需引入 + 懒加载）

**测试覆盖**：
- 后端：2254 passed, 29 skipped, 1 xfailed
- 前端：61 Vitest + 22 Playwright E2E
- 代码质量：mypy 205 文件无错误、ruff check 全绿

**文档体系**（23 个文档）：
- 设计文档：架构、Loop 引擎、Harness、评测、可观测性、前端可视化
- 规格文档：M1-M6 六个里程碑规格
- 运维文档：部署指南、API/SDK、数据模型、安全
- 审计文档：项目审计报告、Judge 套件评估、LLM 能力验收框架
- 总结文档：愿景与目标、路线图、开发完成总结、项目状态报告

**适用场景**：
- ✅ 可信代码自托管（用户自己的代码在自己的机器上跑）
- ✅ 开发与测试环境
- ✅ 内部团队协作
- ✅ 单租户部署

**不适用场景**：
- ❌ 多租户 SaaS（需 Linux + gVisor/Firecracker 沙箱）
- ❌ 不可信代码执行（Windows 平台无法提供沙箱级隔离）

**待完成事项**（需外部资源）：
- 真实 LLM 能力验收（框架已就绪，需 ANTHROPIC_API_KEY 或 OPENAI_API_KEY）
- 百万级 Trace 压测（需压测环境）
- S3 冷热分层实现（需外部存储配置）

**可选增强**：
- Graph 执行阶段 2/3（检查点恢复、租约机制、补偿扫描、取消）
- ECharts chunk 进一步优化（当前 599KB gzipped 205KB）

---

## [0.1.0] - 2026-09-03

### Added - 核心功能

#### 可观测性（M1）
- Trace/Span 采集与存储（ClickHouse + PostgreSQL）
- 调用树与时间轴可视化
- 成本归因（按模型、Provider、时间维度）
- 游标分页与虚拟滚动
- 失败筛选与状态过滤

#### 评测与实验（M2）
- 数据集版本管理
- 批量实验执行
- 指标对比与样本级差异下钻
- 回归门禁
- 多评估器支持（Exact/Fuzzy/Regex/LLM-as-Judge/自定义）

#### Loop 闭环引擎（M3）
- 目标校验与外部验证（Ralph 可执行断言）
- 预算熔断（token/成本/时间/轮次）
- 检查点恢复
- SSE 状态流
- 产出物落盘与工作区工具执行
- Critique 失败分析
- 假完成拦截率 100%、闭环达标率 100%（27 个验收用例）

#### Harness 与沙箱（M4）
- PII 检测（电话/邮箱/身份证/银行卡）
- Prompt Injection 检测（D8 引擎）
- 引用完整性校验
- 危险命令检测（rm -rf / dd / mkfs 等）
- 供应链白名单（npx 包名、npm 子命令/脚本）
- Windows 受限子进程（Job Object 资源限制）
- 规则动作：allow / warn / block / rewrite / route / approval

#### 编排与可视化（M5）
- React Flow 图编辑器（LLM/工具/RAG/代码/分支/Loop/评估/子图节点）
- DAG 校验与类型兼容检查
- 条件分支与并发执行
- LangGraph 图导入（StateGraph → Workflow Graph）
- YAML 双向序列化
- 未保存修改守卫
- 自动布局与缩略图导航

#### 生产化（M6）
- RBAC 权限控制（PostgreSQL RLS）
- 租户隔离（租户变量 + RLS 策略）
- API Key 管理（Fernet 加密 + 密钥轮换）
- JWT 认证（Argon2id 密码哈希）
- 审计日志
- S3 对象存储（大 Trace 溢写）
- TTL 与 GDPR 删除（RetentionWorker）
- Prometheus 指标导出
- Kubernetes + Helm 部署支持

### Added - 前端特性

#### 核心交互
- 暗色模式（Newsprint 风格，light-dark() 单份令牌）
- 三档主题切换（日间/夜间/跟随系统）
- 响应式三断点布局（< 768px / 768-1024px / > 1024px）
- 移动端侧栏抽屉与遮罩
- 触控优化（44×44px 最小触控目标）
- 键盘导航与焦点管理
- WCAG AA 无障碍合规

#### 视觉设计
- Noto Sans SC Variable 800 字重粗体中文
- Inter Variable 拉丁字体
- JetBrains Mono 等宽字体
- 全站统一圆角令牌（卡片 14px、按钮 8px、标签 999px）
- 五级阶梯深色表面（#05070C → #1E2636）

#### 页面功能
- Trace 列表与详情（调用树/时间轴切换）
- Loop 详情页（断言结果、失败证据、预算消耗、产出物）
- 实验对比页（指标聚合、样本下钻）
- 图编排编辑器（React Flow）
- Playground（Prompt 调试、配置对比、固化 Spec）
- 模型配置管理
- 成本分组与趋势
- 数据集与评估器管理
- 内置终端（xterm.js）
- 设置页（API Key、健康状态、管道状态）

#### 性能优化
- ECharts/React Flow/xterm 独立 chunk
- 路由级懒加载
- 虚拟滚动（Trace 树）
- 游标分页
- 按需引入（ECharts 仅 Line/Bar/Pie）

### Added - API 与 SDK

#### REST API
- `/v1/traces` - Trace 采集与查询
- `/v1/spans` - Span 查询与过滤
- `/v1/loops` - Loop 创建、状态查询、取消
- `/v1/experiments` - 批量实验执行
- `/v1/datasets` - 数据集版本管理
- `/v1/evaluators` - 评估器注册与查询
- `/v1/graphs` - 图定义、执行、导入 LangGraph
- `/v1/graphs/runs` - Graph 执行状态查询（异步 Job 模型）
- `/v1/playground` - 请求组装（客户端执行）
- `/v1/models` - 模型配置管理
- `/v1/costs` - 成本聚合与下钻
- `/v1/harness/rules` - Harness 规则管理
- `/v1/auth` - 登录与令牌刷新
- `/v1/health` - 健康检查（可选认证）

#### SDK
- Python SDK（OpenAI/Anthropic 自动埋点）
- TypeScript SDK
- OTLP 适配器
- OpenInference 兼容

### Added - 基础设施

#### 数据库
- PostgreSQL（Trace/Span/Loop/Graph 状态、租户数据）
- ClickHouse（时序聚合、成本归因、物化视图）
- Redis（任务队列、分布式锁）
- Alembic 迁移（15 个迁移文件）

#### Worker
- LoopWorker（Loop 执行 + 补偿扫描）
- GraphWorker（Graph 异步执行）
- RetentionWorker（TTL 删除 + 租约机制）
- EvaluationWorker（批量评测）

#### 部署
- Docker Compose（开发环境）
- Kubernetes + Helm（生产环境）
- 环境变量配置（60+ 配置项）
- 健康检查端点
- Prometheus 指标导出

### Fixed - 关键缺陷

详见上方"关键缺陷修复"部分（P0-P2，共 13 项）。

### Security

- Argon2id 密码哈希
- JWT 认证（HS256 + 密钥轮换支持）
- API Key Fernet 加密
- PostgreSQL RLS 租户隔离
- Harness 规则引擎（PII/注入/危险命令检测）
- 供应链白名单（npx/npm 参数级校验）
- CSRF 保护
- 审计日志
- GDPR 删除支持

### Documentation

- 23 个文档文件（设计/规格/运维/审计/总结）
- API 文档（OpenAPI/Swagger）
- 部署指南（Docker Compose/Kubernetes/安全加固）
- 架构图与数据流图
- 开发完成总结
- 项目状态报告

### Testing

- 2254 后端单元测试 + 集成测试
- 61 前端 Vitest 测试
- 22 Playwright E2E 测试
- mypy 类型检查（205 文件）
- ruff 代码风格检查
- M3 闭环验收基准（80 秒真实执行）

---

## 版本说明

- **[Unreleased]**：当前开发状态，M1-M6 已完成，待外部资源验收
- **[0.1.0]**：首个开发版/单机自托管版，达到路线图设定的交付标准

---

## 参考文档

- [项目状态报告](docs/16-project-status-2026-09-03.md) - 完整项目状态与验收判定
- [开发完成总结](docs/15-development-completion-summary.md) - 技术细节与实现情况
- [审计报告](docs/12-audit-report.md) - 缺陷分析与修复记录
- [部署指南](docs/14-deployment-guide.md) - 生产部署步骤与安全加固
- [路线图](docs/11-roadmap.md) - 原始开发计划
- [架构文档](docs/02-architecture.md) - 系统架构设计

---

**项目定位**：机制已成型的开发版/单机自托管版  
**适用场景**：可信代码自托管、开发环境、单租户部署  
**不适用场景**：多租户 SaaS、不可信代码执行（Windows 平台限制）  
**下一步**：等待外部资源（LLM provider key、压测环境）进行最终验收
