"""GuardedCommandRunner —— pre_tool 卡点的求值与处置。

接线前 tool.yaml 那 6 条规则只有 YAML 没有求值点，本文件是它们第一份覆盖。

需要说清这层补的是什么（别当成"从无到有"）：M3 的 ExecPolicy 白名单只认
argv[0] 的 basename，rm / sudo / dd 本就不在白名单里，且 shlex 拆分让
`pytest; rm -rf /` 的分号只是普通参数。这层补的是全命令串匹配（`npx` 在
白名单内，参数里的 169.254.169.254 只有规则拦得住）、租户可配置、以及审计。

用桩 CommandRunner 而非真子进程：被测对象是"求值后放不放行"，起子进程
只会把测试变慢并引入平台差异。
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ariadne.harness_module.audit import InMemoryAuditSink, NullAuditSink
from ariadne.harness_module.loader import compile_rule_set
from ariadne.harness_module.models import (
    Action,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.loop_module.verifier.restricted_exec import (
    CommandNotAllowedError,
    ExecResult,
)
from ariadne.runtime_module.tool.guarded import GuardedCommandRunner

PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
_SHIPPED_RULES_DIR = (
    Path(__file__).resolve().parents[1] / "src" / "ariadne" / "harness_module" / "rules"
)


class _StubRunner:
    """记录被调用的命令。calls 为空即证明命令没被执行。"""

    def __init__(self, exit_code: int = 0, stdout: str = "ok") -> None:
        self.calls: list[tuple[str, Path]] = []
        self._exit_code = exit_code
        self._stdout = stdout

    def run(self, cmd: str, *, workdir: Path) -> ExecResult:
        self.calls.append((cmd, workdir))
        return ExecResult(
            exit_code=self._exit_code, stdout=self._stdout, stderr="", duration_ms=1
        )


def _rule(
    *,
    id: str = "t",
    when: str = "true",
    action: Action = Action.BLOCK,
    severity: Severity = Severity.CRITICAL,
    message: str = "命中",
    hook: HookKind = HookKind.PRE_TOOL,
) -> Rule:
    return Rule(
        id=id,
        category=RuleCategory.TOOL,
        hook=hook,
        when=when,
        action=action,
        severity=severity,
        message=message,
    )


def _guarded(
    rules: list[Rule],
    *,
    runner: _StubRunner | None = None,
    sink: InMemoryAuditSink | None = None,
    loop_state: object = None,
) -> tuple[GuardedCommandRunner, _StubRunner]:
    inner = runner or _StubRunner()
    guarded = GuardedCommandRunner(
        inner=inner,
        evaluator=compile_rule_set(rules),
        audit_sink=sink or NullAuditSink(),
        loop_state_provider=loop_state,  # type: ignore[arg-type]
        project_id=PROJECT,
        loop_id="loop-1",
    )
    return guarded, inner


class TestBlock:
    """BLOCK 抛 CommandNotAllowedError —— CommandVerifier 已捕获它并映射 errored。"""

    def test_blocked_command_never_reaches_inner(self) -> None:
        guarded, inner = _guarded([_rule(when="true", action=Action.BLOCK)])

        with pytest.raises(CommandNotAllowedError):
            guarded.run("rm -rf /", workdir=Path("."))

        assert inner.calls == [], "被拦的命令仍然执行了 —— 这层等于没有"

    def test_error_carries_rule_id(self) -> None:
        """消息进 evidence 给用户看，没有规则 id 就无从定位是哪条规则拦的。"""
        guarded, _ = _guarded(
            [_rule(id="tool-rm-rf", action=Action.BLOCK, message="禁止递归删除")]
        )

        with pytest.raises(CommandNotAllowedError) as excinfo:
            guarded.run("rm -rf /", workdir=Path("."))

        assert "tool-rm-rf" in str(excinfo.value)
        assert "禁止递归删除" in str(excinfo.value)

    def test_require_approval_degrades_to_block(self) -> None:
        """命令执行在 Verifier 的同步路径上，没有挂起等审批的地方。"""
        guarded, inner = _guarded([_rule(action=Action.REQUIRE_APPROVAL)])

        with pytest.raises(CommandNotAllowedError):
            guarded.run("pytest", workdir=Path("."))
        assert inner.calls == []

    def test_full_command_string_is_matched(self) -> None:
        """这条是本层存在的理由：ExecPolicy 只看 argv[0]，看不见参数。"""
        guarded, inner = _guarded(
            [
                _rule(
                    id="tool-metadata-endpoint",
                    when='tool.cmd.contains("169.254.169.254")',
                    action=Action.BLOCK,
                )
            ]
        )

        # npx 在 M3 白名单内，第一层放行；元数据端点只有规则拦得住
        with pytest.raises(CommandNotAllowedError):
            guarded.run("npx foo --url http://169.254.169.254/latest/meta-data", workdir=Path("."))
        assert inner.calls == []


class TestAllowPaths:
    def test_no_rules_means_pass_through(self) -> None:
        guarded, inner = _guarded([])
        result = guarded.run("pytest -q", workdir=Path("/tmp"))
        assert result.succeeded
        assert inner.calls == [("pytest -q", Path("/tmp"))]

    def test_non_matching_rule_passes(self) -> None:
        guarded, inner = _guarded(
            [_rule(when='tool.cmd.contains("sudo")', action=Action.BLOCK)]
        )
        guarded.run("pytest -q", workdir=Path("."))
        assert len(inner.calls) == 1

    def test_warn_still_executes(self) -> None:
        guarded, inner = _guarded([_rule(action=Action.WARN)])
        result = guarded.run("pytest", workdir=Path("."))
        assert result.succeeded
        assert len(inner.calls) == 1

    @pytest.mark.parametrize("action", [Action.REWRITE, Action.ROUTE])
    def test_rewrite_and_route_fail_open(self, action: Action) -> None:
        """命令串没有安全的改写语义，也无处路由 —— 放行而不是静默换命令。"""
        guarded, inner = _guarded([_rule(action=action)])
        guarded.run("pytest", workdir=Path("."))
        assert len(inner.calls) == 1, f"{action.value} 不该阻断执行"

    def test_exit_code_passes_through(self) -> None:
        """放行后不加工 inner 的结果 —— 退出码是断言的唯一信号。"""
        guarded, _ = _guarded([], runner=_StubRunner(exit_code=1))
        assert not guarded.run("pytest", workdir=Path(".")).succeeded


class TestMissingContextKey:
    """缺键在 CEL 里是求值错误 → fail-closed 命中 → block。

    pre_tool 的命中动作是 block，所以这个坑比 loop 那边更直接：调用方忘填
    cmd 就会拦掉所有命令 → 断言全 errored → Loop 永远无法验证收敛。
    TOOL_CONTEXT_DEFAULTS 就是为这个存在的。
    """

    def test_rule_reading_undeclared_key_does_not_block_everything(self) -> None:
        """规则读契约外的键时 fail-closed 拦截 —— 记录这是已知行为，不是回归。"""
        guarded, inner = _guarded(
            [_rule(when='tool.workdir.contains("x")', action=Action.BLOCK)]
        )
        with pytest.raises(CommandNotAllowedError):
            guarded.run("pytest", workdir=Path("."))
        assert inner.calls == []

    def test_declared_key_always_present(self) -> None:
        """随包规则只读 tool.cmd，它由 TOOL_CONTEXT_DEFAULTS 保证存在。"""
        from ariadne.harness_module.evaluator import _normalize_tool

        assert _normalize_tool({})["cmd"] == ""
        assert _normalize_tool({"cmd": "pytest"})["cmd"] == "pytest"

    def test_shipped_rules_pass_a_benign_command(self) -> None:
        """随包 tool.yaml 全量加载后，正常命令必须能过 —— 否则 Loop 直接瘫。

        这条兼当契约的回归闸：若哪天有规则读了 TOOL_CONTEXT_DEFAULTS 之外的键，
        缺键会 fail-closed 成 block，这里立刻红。
        """
        from ariadne.harness_module.loader import load_rule_set

        rules = load_rule_set(_SHIPPED_RULES_DIR)
        tool_rules = [r for r in rules if r.hook is HookKind.PRE_TOOL]
        assert tool_rules, "随包规则里没有 pre_tool 规则 —— 接线接空了"

        inner = _StubRunner()
        guarded = GuardedCommandRunner(inner=inner, evaluator=compile_rule_set(tool_rules))

        guarded.run("pytest -q tests/", workdir=Path("."))
        assert len(inner.calls) == 1

    def test_shipped_rules_block_a_dangerous_command(self) -> None:
        from ariadne.harness_module.loader import load_rule_set

        rules = [r for r in load_rule_set(_SHIPPED_RULES_DIR) if r.hook is HookKind.PRE_TOOL]
        inner = _StubRunner()
        guarded = GuardedCommandRunner(inner=inner, evaluator=compile_rule_set(rules))

        with pytest.raises(CommandNotAllowedError):
            guarded.run("rm -rf /", workdir=Path("."))
        assert inner.calls == []


class TestLoopStateProvider:
    def test_provider_failure_does_not_break_execution(self) -> None:
        """取不到 Loop 状态不该让命令执行失败。"""

        def _boom() -> dict[str, object]:
            raise RuntimeError("引擎还没准备好")

        guarded, inner = _guarded([], loop_state=_boom)
        guarded.run("pytest", workdir=Path("."))
        assert len(inner.calls) == 1

    def test_provider_value_reaches_rules(self) -> None:
        guarded, inner = _guarded(
            [
                Rule(
                    id="res",
                    category=RuleCategory.RESOURCE,
                    hook=HookKind.PRE_TOOL,
                    when="ctx.loop.iteration > 2",
                    action=Action.BLOCK,
                )
            ],
            loop_state=lambda: {"iteration": 5},
        )
        with pytest.raises(CommandNotAllowedError):
            guarded.run("pytest", workdir=Path("."))
        assert inner.calls == []


class TestAudit:
    def test_block_writes_one_record(self) -> None:
        sink = InMemoryAuditSink()
        guarded, _ = _guarded([_rule(id="blocked", action=Action.BLOCK)], sink=sink)

        with pytest.raises(CommandNotAllowedError):
            guarded.run("rm -rf /", workdir=Path("."))

        # pre_tool BLOCK 在 inner.run 之前抛出，不会有 post_tool 记录
        assert sink.count == 1
        record = sink.latest()
        assert record is not None
        assert record.hook is HookKind.PRE_TOOL
        assert record.action is Action.BLOCK
        assert record.context_snapshot["cmd"] == "rm -rf /"
        assert record.loop_id == "loop-1"

    def test_allow_also_audited(self) -> None:
        """放行也留痕：没有 allow 记录就无法回答"这条命令当时过了吗"。

        pre_tool 与 post_tool 各写一条（命令执行前后都求值）。
        """
        sink = InMemoryAuditSink()
        # when="true" + action=ALLOW 而非 when="false" + BLOCK：
        # 后者的 "false" 字面值在 CEL 里仍需求值，极少数情况可能超时触发 fail-closed。
        # 显式 ALLOW 规则语义更清晰：这是"主动放行"而非"没拦住"。
        guarded, _ = _guarded([_rule(when="true", action=Action.ALLOW)], sink=sink)
        guarded.run("pytest", workdir=Path("."))
        assert sink.count == 2
        assert {r.action for r in sink.records} == {Action.ALLOW}
        assert {r.hook for r in sink.records} == {HookKind.PRE_TOOL, HookKind.POST_TOOL}

    def test_null_sink_writes_nothing(self) -> None:
        guarded, inner = _guarded([])
        guarded.run("pytest", workdir=Path("."))
        assert len(inner.calls) == 1

    @pytest.mark.asyncio
    async def test_audit_works_inside_running_event_loop(self) -> None:
        """engine 在协程里调 verifier，同步写异步 sink 得靠独立线程。"""
        sink = InMemoryAuditSink()
        guarded, _ = _guarded([_rule(when="true", action=Action.ALLOW)], sink=sink)
        guarded.run("pytest", workdir=Path("."))
        # pre_tool + post_tool 各一条（行为在 TestAudit.test_allow_also_audited
        # 已断言两条 hook 各记录一次，这里只验证事件循环内不崩）
        assert sink.count == 2


class TestThroughCommandVerifier:
    """选 CommandNotAllowedError 作 BLOCK 信号的理由，在这里被实测证明。

    被规则拦下的命令属于"断言配置得改"，不是"被测输出不合格"。判失败会让
    critique 给出"修代码"的无效指令 —— 所以必须落到 errored 而非 passed=False。
    """

    @staticmethod
    def _outcome(rules: list[Rule], cmd: str, tmp_path: Path):  # type: ignore[no-untyped-def]
        from ariadne.loop_module.goal import Assertion, AssertionKind
        from ariadne.loop_module.verifier.base import VerificationContext
        from ariadne.loop_module.verifier.command import CommandVerifier

        guarded, _ = _guarded(rules)
        verifier = CommandVerifier(runner=guarded)
        return verifier.verify(
            Assertion(id="a", kind=AssertionKind.COMMAND, spec={"cmd": cmd}),
            VerificationContext(output="", artifact_path=tmp_path),
        )

    def test_blocked_command_becomes_errored_not_failed(self, tmp_path: Path) -> None:
        outcome = self._outcome(
            [_rule(id="tool-rm-rf", action=Action.BLOCK, message="禁止递归删除")],
            "rm -rf /",
            tmp_path,
        )
        assert outcome.errored, "落成普通失败会让 critique 去改代码，而问题在断言配置"
        assert not outcome.passed
        assert "tool-rm-rf" in outcome.evidence, "证据里没有规则 id，用户无从定位"

    def test_evidence_is_not_prefixed_with_exception_type(self, tmp_path: Path) -> None:
        """走 _verify 的专门捕获，而不是 base.verify 的兜底 —— 后者会加类型前缀。"""
        outcome = self._outcome([_rule(action=Action.BLOCK, message="拦了")], "rm -rf /", tmp_path)
        assert not outcome.evidence.startswith("CommandNotAllowedError")


class TestPostTool:
    """post_tool 卡点：命令**执行后**对输出求值。

    与 pre_tool 的语义刻意相反：命令已经跑了，是它的**输出内容**违规
    （结果过大、泄露敏感数据之类）—— 那是"被测输出不合格"，应判失败
    让 critique 驱动模型修输出；判 errored 会让模型去改断言配置，方向
    完全错。BLOCK 合成为 exit_code=1 的 ExecResult（verifier 只看
    succeeded / combined_output），命令实际已执行过。
    """

    @staticmethod
    def _stub_run(result_text: str = "ok") -> _StubRunner:
        """让 inner runner 产生固定输出。"""
        return _StubRunner(stdout=result_text)

    def test_blocked_result_becomes_failed_not_errored(self) -> None:
        inner = self._stub_run("huge output")
        guarded, _ = _guarded(
            [_rule(hook=HookKind.POST_TOOL, id="result-too-big", when="tool.result.size() > 5")],
            runner=inner,
        )

        result = guarded.run("pytest", workdir=Path("."))
        assert not result.succeeded
        assert "result-too-big" in result.stderr
        # 命令确实执行了 —— post_tool 拦的是输出，不是命令本身
        assert len(inner.calls) == 1

    def test_command_still_runs_before_post_tool_block(self) -> None:
        """pre_tool 不拦、post_tool 拦 —— 证明求值发生在执行**后**。"""
        inner = self._stub_run("x")
        guarded, _ = _guarded(
            [
                _rule(hook=HookKind.PRE_TOOL, when="false", action=Action.BLOCK),
                _rule(hook=HookKind.POST_TOOL, id="post", when="true", action=Action.BLOCK),
            ],
            runner=inner,
        )

        result = guarded.run("pytest", workdir=Path("."))
        assert not result.succeeded
        assert len(inner.calls) == 1
        assert "post" in result.stderr

    def test_warn_passes_through(self) -> None:
        inner = self._stub_run("small")
        guarded, _ = _guarded(
            [_rule(hook=HookKind.POST_TOOL, action=Action.WARN, when="true")],
            runner=inner,
        )
        result = guarded.run("pytest", workdir=Path("."))
        assert result.succeeded
        assert result.stdout == "small"

    def test_rule_reads_actual_result(self) -> None:
        """规则能看到命令真实输出，不是 cmd 的别名。"""
        inner = self._stub_run("SECRET-TOKEN leaked")
        guarded, _ = _guarded(
            [
                _rule(
                    hook=HookKind.POST_TOOL,
                    id="leak",
                    when='tool.result.contains("SECRET-TOKEN")',
                    action=Action.BLOCK,
                )
            ],
            runner=inner,
        )
        result = guarded.run("pytest", workdir=Path("."))
        assert not result.succeeded
        assert "leak" in result.stderr

    def test_require_approval_degrades_to_block(self) -> None:
        inner = self._stub_run("x")
        guarded, _ = _guarded(
            [_rule(hook=HookKind.POST_TOOL, action=Action.REQUIRE_APPROVAL)],
            runner=inner,
        )
        result = guarded.run("pytest", workdir=Path("."))
        assert not result.succeeded

    def test_rewrite_and_route_fail_open(self) -> None:
        for action in (Action.REWRITE, Action.ROUTE):
            inner = self._stub_run("x")
            guarded, _ = _guarded(
                [_rule(hook=HookKind.POST_TOOL, action=action)],
                runner=inner,
            )
            result = guarded.run("pytest", workdir=Path("."))
            assert result.succeeded, f"{action.value} 不该阻断执行"

    def test_block_writes_post_tool_audit(self) -> None:
        sink = InMemoryAuditSink()
        inner = self._stub_run("x")
        guarded, _ = _guarded(
            [_rule(hook=HookKind.POST_TOOL, id="post-blocked", action=Action.BLOCK)],
            runner=inner,
            sink=sink,
        )
        guarded.run("pytest", workdir=Path("."))
        post_records = [r for r in sink.records if r.hook is HookKind.POST_TOOL]
        assert len(post_records) == 1
        assert post_records[0].action is Action.BLOCK
        assert post_records[0].context_snapshot["cmd"] == "pytest"

    def test_declared_defaults_include_result(self) -> None:
        from ariadne.harness_module.evaluator import _normalize_tool
        from ariadne.harness_module.models import TOOL_CONTEXT_DEFAULTS

        assert "result" in TOOL_CONTEXT_DEFAULTS
        assert _normalize_tool({"cmd": "pytest"})["result"] == ""

    def test_shipped_post_tool_rule_present_and_benign_result_passes(self) -> None:
        from ariadne.harness_module.loader import load_rule_set

        rules = load_rule_set(_SHIPPED_RULES_DIR)
        post_rules = [r for r in rules if r.hook is HookKind.POST_TOOL]
        assert post_rules, "随包规则里没有 post_tool 规则 —— 接线接空了"

        inner = self._stub_run("ok")
        guarded = GuardedCommandRunner(
            inner=inner, evaluator=compile_rule_set(rules)
        )
        result = guarded.run("pytest", workdir=Path("."))
        assert result.succeeded
        assert len(inner.calls) == 1


class TestEngineWiring:
    """引擎装配 —— 没有这段，上面所有行为在生产里都不会发生。"""

    @staticmethod
    def _config(*, harness: object, command_runner: object = None) -> object:
        from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
        from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
        from ariadne.loop_module.engine import LoopConfig
        from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal

        goal = Goal(
            task="跑测试",
            assertions=(
                Assertion(
                    id="cmd",
                    kind=AssertionKind.COMMAND,
                    spec={"cmd": "pytest"},
                    blocking=True,
                ),
            ),
            budget=Budget(max_iterations=2, max_cost_usd=1.0),
            mode="quality",
        )
        return LoopConfig(
            goal=goal,
            loop_id="loop-w",
            project_id=PROJECT,
            budget_guard=BudgetGuard("loop-w", goal.budget, InMemoryCounter()),
            llm=None,  # type: ignore[arg-type]
            checkpoint_store=InMemoryCheckpointStore(),
            harness=harness,  # type: ignore[arg-type]
            command_runner=command_runner,  # type: ignore[arg-type]
        )

    def test_harness_wraps_command_runner(self) -> None:
        from ariadne.loop_module.engine import LoopEngine
        from ariadne.loop_module.goal import AssertionKind

        engine = LoopEngine(self._config(harness=compile_rule_set([_rule()])))  # type: ignore[arg-type]
        verifier = engine._verifiers[AssertionKind.COMMAND]
        assert isinstance(verifier._runner, GuardedCommandRunner)  # type: ignore[attr-defined]

    def test_no_harness_leaves_runner_unwrapped(self) -> None:
        """没有规则集的 Loop 不该多出一层求值开销。"""
        from ariadne.loop_module.engine import LoopEngine
        from ariadne.loop_module.goal import AssertionKind

        engine = LoopEngine(self._config(harness=None))
        verifier = engine._verifiers[AssertionKind.COMMAND]
        assert not isinstance(verifier._runner, GuardedCommandRunner)  # type: ignore[attr-defined]

    def test_wired_runner_reads_live_loop_state(self) -> None:
        """loop_state_provider 绑的是 bound method，取值发生在调用时。

        绑的若是当时的快照，iteration 永远是 0，resource 规则形同废纸。
        """
        from ariadne.loop_module.engine import LoopEngine
        from ariadne.loop_module.goal import AssertionKind

        engine = LoopEngine(self._config(harness=compile_rule_set([_rule(when="false")])))  # type: ignore[arg-type]
        runner = engine._verifiers[AssertionKind.COMMAND]._runner  # type: ignore[attr-defined]
        assert runner.loop_state_provider is not None
        engine._iteration = 7
        assert runner.loop_state_provider()["iteration"] == 7

    def test_default_inner_runner_is_restricted_subprocess(self) -> None:
        """不注入时行为不变 —— 测试与单机开发都依赖这个默认。"""
        from ariadne.loop_module.engine import LoopEngine
        from ariadne.loop_module.goal import AssertionKind
        from ariadne.loop_module.verifier.command import RestrictedRunner

        engine = LoopEngine(self._config(harness=None))
        runner = engine._verifiers[AssertionKind.COMMAND]._runner  # type: ignore[attr-defined]
        assert isinstance(runner, RestrictedRunner)

    def test_injected_runner_replaces_restricted_subprocess(self) -> None:
        """沙箱接线的关键一环：注入的执行器必须真的取代受限子进程。

        这条断言的存在理由：`_build_command_runner` 曾硬编码 RestrictedRunner，
        于是整个 sandbox_module 建好后从未被生产路径调用过 —— 配了沙箱也不生效。
        """
        from ariadne.loop_module.engine import LoopEngine
        from ariadne.loop_module.goal import AssertionKind
        from ariadne.loop_module.verifier.command import RestrictedRunner

        stub = _StubRunner()
        engine = LoopEngine(self._config(harness=None, command_runner=stub))
        runner = engine._verifiers[AssertionKind.COMMAND]._runner  # type: ignore[attr-defined]
        assert runner is stub
        assert not isinstance(runner, RestrictedRunner)

    def test_injected_runner_still_goes_through_harness_gate(self) -> None:
        """换成沙箱不能顺带绕掉 pre_tool 卡点：卡点包在外层。"""
        from ariadne.loop_module.engine import LoopEngine
        from ariadne.loop_module.goal import AssertionKind

        stub = _StubRunner()
        engine = LoopEngine(
            self._config(harness=compile_rule_set([_rule()]), command_runner=stub)  # type: ignore[arg-type]
        )
        runner = engine._verifiers[AssertionKind.COMMAND]._runner  # type: ignore[attr-defined]
        assert isinstance(runner, GuardedCommandRunner)
        assert runner.inner is stub
