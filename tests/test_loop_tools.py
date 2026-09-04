"""Loop 工具执行测试（审计 P0-4：tool_executor 从未接线）。

三层都要有测试：
1. 解析（纯函数）：ariadne-tool 围栏块 → ToolDirective
2. 引擎接线：指令被真的执行、失败进 critique、未配置执行器 fail-closed、
   幂等守卫、Harness pre_tool 拦截
3. 生产实现：WorkspaceToolExecutor 的路径安全与错误信息
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
from ariadne.loop_module.checkpoint import Checkpoint, CheckpointStore
from ariadne.loop_module.engine import LLMResponse, LoopConfig, LoopEngine, LoopEvent
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.tools import (
    TOOL_FENCE_LANGUAGE,
    ToolExecutionError,
    WorkspaceToolExecutor,
    parse_tool_directives,
)

LOOP_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


# ---------- 1. 解析 ----------


class TestParseToolDirectives:
    def test_parses_directive(self) -> None:
        output = (
            "我先写配置。\n"
            "```ariadne-tool\n"
            '{"tool": "write_file", "args": {"path": "a.json", "content": "{}"}}\n'
            "```\n"
        )
        report = parse_tool_directives(output)
        assert not report.errors
        assert len(report.directives) == 1
        assert report.directives[0].name == "write_file"
        assert report.directives[0].args == {"path": "a.json", "content": "{}"}

    def test_ignores_non_tool_fences(self) -> None:
        report = parse_tool_directives("```python\nprint('hi')\n```\n")
        assert report.directives == ()
        assert not report.errors

    def test_malformed_json_is_error_not_exception(self) -> None:
        report = parse_tool_directives("```ariadne-tool\n{not json\n```")
        assert report.directives == ()
        assert any("JSON" in e for e in report.errors)

    def test_missing_tool_field_is_error(self) -> None:
        report = parse_tool_directives('```ariadne-tool\n{"args": {}}\n```')
        assert report.directives == ()
        assert any('"tool"' in e for e in report.errors)

    def test_non_object_args_is_error(self) -> None:
        report = parse_tool_directives(
            '```ariadne-tool\n{"tool": "write_file", "args": [1]}\n```'
        )
        assert report.directives == ()
        assert any("args" in e for e in report.errors)

    def test_call_cap(self) -> None:
        block = (
            "```ariadne-tool\n"
            '{"tool": "write_file", "args": {"path": "x", "content": ""}}\n'
            "```\n"
        )
        report = parse_tool_directives(block * 30)
        assert len(report.directives) == 16
        assert any("超过上限" in e for e in report.errors)

    def test_language_marker_is_stable_contract(self) -> None:
        """围栏标注是模型可见的契约，改名等于破坏所有存量输出。"""
        assert TOOL_FENCE_LANGUAGE == "ariadne-tool"


# ---------- 2. 引擎接线 ----------


class RecordingTools:
    """记录调用的工具执行器；可注入失败。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.fail = fail

    async def __call__(self, name: str, args: dict[str, object]) -> str:
        self.calls.append((name, args))
        if self.fail:
            raise ToolExecutionError("boom")
        return f"ok:{name}"


class _MemoryIdempotency:
    """IdempotencyStore Protocol 的内存实现。"""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._acquired: set[str] = set()

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool:
        if key in self._acquired:
            return False
        self._acquired.add(key)
        return True

    async def recall(self, key: str) -> str | None:
        return self._values.get(key)

    async def remember(self, key: str, payload: str, ttl_seconds: int) -> None:
        self._values[key] = payload


def make_goal() -> Goal:
    return Goal(
        task="写配置文件",
        assertions=(
            Assertion(id="ok", kind=AssertionKind.REGEX, spec={"pattern": "."}),
        ),
        budget=Budget(max_iterations=3, max_total_tokens=100_000),
        mode="quality",
    )


class NullStore(CheckpointStore):
    async def save(self, checkpoint: Checkpoint, *, project_id: uuid.UUID) -> None: ...

    async def latest(
        self, loop_id: str, *, project_id: uuid.UUID
    ) -> Checkpoint | None:
        return None


class _StaticLLM:
    def __init__(self, output: str) -> None:
        self._output = output

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        return LLMResponse(
            output=self._output,
            input_tokens=10,
            output_tokens=10,
            cost_usd=Decimal("0.01"),
        )


