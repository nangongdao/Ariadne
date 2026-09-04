"""Loop 工具执行 —— `LoopConfig.tool_executor` 的契约与生产实现。

审计 P0-4（R12 同族）：该字段声明了一个"副作用执行钩子"，但引擎从未
调用它，Worker 也从未装配它 —— 建好但没人接线的死配置。本模块补两件事：

1. **契约**：模型在输出里用围栏块显式请求工具调用 ——
   ```ariadne-tool
   {"tool": "write_file", "args": {"path": "config.json", "content": "{}"}}
   ```
   引擎在产出物落盘之后解析并执行这些块。选围栏块而非 JSON 工具协议，
   与 artifact.py 的落盘解析是同一设计判断：这是模型在代码任务里的自然
   输出形式，不需要额外的调用协议。解析复用 parse_blocks（同一套围栏
   正则与去缩进处理，被 artifact 测试钉死过）。

2. **生产实现**：WorkspaceToolExecutor —— 工作目录作用域的 write_file。
   路径安全与大小限制直接走 materialize（防穿越、防写满磁盘，同一份
   代码而不是各写各的）。

当前边界（刻意收紧，别放宽）：
- 只有**副作用型**工具。工具结果只进日志与审计，不回灌下一轮上下文
  （回灌要动 ContextBuilder 的分段结构）—— 因此不要加 read_file 之类
  "结果对模型不可见就毫无意义"的工具。
- 未配置 tool_executor 时模型请求工具 → 本轮按执行失败处理，critique
  会告诉模型改用围栏代码块落盘。**绝不静默忽略**：模型以为写成功了而
  断言在验证磁盘，静默忽略就是空转烧预算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from ariadne.loop_module.artifact import materialize, parse_blocks
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

# 围栏块信息串里标识工具调用的语言标注
TOOL_FENCE_LANGUAGE: Final = "ariadne-tool"

# 单轮工具调用上限。与 MAX_FILES_PER_WRITE 同量级：太多调用说明模型在
# 用工具做本该一次落盘完成的事，熔掉比放大成本低。
MAX_TOOL_CALLS: Final = 16


class ToolExecutionError(RuntimeError):
    """工具执行失败。engine 捕获后转成执行失败，消息进 critique。"""


@dataclass(frozen=True)
class ToolDirective:
    """一条工具调用请求。"""

    name: str
    args: dict[str, object]


@dataclass(frozen=True)
class ToolParseReport:
    """解析结果。directives 与 errors 可以同时非空（部分块合法）。"""

    directives: tuple[ToolDirective, ...] = ()
    errors: tuple[str, ...] = ()


def parse_tool_directives(output: str) -> ToolParseReport:
    """从模型输出解析 ariadne-tool 围栏块。纯函数。

    畸形块（非 JSON、缺 tool 字段、args 非对象）进 errors 而不是抛异常
    —— 解析失败是模型输出质量问题，critique 拿到描述后模型下一轮能修。
    """
    directives: list[ToolDirective] = []
    errors: list[str] = []
    for block in parse_blocks(output):
        if block.language.lower() != TOOL_FENCE_LANGUAGE:
            continue
        try:
            payload = json.loads(block.body)
        except json.JSONDecodeError as exc:
            errors.append(f"ariadne-tool 块不是合法 JSON: {exc}")
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("tool"), str):
            errors.append('ariadne-tool 块缺少字符串字段 "tool"')
            continue
        name = payload["tool"]
        args = payload.get("args", {})
        if not isinstance(args, dict):
            errors.append(f"工具 {name} 的 args 必须是对象")
            continue
        directives.append(
            ToolDirective(
                name=name,
                args={str(k): v for k, v in args.items()},
            )
        )

    if len(directives) > MAX_TOOL_CALLS:
        errors.append(
            f"单轮工具调用 {len(directives)} 次超过上限 {MAX_TOOL_CALLS}，"
            "超出部分已丢弃"
        )
        directives = directives[:MAX_TOOL_CALLS]
    return ToolParseReport(directives=tuple(directives), errors=tuple(errors))


@dataclass
class WorkspaceToolExecutor:
    """工作目录作用域的文件写入工具（生产装配）。

    目录按 loop_id 确定性命名，与 `_prepare_workspace` 的约定一致：
    带 COMMAND 断言的 Loop 两处是**同一个目录**（模型用工具写的文件和
    用围栏块写的文件都会被同一组命令断言验证）；不带 COMMAND 的 Loop
    首次调用工具时才创建目录。

    满足 `Callable[[str, dict[str, object]], Awaitable[str]]`。

    有 harness 时挂 pre_persist 卡点（与 GuardedArtifactWriter 同构）：
    工具写入与产出物落盘共用同一套 pre_persist 规则
    （output-sensitive-high / output-huge），否则模型用工具写敏感内容
    会绕过内容规则 —— 工具口与产出物口是同一工作目录，规则作者没有
    理由预期两处行为不同。
    """

    base_dir: Path
    loop_id: str
    harness: Any | None = None
    audit_sink: Any = None
    project_id: UUID | None = None
    loop_state_provider: Callable[[], dict[str, object]] | None = None
    _ensured: bool = field(default=False, repr=False)

    @property
    def workdir(self) -> Path:
        if not self._ensured:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ToolExecutionError(
                    f"无法创建工具工作目录 {self._dir}: {exc}"
                ) from exc
            self._ensured = True
        return self._dir

    def __post_init__(self) -> None:
        self._dir = self.base_dir / self.loop_id

    async def __call__(self, name: str, args: dict[str, object]) -> str:
        if name != "write_file":
            raise ToolExecutionError(
                f"未知工具 {name!r}。可用工具: write_file"
                "（read 类工具暂不支持：结果无法回灌模型上下文）"
            )
        relative = args.get("path")
        content = args.get("content")
        if not isinstance(relative, str) or not relative.strip():
            raise ToolExecutionError('write_file 需要字符串参数 "path"')
        if not isinstance(content, str):
            raise ToolExecutionError('write_file 需要字符串参数 "content"')

        # pre_persist 卡点：工具写入与产出物落盘共用同一套内容规则
        if self.harness is not None:
            from ariadne.harness_module.audit import NullAuditSink
            from ariadne.harness_module.models import Action
            from ariadne.runtime_module.artifact.guarded import (
                audit_pre_persist_decision,
                evaluate_pre_persist,
            )

            sink = self.audit_sink or NullAuditSink()
            decision = evaluate_pre_persist(
                evaluator=self.harness,
                output_text=content,
                audit_sink=sink,
                loop_state_provider=self.loop_state_provider,
                project_id=self.project_id,
                loop_id=self.loop_id,
            )
            from ariadne.harness_module.models import HarnessContext, HookKind

            audit_pre_persist_decision(
                decision=decision,
                context=HarnessContext(
                    hook=HookKind.PRE_PERSIST,
                    artifact={"text": content},
                    output={"text": content},
                    loop={},
                ),
                audit_sink=sink,
                project_id=self.project_id,
                loop_id=self.loop_id,
                output_len=len(content),
            )
            if decision.action in (Action.BLOCK, Action.REQUIRE_APPROVAL):
                message = (
                    decision.winning_hit.message
                    if decision.winning_hit
                    else "工具写入被 Harness 规则拦截"
                )
                rule_id = (
                    decision.winning_hit.rule.id if decision.winning_hit else ""
                )
                logger.warning(
                    "harness blocked tool write at pre_persist",
                    extra={"loop_id": self.loop_id, "rule_id": rule_id},
                )
                raise ToolExecutionError(
                    f"被 Harness 规则拦截（{rule_id}）：{message}"
                    if rule_id
                    else message
                )

        # materialize 内置路径穿越拒绝与单文件/总量上限 —— 工具写入与
        # 种子文件、产出物落盘共用同一份安全边界
        report = materialize(((relative, content),), self.workdir)
        if not report.ok:
            raise ToolExecutionError(report.error)
        return f"已写入 {relative}（{len(content.encode('utf-8'))} 字节）"


__all__ = [
    "MAX_TOOL_CALLS",
    "TOOL_FENCE_LANGUAGE",
    "ToolDirective",
    "ToolExecutionError",
    "ToolParseReport",
    "WorkspaceToolExecutor",
    "parse_tool_directives",
]
