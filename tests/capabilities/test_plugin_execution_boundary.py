"""阶段 4 收口：**插件执行边界**（归类 / 强制终止 / Worker 接线 / 诚实四级状态）。

每个用例问的是同一个问题："这次执行到底算谁的、配额到底执行到哪一步？"

= ==========================  ==================================================
用例                          钉住的事实
= ==========================  ==================================================
归类                          只有 ``plugin_id`` + Manifest + ``quota_spec`` 三件齐备才算插件；
                              否则显式 ``builtin``（系统默认上限），永不被插件配额治理
无 Worker                     ``PLUGIN_WORKER_UNAVAILABLE``：绝不放回进程内无配额执行
只有协作式                    需要强杀却拿不到 → ``PLUGIN_QUOTA_NOT_ENFORCED``（拒绝，不假装跑过）
真实调用点                    ``python_exec`` 的插件执行真经 ``PluginManager.worker_for``
四级状态                      declared / observed / wired / enforced；协作式**不算** enforced
开关关闭                      与改造前逐字节一致（不取 Worker、不判定、不阻断）
= ==========================  ==================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import shutil
import sys

import pytest

from lumi_contracts.events.errors import spec_for
from lumi_contracts.plugins import PluginManifest
from lumi_contracts.plugins import execution as execution_contract
from lumi_contracts.plugins.vocabulary import IsolationLevel, TrustLevel
from lumi_contracts.plugins.execution import (
    COOPERATIVE_TERMINATION_REASONS,
    FORCED_TERMINATION_REASONS,
    PLUGIN_QUOTA_NOT_ENFORCED,
    PLUGIN_WORKER_UNAVAILABLE,
    SYSTEM_DEFAULT_LIMITS,
    ExecutionClass,
    TerminationMode,
    TerminationReason,
    classify_execution,
    decide_termination,
    forced_termination_reasons,
    quota_status_levels,
)

from app.agents.sandbox.base import SandboxResult
from app.agents.skills.base import SkillContext
from app.core.config import settings
from app.plugins import PluginManager, PluginRegistry, PluginStateStore
from app.plugins import boundary as boundary_mod
from app.plugins.boundary import (
    blocked_error,
    evidence_for,
    plugin_quota_status,
    record_execution,
    record_wired,
    reset_quota_evidence,
    resolve_execution,
)
from app.plugins.manager import FLAG
from app.plugins.quota import (
    DEFAULT_WALL_SECONDS,
    PluginQuotaSpec,
    PluginWorker,
)

from _paths import REPO_ROOT
_CASE = itertools.count(1)
PLUGIN_ID = "lumi.batch_runner"


# ── 测试脚手架 ───────────────────────────────────────────


def _manifest(**overrides) -> PluginManifest:
    payload = {
        "id": PLUGIN_ID,
        "version": "3.2.1",
        "kind": "skill_plugin",
        "deployment": "server",
        "data_locality": "cloud",
        "isolation": "sandboxed",
        "trust_level": "official",
        "provides": {"capabilities": ["code.execute@1"]},
    }
    payload.update(overrides)
    return PluginManifest(**payload)


def _spec(*, hard: bool = True, wall_seconds: int = 5, **extra) -> PluginQuotaSpec:
    from lumi_contracts.plugins.lifecycle import PluginQuota

    return PluginQuotaSpec(
        plugin_id=PLUGIN_ID,
        plugin_version="3.2.1",
        quota=PluginQuota(
            timeout_seconds=int(wall_seconds),
            max_output_bytes=int(extra.get("max_output_bytes", 100_000)),
            memory_mb=int(extra.get("memory_mb", 128)),
        ),
        cpu_seconds=float(extra.get("cpu_seconds", 1.0)),
        max_concurrency=int(extra.get("max_concurrency", 1)),
        hard_limits=hard,
    )


@contextlib.contextmanager
def plugin_env(*, flag: bool = True, install: bool = True, **manifest_overrides):
    """独立状态目录 + 真实插件管理器（可选装好一个插件）。"""
    directory = (
        REPO_ROOT
        / ".ptmp"
        / f"plugin_boundary_{os.getpid()}_{next(_CASE)}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, FLAG, flag)
    reset_quota_evidence()
    try:
        store = PluginStateStore(state_dir=directory)
        registry = PluginRegistry(store=store)
        manager = PluginManager(registry=registry, state_store=store)
        if install:
            asyncio.run(manager.install(_manifest(**manifest_overrides)))
        yield manager
    finally:
        reset_quota_evidence()
        monkey.undo()
        shutil.rmtree(directory, ignore_errors=True)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _quota_flag_on(monkeypatch):
    """默认打开灰度开关（本文件的主题就是"开关打开时边界真的生效"）。

    开关关闭的等价性用例用 ``plugin_env(flag=False)`` / ``monkeypatch`` 显式覆盖。
    """
    monkeypatch.setattr(settings, FLAG, True)
    reset_quota_evidence()
    yield
    reset_quota_evidence()


# ── (1) 唯一的归类函数（纯函数） ──────────────────────────


def test_classifier_requires_plugin_id_manifest_and_quota_spec():
    spec = _spec()
    manifest = _manifest()
    plugin = classify_execution(plugin_id=PLUGIN_ID, manifest=manifest, quota_spec=spec)
    assert plugin.kind == ExecutionClass.PLUGIN.value
    assert plugin.plugin_governed is True
    assert plugin.missing == ()
    assert plugin.limits["governed_by"] == "plugin_manifest"
    assert plugin.limits["wall_seconds"] == 5.0 and plugin.limits["hard_limits"] is True

    for kwargs in (
        {"manifest": manifest, "quota_spec": spec},                      # 缺 plugin_id
        {"plugin_id": PLUGIN_ID, "quota_spec": spec},                    # 缺 manifest
        {"plugin_id": PLUGIN_ID, "manifest": manifest},                  # 缺 quota_spec
        {"plugin_id": PLUGIN_ID},                                        # 只剩身份
        {},                                                              # 什么都没有
    ):
        builtin = classify_execution(**kwargs)
        assert builtin.kind == ExecutionClass.BUILTIN.value, kwargs
        assert builtin.plugin_governed is False, kwargs
        assert builtin.missing, kwargs


def test_builtin_execution_is_never_plugin_quota_governed():
    builtin = classify_execution()
    assert builtin.system_default is True
    assert builtin.limits == SYSTEM_DEFAULT_LIMITS
    assert builtin.limits["hard_limits"] is False
    assert builtin.limits["max_concurrency"] == 0, "内置执行不进插件并发门"
    # 即便带了插件身份但边界不齐 → 仍是 builtin，且被标为"无归属插件"
    unattributed = classify_execution(plugin_id=PLUGIN_ID)
    assert unattributed.kind == ExecutionClass.BUILTIN.value
    assert unattributed.unattributed_plugin is True
    assert unattributed.limits == SYSTEM_DEFAULT_LIMITS


def test_classifier_flags_mismatched_quota_spec_as_unattributed():
    other = _spec()
    mismatched = PluginQuotaSpec(
        plugin_id="someone.else",
        plugin_version="1.0.0",
        quota=other.quota,
        hard_limits=True,
    )
    result = classify_execution(
        plugin_id=PLUGIN_ID, manifest=_manifest(), quota_spec=mismatched
    )
    assert result.kind == ExecutionClass.BUILTIN.value
    assert result.mismatched_spec is True
    assert result.unattributed_plugin is True
    assert "quota_spec" in result.missing


def test_system_default_limits_agree_with_the_execution_boundary_defaults():
    assert SYSTEM_DEFAULT_LIMITS["wall_seconds"] == DEFAULT_WALL_SECONDS
    assert SYSTEM_DEFAULT_LIMITS["memory_mb"] == 512
    assert SYSTEM_DEFAULT_LIMITS["cpu_seconds"] == 60.0
    assert SYSTEM_DEFAULT_LIMITS["governed_by"] == "system_default"


# ── (2) 终止方式：cooperative / forced / refused（纯函数） ──


def test_cooperative_reasons_are_only_notify_cancel_and_user_stop():
    assert COOPERATIVE_TERMINATION_REASONS == {
        TerminationReason.TIMEOUT_NOTIFY.value,
        TerminationReason.CANCEL.value,
        TerminationReason.USER_STOP.value,
    }
    assert FORCED_TERMINATION_REASONS == {
        TerminationReason.CPU_LIMIT.value,
        TerminationReason.MEMORY_LIMIT.value,
        TerminationReason.RUNAWAY_LOOP.value,
        TerminationReason.RUNAWAY_CHILD_PROCESS.value,
    }
    execution = classify_execution(plugin_id=PLUGIN_ID, manifest=_manifest(), quota_spec=_spec())
    for reason in sorted(COOPERATIVE_TERMINATION_REASONS):
        decision = decide_termination(execution=execution, reason=reason)
        assert decision.mode == TerminationMode.COOPERATIVE.value, reason
        assert decision.error_code == ""
        assert decision.hard_termination_required is False


def test_forced_reasons_demand_a_killable_process_for_plugin_executions():
    execution = classify_execution(plugin_id=PLUGIN_ID, manifest=_manifest(), quota_spec=_spec())
    for reason in sorted(FORCED_TERMINATION_REASONS):
        decision = decide_termination(execution=execution, reason=reason)
        assert decision.mode == TerminationMode.FORCED.value, reason
        assert decision.hard_termination_required is True


def test_plugin_hard_limits_default_to_forced_termination():
    execution = classify_execution(plugin_id=PLUGIN_ID, manifest=_manifest(), quota_spec=_spec())
    reasons = forced_termination_reasons(execution)
    assert set(reasons) == {
        TerminationReason.CPU_LIMIT.value,
        TerminationReason.MEMORY_LIMIT.value,
        TerminationReason.RUNAWAY_LOOP.value,
    }
    decision = decide_termination(execution=execution)  # 不给 reason → 取最严的
    assert decision.mode == TerminationMode.FORCED.value
    assert decision.reason == TerminationReason.RUNAWAY_LOOP.value
    # 软约束（内置插件）不需要强杀：时间到了只 WARN
    soft = classify_execution(
        plugin_id=PLUGIN_ID, manifest=_manifest(), quota_spec=_spec(hard=False)
    )
    assert forced_termination_reasons(soft) == ()
    assert decide_termination(execution=soft).mode == TerminationMode.COOPERATIVE.value


def test_refuses_when_forced_termination_is_needed_but_only_cooperative_is_available():
    execution = classify_execution(plugin_id=PLUGIN_ID, manifest=_manifest(), quota_spec=_spec())
    decision = decide_termination(
        execution=execution,
        reason=TerminationReason.RUNAWAY_CHILD_PROCESS.value,
        worker_available=True,
        forced_available=False,
    )
    assert decision.mode == TerminationMode.REFUSED.value
    assert decision.error_code == PLUGIN_QUOTA_NOT_ENFORCED
    assert decision.forced_available is False and decision.hard_termination_required is True

    no_worker = decide_termination(
        execution=execution,
        reason=TerminationReason.CPU_LIMIT.value,
        worker_available=False,
        forced_available=False,
    )
    assert no_worker.mode == TerminationMode.REFUSED.value
    assert no_worker.error_code == PLUGIN_WORKER_UNAVAILABLE


def test_unattributed_quota_claim_is_refused_not_run_as_builtin():
    execution = classify_execution(manifest=_manifest(), quota_spec=_spec())
    decision = decide_termination(execution=execution)
    assert decision.mode == TerminationMode.REFUSED.value
    assert decision.error_code == PLUGIN_QUOTA_NOT_ENFORCED
    # 真内置（无任何插件声明）永远是协作式，不受插件配额治理
    builtin = classify_execution()
    assert decide_termination(execution=builtin).mode == TerminationMode.COOPERATIVE.value


# ── (3) 无 Worker → 稳定阻断码 ────────────────────────────


def test_no_worker_blocks_with_plugin_worker_unavailable():
    plan = _run(
        resolve_execution(
            manager=None,
            plugin_id=PLUGIN_ID,
            manifest=_manifest(),
            quota_spec=_spec(),
        )
    )
    assert plan.refused is True
    assert plan.error_code == PLUGIN_WORKER_UNAVAILABLE
    assert plan.quota_spec is not None, "声明不会被丢掉，只是不能执行"
    assert plan.safe_message and plan.safe_message == spec_for(PLUGIN_WORKER_UNAVAILABLE).safe_message
    assert plan.worker is None


def test_uninstalled_plugin_blocks_with_plugin_worker_unavailable():
    with plugin_env(install=False) as manager:
        plan = _run(resolve_execution(manager=manager, plugin_id=PLUGIN_ID))
    assert plan.refused is True
    assert plan.error_code == PLUGIN_WORKER_UNAVAILABLE


def test_worker_for_failure_blocks_instead_of_falling_back(monkeypatch):
    with plugin_env() as manager:
        def _boom(*args, **kwargs):  # pragma: no cover - 只在被调用时触发
            raise RuntimeError("worker 进程池不可用")

        monkeypatch.setattr(manager, "worker_for", _boom)
        plan = _run(resolve_execution(manager=manager, plugin_id=PLUGIN_ID))
        assert evidence_for(PLUGIN_ID).refused >= 1, "拒绝也是边界生效的证据"
    assert plan.refused is True
    assert plan.error_code == PLUGIN_WORKER_UNAVAILABLE


def test_blocked_plan_becomes_a_plugin_rejected_with_the_stable_code():
    plan = _run(resolve_execution(manager=None, plugin_id=PLUGIN_ID))
    error = blocked_error(plan)
    assert error.code == PLUGIN_WORKER_UNAVAILABLE
    assert error.message == plan.safe_message
    assert error.details["refused"] is True


# ── (4) 插件执行真经 worker_for ───────────────────────────


def test_plugin_execution_goes_through_worker_for(monkeypatch):
    limits = {"wall_seconds": 7.0, "cpu_seconds": 3.0, "memory_mb": 96, "max_concurrency": 2}
    with plugin_env(resource_limits=limits) as manager:
        calls: list[tuple] = []
        original = manager.worker_for

        def _spy(plugin_id, **kwargs):
            calls.append((plugin_id, kwargs))
            return original(plugin_id, **kwargs)

        monkeypatch.setattr(manager, "worker_for", _spy)
        plan = _run(resolve_execution(manager=manager, plugin_id=PLUGIN_ID))

        assert calls and calls[0][0] == PLUGIN_ID, "必须经 PluginManager.worker_for 取边界"
        assert plan.refused is False
        assert plan.plugin_governed is True and plan.forced is True
        assert plan.quota_spec is not None
        assert plan.quota_spec.plugin_id == PLUGIN_ID
        assert plan.quota_spec.quota.timeout_seconds == 7
        evidence = evidence_for(PLUGIN_ID)
        assert evidence.wired == 1


def test_builtin_execution_never_consults_worker_for(monkeypatch):
    with plugin_env() as manager:
        monkeypatch.setattr(
            manager, "worker_for", lambda *a, **k: pytest.fail("内置执行不得取插件 Worker")
        )
        plan = _run(resolve_execution(manager=manager))
    assert plan.refused is False
    assert plan.plugin_governed is False
    assert plan.quota_spec is None
    assert plan.mode == TerminationMode.COOPERATIVE.value


def test_unattributed_quota_claim_is_blocked_at_the_boundary():
    plan = _run(resolve_execution(manifest=_manifest(), quota_spec=_spec()))
    assert plan.refused is True
    assert plan.error_code == PLUGIN_QUOTA_NOT_ENFORCED
    assert plan.termination.hard_termination_required is True


def test_forced_needed_but_unavailable_is_refused_with_not_enforced():
    with plugin_env() as manager:
        plan = _run(
            resolve_execution(manager=manager, plugin_id=PLUGIN_ID, forced_available=False)
        )
    assert plan.refused is True
    assert plan.error_code == PLUGIN_QUOTA_NOT_ENFORCED
    assert plan.termination.forced_available is False
    assert plan.worker is not None, "Worker 拿到了，只是它不能强杀"


def test_cooperative_reason_is_allowed_for_a_plugin_execution():
    with plugin_env() as manager:
        plan = _run(
            resolve_execution(
                manager=manager,
                plugin_id=PLUGIN_ID,
                termination=TerminationReason.USER_STOP.value,
            )
        )
    assert plan.refused is False
    assert plan.mode == TerminationMode.COOPERATIVE.value


# ── (5) 诚实四级状态 ─────────────────────────────────────


def test_status_levels_helper_never_claims_enforced_for_cooperative_only():
    levels = quota_status_levels(
        declared=True, observed=True, wired=True, enforced=True, cooperative_only=True
    )
    assert levels["enforced"] is False
    assert levels["level"] == "wired"
    assert "cooperative_only" in levels["not_enforced_because"]
    full = quota_status_levels(declared=True, observed=True, wired=True, enforced=True)
    assert full["enforced"] is True and full["level"] == "enforced"
    only_declared = quota_status_levels(declared=True)
    assert only_declared["level"] == "declared"
    assert only_declared["not_enforced_because"] == ["no_enforced_execution"]


def test_status_is_declared_before_any_real_execution():
    with plugin_env() as manager:
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert status["levels"]["declared"] is True
    assert status["levels"]["observed"] is False
    assert status["levels"]["wired"] is False
    assert status["levels"]["enforced"] is False
    assert status["level"] == "declared"
    assert "no_wired_execution" in status["levels"]["not_enforced_because"]
    assert status["limits"]["plugin_id"] == PLUGIN_ID
    assert status["enforcement"]["forced_path_available"] is True


def test_status_is_observed_after_a_cooperative_execution_but_not_enforced():
    with plugin_env() as manager:
        record_execution(PLUGIN_ID, mode=TerminationMode.COOPERATIVE.value, output_bytes=12)
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert status["levels"]["observed"] is True
    assert status["levels"]["wired"] is False
    assert status["levels"]["enforced"] is False
    assert status["level"] == "observed"


def test_status_is_wired_but_not_enforced_when_only_cooperative_runs_happened():
    with plugin_env() as manager:
        record_wired(PLUGIN_ID, mode=TerminationMode.COOPERATIVE.value, quota_spec=_spec())
        record_execution(PLUGIN_ID, mode=TerminationMode.COOPERATIVE.value, output_bytes=1)
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert status["levels"]["wired"] is True
    assert status["levels"]["cooperative_only"] is True
    assert status["levels"]["enforced"] is False
    assert status["level"] == "wired"
    assert "cooperative_only" in status["enforcement"]["not_enforced_because"]


def test_status_is_enforced_only_after_a_real_killable_process_run():
    with plugin_env(resource_limits={"wall_seconds": 10.0, "cpu_seconds": 2.0, "memory_mb": 512}) as manager:
        plan = _run(resolve_execution(manager=manager, plugin_id=PLUGIN_ID))
        assert plan.forced is True
        outcome = _run(
            plan.worker.run_process([sys.executable, "-c", "print('enforced-plugin')"])
        )
        assert outcome.ok is True and "enforced-plugin" in outcome.content
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert status["levels"] == {
        "level": "enforced",
        "declared": True,
        "observed": True,
        "wired": True,
        "enforced": True,
        "cooperative_only": False,
        "not_enforced_because": [],
    }
    assert status["level"] == "enforced"
    assert status["evidence"]["forced"] == 1 and status["evidence"]["cooperative"] == 0


def test_builtin_soft_limits_are_never_reported_as_enforced():
    with plugin_env() as manager:
        # 未验签的插件在安装期会被降级为 third_party（不能自称 builtin）；
        # 这里直接把已安装记录改成 builtin，验证**软约束**永远不会被报成 enforced。
        installation = manager.get(PLUGIN_ID)
        installation.manifest = installation.manifest.model_copy(
            update={"trust_level": TrustLevel.BUILTIN, "isolation": IsolationLevel.IN_PROCESS}
        )
        record_wired(PLUGIN_ID, mode=TerminationMode.COOPERATIVE.value)
        record_execution(PLUGIN_ID, mode=TerminationMode.COOPERATIVE.value)
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert status["limits"]["hard_limits"] is False
    assert status["enforcement"]["hard_limits"] is False
    assert status["enforcement"]["forced_path_available"] is False
    assert status["levels"]["enforced"] is False
    assert "builtin_soft_limits" in status["enforcement"]["not_enforced_because"]


def test_status_flag_off_is_honest_and_never_enforced():
    with plugin_env(flag=False) as manager:
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert status["flag_enabled"] is False
    assert status["levels"]["enforced"] is False
    assert "flag_off" in status["enforcement"]["not_enforced_because"]


# ── (6) 进程内协作取消只允许三个原因 ──────────────────────


def test_in_process_run_refuses_forced_reasons_and_keeps_cooperative_ones(monkeypatch):
    from lumi_contracts.plugins import lifecycle as plugin_lifecycle

    monkeypatch.setattr(settings, FLAG, True)
    reset_quota_evidence()
    worker = PluginWorker(spec=_spec(), settings=settings)

    async def body() -> str:
        return "cooperative-ok"

    refused = _run(worker.run(body, reason=TerminationReason.RUNAWAY_LOOP.value))
    assert refused.ok is False
    assert refused.action == plugin_lifecycle.QuotaAction.REFUSED.value
    assert refused.error_code == PLUGIN_QUOTA_NOT_ENFORCED
    assert refused.blocked is True, "被拒绝的执行必须算阻断（不许当成功）"
    assert refused.content == ""
    assert refused.decision["mode"] == TerminationMode.REFUSED.value

    allowed = _run(worker.run(body, reason=TerminationReason.CANCEL.value))
    assert allowed.ok is True and allowed.content == "cooperative-ok"
    assert allowed.action == plugin_lifecycle.QuotaAction.NONE.value
    assert evidence_for(PLUGIN_ID).cooperative >= 1


# ── (7) 开关关闭 = 改造前 ────────────────────────────────


def test_flag_off_is_passthrough_and_never_consults_worker_for(monkeypatch):
    with plugin_env(flag=False) as manager:
        monkeypatch.setattr(
            manager, "worker_for", lambda *a, **k: pytest.fail("开关关闭时不得取插件 Worker")
        )
        plan = _run(resolve_execution(manager=manager, plugin_id=PLUGIN_ID))
    assert plan.passthrough is True
    assert plan.refused is False
    assert plan.quota_spec is None, "开关关闭时不把配额传给沙箱（逐字节一致）"
    assert evidence_for(PLUGIN_ID).wired == 0


def test_flag_off_keeps_the_legacy_sandbox_call_at_the_python_exec_call_site(monkeypatch, tmp_path):
    from plugins.tools.shell import python_exec as python_exec_mod

    class _FakeSandbox:
        name = "docker"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run_script(self, code, **kwargs):
            self.calls.append(kwargs)
            return SandboxResult(status="success", stdout="legacy-ok")

    fake = _FakeSandbox()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(python_exec_mod, "get_sandbox", lambda: fake)
    with plugin_env(flag=False) as manager:
        monkeypatch.setattr(
            "app.plugins.manager.active_plugin_manager", lambda: manager
        )
        result = _run(
            python_exec_mod.PythonExecSkill().execute(
                {"code": "print('legacy')"},
                SkillContext(user_id="u1", conversation_id="c1", plugin_id=PLUGIN_ID),
            )
        )
    assert result.success is True
    assert fake.calls and fake.calls[0].get("quota_spec") is None


def test_flag_on_passes_the_worker_quota_to_the_sandbox(monkeypatch, tmp_path):
    from plugins.tools.shell import python_exec as python_exec_mod

    class _FakeSandbox:
        name = "docker"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run_script(self, code, **kwargs):
            self.calls.append(kwargs)
            return SandboxResult(status="success", stdout="ok")

    fake = _FakeSandbox()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(python_exec_mod, "get_sandbox", lambda: fake)
    with plugin_env(resource_limits={"wall_seconds": 9.0, "memory_mb": 128}) as manager:
        monkeypatch.setattr(
            "app.plugins.manager.active_plugin_manager", lambda: manager
        )
        result = _run(
            python_exec_mod.PythonExecSkill().execute(
                {"code": "print('plugin')"},
                SkillContext(user_id="u1", conversation_id="c1", plugin_id=PLUGIN_ID),
            )
        )
        status = plugin_quota_status(manager, PLUGIN_ID)
    assert result.success is True
    spec = fake.calls[0]["quota_spec"]
    assert spec is not None and spec.plugin_id == PLUGIN_ID
    assert spec.quota.timeout_seconds == 9
    assert fake.calls[0]["owner"] == "u1"
    assert status["levels"]["wired"] is True


def test_flag_on_without_a_worker_fails_with_the_blocking_code(monkeypatch, tmp_path):
    from plugins.tools.shell import python_exec as python_exec_mod

    class _FakeSandbox:
        name = "docker"

        async def run_script(self, code, **kwargs):  # pragma: no cover - 不该被调用
            pytest.fail("没有 Worker 时绝不允许回退到沙箱执行")

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(python_exec_mod, "get_sandbox", lambda: _FakeSandbox())
    with plugin_env(flag=True):
        monkeypatch.setattr(
            "app.plugins.manager.active_plugin_manager", lambda: None
        )
        result = _run(
            python_exec_mod.PythonExecSkill().execute(
                {"code": "print('nope')"},
                SkillContext(user_id="u1", conversation_id="c1", plugin_id=PLUGIN_ID),
            )
        )
    assert result.success is False
    assert result.error_code == PLUGIN_WORKER_UNAVAILABLE
    assert result.metadata["quota"]["refused"] is True


def test_plugin_execution_really_kills_an_over_limit_script(monkeypatch, tmp_path):
    """端到端：插件执行真经 worker_for → Worker → 子进程 → 到点真杀。"""
    import time

    from app.agents.sandbox.local import LocalSandbox
    from plugins.tools.shell import python_exec as python_exec_mod

    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "AGENT_ALLOW_UNSAFE_LOCAL_SANDBOX", True)
    monkeypatch.setattr(python_exec_mod, "get_sandbox", lambda: LocalSandbox())
    with plugin_env(
        resource_limits={"wall_seconds": 1.0, "cpu_seconds": 5.0, "memory_mb": 512}
    ) as manager:
        monkeypatch.setattr(
            "app.plugins.manager.active_plugin_manager", lambda: manager
        )
        started = time.monotonic()
        result = _run(
            python_exec_mod.PythonExecSkill().execute(
                {"code": "import time; time.sleep(30)", "timeout": 30},
                SkillContext(user_id="u1", conversation_id="c1", plugin_id=PLUGIN_ID),
            )
        )
        elapsed = time.monotonic() - started
        status = plugin_quota_status(manager, PLUGIN_ID)

    assert result.success is False
    assert result.error_code == "TIMEOUT", "配额墙钟（Manifest 声明）必须压过调用方 timeout"
    assert elapsed < 10, f"必须到点就杀，而不是等脚本跑完（用了 {elapsed:.1f}s）"
    assert status["levels"]["enforced"] is True
    assert status["level"] == "enforced"


def test_builtin_python_exec_is_untouched_even_with_the_flag_on(monkeypatch, tmp_path):
    from plugins.tools.shell import python_exec as python_exec_mod

    class _FakeSandbox:
        name = "docker"

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run_script(self, code, **kwargs):
            self.calls.append(kwargs)
            return SandboxResult(status="success", stdout="builtin-ok")

    fake = _FakeSandbox()
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(python_exec_mod, "get_sandbox", lambda: fake)
    with plugin_env() as manager:
        monkeypatch.setattr(
            "app.plugins.manager.active_plugin_manager", lambda: manager
        )
        result = _run(
            python_exec_mod.PythonExecSkill().execute(
                {"code": "print('builtin')"}, SkillContext(user_id="u1", conversation_id="c1")
            )
        )
    assert result.success is True
    assert fake.calls[0].get("quota_spec") is None, "内置执行绝不带插件配额"
    assert evidence_for(PLUGIN_ID).wired == 0


# ── (8) API：结构化四级状态 ──────────────────────────────


def test_plugins_api_exposes_structured_quota_status(monkeypatch):
    from app.api.v1 import plugins as plugins_api

    with plugin_env() as manager:
        monkeypatch.setattr(plugins_api, "plugin_manager", manager)
        listed = _run(plugins_api.list_plugins(payload={}))
        detail = _run(plugins_api.plugin_quota_status(PLUGIN_ID, payload={}))

    entry = next(item for item in listed["data"]["plugins"] if item["plugin_id"] == PLUGIN_ID)
    status = entry["quota_status"]
    assert status["level"] == "declared"
    assert set(status["levels"]) >= {"declared", "observed", "wired", "enforced", "level"}
    assert status["level_meaning"]["enforced"].startswith("开关打开")
    assert detail["data"]["plugin_id"] == PLUGIN_ID
    assert detail["data"]["enforcement"]["enforced"] is False


def test_plugins_api_quota_endpoint_404s_for_unknown_plugin(monkeypatch):
    from app.api.v1 import plugins as plugins_api
    from app.core.exceptions import NotFoundException

    with plugin_env() as manager:
        monkeypatch.setattr(plugins_api, "plugin_manager", manager)
        with pytest.raises(NotFoundException):
            _run(plugins_api.plugin_quota_status("not.installed", payload={}))


def test_contract_exports_are_reachable_from_the_plugins_package():
    """契约入口只有一个（``lumi_contracts.plugins.execution``），不散落实现。"""
    assert execution_contract.classify_execution is classify_execution
    assert execution_contract.PLUGIN_WORKER_UNAVAILABLE == PLUGIN_WORKER_UNAVAILABLE
    assert boundary_mod.resolve_execution.__module__ == "app.plugins.boundary"
    assert boundary_mod.FLAG == FLAG