def make_engine(output: str, *, tool_executor: Any = ...) -> LoopEngine:
    """构造能跑单轮迭代的 engine。tool_executor=... 表示不注入。"""
    goal = make_goal()
    kwargs: dict[str, Any] = {}
    if tool_executor is not ...:
        kwargs["tool_executor"] = tool_executor
    return LoopEngine(
        LoopConfig(
            goal=goal,
            loop_id=LOOP_ID,
            project_id=PROJECT_ID,
            budget_guard=BudgetGuard(
                loop_id=LOOP_ID, budget=goal.budget, counter=InMemoryCounter()
            ),
            llm=_StaticLLM(output),  # type: ignore[arg-type]
            checkpoint_store=NullStore(),
            **kwargs,
        )
    )


async def run_one_iteration(engine: LoopEngine) -> None:
    """与 engine.run() 首轮相同的路径：VALIDATE → 一整步迭代到 JUDGING。"""
    await engine._advance(LoopEvent.START)
    await engine._advance(await engine._validate())
    await engine._step()


class TestEngineToolWiring:
    async def test_directive_executed(self) -> None:
        output = (
            '```ariadne-tool\n{"tool": "write_file", '
            '"args": {"path": "x.txt", "content": "hi"}}\n```\n'
        )
        tools = RecordingTools()
        engine = make_engine(output, tool_executor=tools)
        await run_one_iteration(engine)
        assert tools.calls == [("write_file", {"path": "x.txt", "content": "hi"})]
        # 工具成功不产生执行错误，本轮正常进入验证
        assert engine._execution_error == ""

    async def test_no_directives_is_noop(self) -> None:
        tools = RecordingTools()
        engine = make_engine("普通输出，没有工具调用", tool_executor=tools)
        await run_one_iteration(engine)
        assert tools.calls == []

    async def test_tool_failure_fails_execution(self) -> None:
        tools = RecordingTools(fail=True)
        engine = make_engine(
            '```ariadne-tool\n{"tool": "write_file", "args": {}}\n```',
            tool_executor=tools,
        )
        await run_one_iteration(engine)
        assert "boom" in engine._execution_error

    async def test_missing_executor_fails_closed(self) -> None:
        """未配置执行器时绝不能静默 —— 模型以为副作用发生了。"""
        engine = make_engine(
            '```ariadne-tool\n{"tool": "write_file", "args": {"path": "x"}}\n```'
        )
        await run_one_iteration(engine)
        assert "未配置工具执行器" in engine._execution_error

    async def test_parse_error_fails_execution(self) -> None:
        engine = make_engine(
            "```ariadne-tool\n{broken\n```", tool_executor=RecordingTools()
        )
        await run_one_iteration(engine)
        assert "工具执行失败" in engine._execution_error


class TestEngineToolIdempotency:
    async def test_replayed_directive_reuses_result(self) -> None:
        """Worker 接管后重放同一轮：幂等命中，不重复执行副作用。"""

        class OnceTools:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, name: str, args: dict[str, object]) -> str:
                self.calls += 1
                return "done"

        output = (
            '```ariadne-tool\n{"tool": "write_file", '
            '"args": {"path": "x", "content": "y"}}\n```\n'
        )
        tools = OnceTools()
        store = _MemoryIdempotency()

        for _ in range(2):
            engine = make_engine(output, tool_executor=tools)
            engine._cfg.idempotency = store
            await run_one_iteration(engine)
        assert tools.calls == 1


class TestEngineToolHarness:
    @staticmethod
    def _harness(tool_decision: Any) -> Any:
        """PRE_MODEL 放行、PRE_TOOL 按参数裁决的桩。"""

        class _Decision:
            def __init__(self, blocked: bool, needs_approval: bool, action: str) -> None:
                self.blocked = blocked
                self.needs_approval = needs_approval
                self.action = action
                self.hits: tuple[Any, ...] = ()
                self.winning_hit = None

        allow = _Decision(False, False, "allow")

        class Harness:
            def evaluate(self, *, hook: Any, context: Any) -> Any:
                if hook.value != "pre_tool":
                    return allow
                assert context.tool["tool"] == "write_file"
                assert context.tool["args"] == {}
                return tool_decision

        return Harness()

    async def test_blocked_tool_fails_execution(self) -> None:
        """pre_tool 拦截是硬约束：本轮执行失败，且工具没被调用。"""

        class _Decision:
            blocked = True
            needs_approval = False
            hits: tuple[Any, ...] = ()
            winning_hit = None
            action = "block"

        tools = RecordingTools()
        engine = make_engine(
            '```ariadne-tool\n{"tool": "write_file", "args": {}}\n```',
            tool_executor=tools,
        )
        engine._cfg.harness = self._harness(_Decision())
        await run_one_iteration(engine)
        assert "被 Harness 规则拦截" in engine._execution_error
        assert tools.calls == []

    async def test_approval_required_fails_closed(self) -> None:
        """执行路径上没有审批挂起点：needs_approval 按失败处理。"""

        class _Decision:
            blocked = False
            needs_approval = True
            hits: tuple[Any, ...] = ()
            winning_hit = None
            action = "require_approval"

        tools = RecordingTools()
        engine = make_engine(
            '```ariadne-tool\n{"tool": "write_file", "args": {}}\n```',
            tool_executor=tools,
        )
        engine._cfg.harness = self._harness(_Decision())
        await run_one_iteration(engine)
        assert "人工审批" in engine._execution_error
        assert tools.calls == []

    async def test_allowed_tool_executes(self) -> None:
        class _Decision:
            blocked = False
            needs_approval = False
            hits: tuple[Any, ...] = ()
            winning_hit = None
            action = "allow"

        tools = RecordingTools()
        engine = make_engine(
            '```ariadne-tool\n{"tool": "write_file", "args": {}}\n```',
            tool_executor=tools,
        )
        engine._cfg.harness = self._harness(_Decision())
        await run_one_iteration(engine)
        assert tools.calls == [("write_file", {})]


