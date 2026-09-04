"""可观测性模块：Prometheus 指标 + SLO 燃烧率引擎 + 告警 + 归因。

M6 §9 验收项 #7：错误预算告警带归因（project/model/assertion 维度）。

三个层次：
1. **metrics.py** — 所有 Prometheus 指标集中声明（21 个指标族），
   覆盖 Loop/Harness/Eval/Collector/Sandbox 全路径。
2. **slo.py** — 多窗口燃烧率 SLO 引擎（Google SRE 双窗口模式）。
3. **alerts.py** — 告警生成 + 归因注入 + 去重（AlertManager）。
"""

from __future__ import annotations

from ariadne.observability import metrics  # noqa: F401 — 副作用：注册指标