# ---------- 3. 生产实现 ----------


class TestWorkspaceToolExecutor:
    async def test_write_file(self, tmp_path: Path) -> None:
        executor = WorkspaceToolExecutor(base_dir=tmp_path, loop_id=LOOP_ID)
        result = await executor("write_file", {"path": "a/b.txt", "content": "hi"})
        assert "a/b.txt" in result
        assert (tmp_path / LOOP_ID / "a" / "b.txt").read_text() == "hi"

    async def test_path_traversal_rejected(self, tmp_path: Path) -> None:
        executor = WorkspaceToolExecutor(base_dir=tmp_path, loop_id=LOOP_ID)
        with pytest.raises(ToolExecutionError, match="越界"):
            await executor(
                "write_file", {"path": "../../evil.txt", "content": "x"}
            )

    async def test_unknown_tool_rejected(self, tmp_path: Path) -> None:
        executor = WorkspaceToolExecutor(base_dir=tmp_path, loop_id=LOOP_ID)
        with pytest.raises(ToolExecutionError, match="未知工具"):
            await executor("send_email", {})

    async def test_missing_args_rejected(self, tmp_path: Path) -> None:
        executor = WorkspaceToolExecutor(base_dir=tmp_path, loop_id=LOOP_ID)
        with pytest.raises(ToolExecutionError, match="path"):
            await executor("write_file", {"content": "x"})

    async def test_no_root_escape(self, tmp_path: Path) -> None:
        """绝对路径同样拒绝。"""
        executor = WorkspaceToolExecutor(base_dir=tmp_path, loop_id=LOOP_ID)
        with pytest.raises(ToolExecutionError):
            await executor(
                "write_file", {"path": "C:/Windows/temp/evil.txt", "content": "x"}
            )

    async def test_pre_persist_block_rejects_write(self, tmp_path: Path) -> None:
        """配了 harness 时，pre_persist BLOCK 拦截工具写入（与产出物口同卡点）。"""

        class BlockDecision:
            action = "block"
            hits: tuple[Any, ...] = ()
            winning_hit = None

        class BlockHarness:
            def evaluate(self, *, hook: Any, context: Any) -> BlockDecision:
                assert hook.value == "pre_persist"
                assert context.output["text"] == "sensitive"
                return BlockDecision()

        executor = WorkspaceToolExecutor(
            base_dir=tmp_path, loop_id=LOOP_ID, harness=BlockHarness()
        )
        with pytest.raises(ToolExecutionError, match="被 Harness 规则拦截"):
            await executor("write_file", {"path": "secret.txt", "content": "sensitive"})
        # 拦截后文件不得落盘
        assert not (tmp_path / LOOP_ID / "secret.txt").exists()

    async def test_pre_persist_allow_writes(self, tmp_path: Path) -> None:
        """配了 harness 且 pre_persist 放行时正常写入。"""

        class AllowDecision:
            action = "allow"
            hits: tuple[Any, ...] = ()
            winning_hit = None

        class AllowHarness:
            def evaluate(self, *, hook: Any, context: Any) -> AllowDecision:
                return AllowDecision()

        executor = WorkspaceToolExecutor(
            base_dir=tmp_path, loop_id=LOOP_ID, harness=AllowHarness()
        )
        result = await executor("write_file", {"path": "ok.txt", "content": "fine"})
        assert "ok.txt" in result
        assert (tmp_path / LOOP_ID / "ok.txt").read_text() == "fine"
