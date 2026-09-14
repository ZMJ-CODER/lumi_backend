"""阶段 4：插件生命周期接入 ``PluginManager``（灰度 ``PLUGIN_QUOTA_ENFORCEMENT``）。

钉住四件事：

1. install/enable/disable/upgrade/uninstall 全部走**冻结状态机**，非法迁移被拒绝；
2. 运行中 Job 的分级策略按 ``operation_policy()`` 表执行：``disable`` 复用既有预检
   （``CAPABILITY_UNAVAILABLE``）、``upgrade`` drain、``uninstall`` 经既有 Effect
   Journal 标 ``uncertain``、``crash`` 重启 + 幂等去重 + 熔断自动停用；
3. 不重复造轮子：预检是既有 ``CapabilityPreflightService``，副作用是既有
   ``app.agents.orchestration.runtime.effects`` 适配器；
4. **开关关闭 = 改造前**：不咨询状态机、不写生命周期记录、落盘记录逐字节一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

from lumi_contracts.plugins import PluginManifest
from lumi_contracts.plugins import lifecycle as plugin_lifecycle

from app.agents.orchestration.preflight.capability_preflight import (
    PreflightStatus,
    preflight_capabilities,
)
from app.agents.orchestration.preflight.capability_preflight_service import (
    CapabilityPreflightService,
)
from app.core.config import settings
from app.repositories.effect_journal_repository import InMemoryEffectJournalRepository
from app.plugins import (
    PluginManager,
    PluginOperationError,
    PluginRegistry,
    PluginStateStore,
)
from app.plugins.manager import (
    FLAG,
    LIFECYCLE_RECORD_KEY,
    PLUGIN_STATE_TRANSITION_ILLEGAL,
)

from _paths import REPO_ROOT
_CASE = itertools.count(1)
PLUGIN_ID = "lumi.code_expert"


@contextlib.contextmanager
def state_dir():
    """独立的插件状态目录（放在工作区 ``.ptmp``；名字带 PID 与序号，互不干扰）。"""
    import shutil

    base = REPO_ROOT / ".ptmp"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"plugin_lifecycle_{os.getpid()}_{next(_CASE)}"
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _manifest(**overrides) -> PluginManifest:
    payload = {
        "id": PLUGIN_ID,
        "version": "1.0.0",
        "kind": "skill_plugin",
        "deployment": "server",
        "data_locality": "cloud",
        "isolation": "sandboxed",
        "trust_level": "official",
        "provides": {"capabilities": ["code.execute@1"]},
    }
    payload.update(overrides)
    return PluginManifest(**payload)


@contextlib.contextmanager
def manager(directory: Path, *, flag: bool = True, store: PluginStateStore | None = None):
    """构造管理器 + 注册表；``flag`` 直接决定灰度开关。"""
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, FLAG, flag)
    try:
        state = store or PluginStateStore(state_dir=directory)
        registry = PluginRegistry(store=state)
        yield PluginManager(registry=registry, state_store=state)
    finally:
        monkey.undo()


def _run(awaitable):
    return asyncio.run(awaitable)


def _stored_state(directory: Path) -> dict:
    target = Path(directory) / "state.json"
    if not target.exists():
        return {}
    payload = json.loads(target.read_text(encoding="utf-8"))
    return {row["plugin_id"]: row for row in payload["plugins"]}


# ── (1) 状态机 ───────────────────────────────────────────


def test_install_enable_disable_follow_the_frozen_state_machine():
    with state_dir() as path, manager(path) as mgr:
        installation = _run(mgr.install(_manifest()))
        assert installation.enabled is True
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"
        assert _stored_state(path)[PLUGIN_ID][LIFECYCLE_RECORD_KEY]["state"] == "enabled"

        disabled = _run(mgr.disable(PLUGIN_ID, reason="用户停用"))
        assert disabled.enabled is False
        assert mgr.lifecycle_state(PLUGIN_ID) == "disabled"
        record = _stored_state(path)[PLUGIN_ID][LIFECYCLE_RECORD_KEY]
        assert record["state"] == "disabled"
        # 操作策略按 operation_policy() 留痕（不是硬编码字符串）
        assert record["operation_policy"] == plugin_lifecycle.operation_policy("disable").as_dict()

        assert _run(mgr.enable(PLUGIN_ID)).enabled is True
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"


def test_illegal_transition_is_rejected_and_leaves_the_registry_untouched():
    with state_dir() as path, manager(path) as mgr:
        # 只登记不激活 → registered；registered → disabled 不在迁移表内。
        installation = _run(mgr.install(_manifest(), activate=False))
        assert installation.enabled is False
        assert mgr.lifecycle_state(PLUGIN_ID) == "registered"
        with pytest.raises(PluginOperationError) as info:
            _run(mgr.disable(PLUGIN_ID))
        assert info.value.code == PLUGIN_STATE_TRANSITION_ILLEGAL
        assert info.value.details == {"plugin_id": PLUGIN_ID, "from": "registered", "to": "disabled"}
        # 拒绝发生在注册表被改动之前
        assert mgr.get(PLUGIN_ID).enabled is False
        assert mgr.lifecycle_state(PLUGIN_ID) == "registered"
        # 合法路径仍然可用
        assert _run(mgr.enable(PLUGIN_ID)).enabled is True


def test_uninstalled_is_terminal_until_reinstalled():
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        assert _run(mgr.uninstall(PLUGIN_ID)) is True
        assert mgr.get(PLUGIN_ID) is None
        assert mgr.lifecycle_state(PLUGIN_ID) == "uninstalled"
        # 重新安装 = 新登记（不是从终态复活）
        reinstalled = _run(mgr.install(_manifest()))
        assert reinstalled.enabled is True
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"


# ── (2) upgrade → drain ──────────────────────────────────


def test_upgrade_drains_old_jobs_and_new_jobs_use_the_new_version():
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        old_job = mgr.begin_step(PLUGIN_ID, job_id="job-old", step_id="s1")

        upgraded = _run(mgr.upgrade(_manifest(version="1.1.0")))
        assert upgraded.version == "1.1.0"
        assert mgr.lifecycle_state(PLUGIN_ID) == "upgrading", "有在途 Job 时必须保持 drain 窗口"
        drain = mgr.drain_state(PLUGIN_ID)
        assert (drain.from_version, drain.to_version, drain.closed) == ("1.0.0", "1.1.0", False)

        # 老 Job 继续用旧版本（旧 Worker 跑完当前 Job）
        assert mgr.trace_fields(plugin_id=PLUGIN_ID, job_id="job-old")["plugin_version"] == "1.0.0"
        assert old_job.plugin_version == "1.0.0"
        # 新 Job 用新版本，且 trace 里可见
        new_job = mgr.begin_step(PLUGIN_ID, job_id="job-new", step_id="s1")
        assert new_job.plugin_version == "1.1.0"
        trace = mgr.trace_fields(plugin_id=PLUGIN_ID, job_id="job-new")
        assert trace["plugin_version"] == "1.1.0" and trace["plugin_draining"] is True

        # 老 Job 收尾后新 Job 仍在跑 → drain 未收口；新 Job 也收尾 → 回到 enabled
        mgr.finish_step(old_job)
        assert mgr.drain_state(PLUGIN_ID).closed is False
        assert mgr.lifecycle_state(PLUGIN_ID) == "upgrading"
        mgr.finish_step(new_job)
        assert mgr.drain_state(PLUGIN_ID).closed is True
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"
        assert mgr.trace_fields(plugin_id=PLUGIN_ID, job_id="job-old")["plugin_version"] == "1.0.0"


def test_upgrade_without_inflight_closes_the_drain_immediately():
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        _run(mgr.upgrade(_manifest(version="2.0.0")))
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"
        assert mgr.drain_state(PLUGIN_ID).closed is True


# ── (3) disable → 既有预检阻断 ────────────────────────────


def test_disable_lets_started_calls_finish_and_blocks_new_steps_via_preflight(monkeypatch):
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        inflight = mgr.begin_step(PLUGIN_ID, job_id="job-running", step_id="s1")

        _run(mgr.disable(PLUGIN_ID, reason="停用"))
        # inflight="finish"：在途 step 不被回收，仍能正常结束
        assert mgr.inflight_count(PLUGIN_ID) == 1
        mgr.finish_step(inflight)
        assert mgr.inflight_count(PLUGIN_ID) == 0

        # new_steps="preflight_block"：能力进入"不可用"事实，由**既有预检**翻成错误码
        assert mgr.blocked_capabilities() == {"code.execute"}
        result = preflight_capabilities(
            required_capabilities=["code.execute"],
            available_capabilities={"code.execute": False},
            registered_tools=["sandbox_run_in_sandbox"],
        )
        assert result.status == PreflightStatus.CAPABILITY_UNAVAILABLE.value
        assert result.error_code == "CAPABILITY_UNAVAILABLE"
        assert result.must_call_model is False and result.tool_window == ()


def test_disabled_plugin_is_reported_unavailable_by_the_existing_broker_probe(monkeypatch):
    """插件事实经既有 probe → CapabilityPreflightService → CAPABILITY_UNAVAILABLE。"""
    from app.agents.capabilities.broker import broker as broker_mod
    from app.agents.orchestration.planning import office_plan_selection_service as selection
    from app.agents.orchestration.planning.context import PlanRequestContext
    from app.plugins import manager as manager_mod

    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        _run(mgr.disable(PLUGIN_ID, reason="停用"))
        monkeypatch.setattr(manager_mod, "_manager", mgr)
        # Broker 本身对该能力"无问题"（返回空事实），唯一来源就是插件状态
        monkeypatch.setattr(
            broker_mod.capability_broker, "preflight_facts", lambda *a, **k: {}
        )
        context = PlanRequestContext.from_legacy_args(
            user_id="u1", request="跑一段脚本", scene="office", workspace_id="w1"
        )
        probe = selection._broker_capability_probe(context)
        facts = probe(["code.execute@1"])
        assert facts == {"code.execute@1": "capability_unavailable"}

        service = CapabilityPreflightService(settings=SimpleNamespace(**{FLAG: True}))
        result = service.preflight(
            profile={"required_capabilities": ["code.execute@1"], "action_intents": ["EXECUTE"]},
            probe=probe,
        )
        assert result.status == PreflightStatus.CAPABILITY_UNAVAILABLE.value
        assert result.must_call_model is False


# ── (4) uninstall → 既有 Effect Journal ──────────────────


def test_uninstall_marks_inflight_effects_uncertain_through_the_existing_journal():
    from app.agents.orchestration.runtime import effects

    repository = InMemoryEffectJournalRepository()
    effects.set_effect_journal_repository_for_tests(repository)
    try:
        with state_dir() as path, manager(path) as mgr:
            _run(mgr.install(_manifest()))
            _run(effects.record_effect_intent("effect-key-1", {"job_id": "j1", "node_id": "s1"}))
            _run(effects.record_effect_intent("effect-key-2", {"job_id": "j1", "node_id": "s2"}))
            lease = mgr.begin_step(PLUGIN_ID, job_id="j1", step_id="s1", effect_key="effect-key-1")
            mgr.begin_step(PLUGIN_ID, job_id="j1", step_id="s2", effect_key="effect-key-2")
            assert mgr.inflight_count(PLUGIN_ID) == 2

            assert _run(mgr.uninstall(PLUGIN_ID)) is True
            for key in ("effect-key-1", "effect-key-2"):
                record = _run(effects.get_effect(key))
                assert record["status"] == "uncertain", f"{key} 必须被标 uncertain"
                assert record["reason"] == "plugin_uninstalled"
            assert lease.effect_key == "effect-key-1"
            assert mgr.lifecycle_state(PLUGIN_ID) == "uninstalled"
    finally:
        effects.set_effect_journal_repository_for_tests(None)


def test_uninstall_without_inflight_effects_is_a_noop_on_the_journal():
    from app.agents.orchestration.runtime import effects

    repository = InMemoryEffectJournalRepository()
    effects.set_effect_journal_repository_for_tests(repository)
    try:
        with state_dir() as path, manager(path) as mgr:
            _run(mgr.install(_manifest()))
            assert _run(mgr.uninstall(PLUGIN_ID)) is True
            assert _run(effects.get_effect("nothing")) is None
    finally:
        effects.set_effect_journal_repository_for_tests(None)


# ── (5) crash → 重启 / 去重 / 熔断 ────────────────────────


def test_crash_restarts_and_dedupes_by_idempotency_key():
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        first = mgr.record_crash(PLUGIN_ID, idempotency_key="key-a", error="boom")
        assert (first.restarted, first.deduplicated, first.circuit_broken) == (True, False, False)
        assert first.state == "enabled" and first.consecutive_crashes == 1
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"
        assert mgr.consecutive_crashes(PLUGIN_ID) == 1
        # 策略表留痕：crash → inflight="restart"、new_steps="normal"
        record = _stored_state(path)[PLUGIN_ID][LIFECYCLE_RECORD_KEY]
        assert record["operation_policy"] == plugin_lifecycle.operation_policy("crash").as_dict()

        # 同一个幂等键重放：不重复计数、不重复重启
        replay = mgr.record_crash(PLUGIN_ID, idempotency_key="key-a", error="boom")
        assert replay.deduplicated is True
        assert replay.consecutive_crashes == 1 and replay.restarted is False

        second = mgr.record_crash(PLUGIN_ID, idempotency_key="key-b", error="boom")
        assert second.consecutive_crashes == 2 and second.restarted is True
        assert second.circuit_broken is False


def test_repeated_crashes_circuit_break_to_auto_disabled_with_a_warning():
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]), level="WARNING")
    try:
        with state_dir() as path, manager(path) as mgr:
            _run(mgr.install(_manifest()))
            outcomes = [
                mgr.record_crash(PLUGIN_ID, idempotency_key=f"key-{index}", error="worker died")
                for index in range(plugin_lifecycle.CRASH_CIRCUIT_THRESHOLD)
            ]
            assert [item.circuit_broken for item in outcomes] == [False, False, True]
            last = outcomes[-1]
            assert last.state == "disabled" and last.restarted is True
            assert last.consecutive_crashes == plugin_lifecycle.CRASH_CIRCUIT_THRESHOLD
            assert mgr.lifecycle_state(PLUGIN_ID) == "disabled"
            assert mgr.get(PLUGIN_ID).enabled is False, "熔断必须真的停用插件"
            assert any("熔断" in message for message in messages), messages
    finally:
        logger.remove(sink_id)


def test_successful_run_resets_the_crash_counter():
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        mgr.record_crash(PLUGIN_ID, idempotency_key="k1")
        assert mgr.consecutive_crashes(PLUGIN_ID) == 1
        mgr.note_success(PLUGIN_ID)
        assert mgr.consecutive_crashes(PLUGIN_ID) == 0
        # 清零后重新计数，不会误判熔断
        assert mgr.record_crash(PLUGIN_ID, idempotency_key="k2").consecutive_crashes == 1


# ── (6) 开关关闭：与改造前一致 ────────────────────────────


def test_flag_off_never_consults_the_state_machine_or_the_journal(monkeypatch):
    from app.agents.orchestration.runtime import effects

    def _boom(*args, **kwargs):  # pragma: no cover - 只在被误调用时触发
        raise AssertionError("开关关闭时不得咨询生命周期契约")

    monkeypatch.setattr(plugin_lifecycle, "transition", _boom)
    monkeypatch.setattr(plugin_lifecycle, "operation_policy", _boom)
    monkeypatch.setattr(plugin_lifecycle, "should_circuit_break", _boom)
    monkeypatch.setattr(effects, "mark_effect_uncertain", _boom)

    with state_dir() as path, manager(path, flag=False) as mgr:
        assert mgr.enabled() is False
        assert mgr.lifecycle_state(PLUGIN_ID) == ""  # 尚未安装
        installation = _run(mgr.install(_manifest()))
        assert installation.enabled is True
        assert mgr.lifecycle_state(PLUGIN_ID) == "enabled"
        assert mgr.blocked_capabilities() == set()
        assert mgr.record_crash(PLUGIN_ID, idempotency_key="k") is None
        assert mgr.drain_state(PLUGIN_ID) is None

        lease = mgr.begin_step(PLUGIN_ID, job_id="j", step_id="s", effect_key="k")
        assert lease.tracked is False and lease.plugin_version == "1.0.0"
        assert mgr.inflight_count(PLUGIN_ID) == 0
        mgr.finish_step(lease)

        assert _run(mgr.disable(PLUGIN_ID)).enabled is False
        assert mgr.lifecycle_state(PLUGIN_ID) == "disabled"
        assert _run(mgr.enable(PLUGIN_ID)).enabled is True
        assert _run(mgr.upgrade(_manifest(version="1.1.0"))).version == "1.1.0"
        assert _run(mgr.uninstall(PLUGIN_ID)) is True

        # 落盘记录里没有生命周期留痕
        assert LIFECYCLE_RECORD_KEY not in json.dumps(_stored_state(path))


def test_flag_off_persisted_record_matches_the_registry_path():
    """同一 Manifest：经管理器的落盘记录与直接经注册表的**逐字段一致**。"""
    with state_dir() as managed_dir, manager(managed_dir, flag=False) as mgr:
        _run(mgr.install(_manifest()))
        managed = _stored_state(managed_dir)[PLUGIN_ID]

    with state_dir() as plain_dir:
        direct = PluginRegistry(store=PluginStateStore(state_dir=plain_dir))
        direct.install(_manifest())
        plain = _stored_state(plain_dir)[PLUGIN_ID]

    assert set(managed) == set(plain)
    volatile = {"installed_at", "updated_at"}
    assert {key: managed[key] for key in managed if key not in volatile} == {
        key: plain[key] for key in plain if key not in volatile
    }
    assert LIFECYCLE_RECORD_KEY not in managed


def test_flag_on_adds_lifecycle_bookkeeping_but_keeps_the_registry_fields():
    with state_dir() as path, manager(path) as mgr:
        _run(mgr.install(_manifest()))
        row = _stored_state(path)[PLUGIN_ID]
        # 注册表既有字段一个不少
        for key in ("plugin_id", "version", "kind", "enabled", "verified", "digest", "provides"):
            assert key in row
        assert row[LIFECYCLE_RECORD_KEY]["state"] == "enabled"


# ── (7) 声明 → 执行：生命周期层交出真正带配额的 Worker ────


def test_worker_for_turns_the_manifest_declaration_into_a_real_boundary():
    with state_dir() as path, manager(path) as mgr:
        _run(
            mgr.install(
                _manifest(
                    resource_limits={
                        "memory_mb": 96,
                        "cpu_seconds": 4.0,
                        "wall_seconds": 6.0,
                        "max_output_bytes": 3072,
                        "max_concurrency": 2,
                    }
                )
            )
        )
        worker = mgr.worker_for(PLUGIN_ID)
        assert worker.enabled() is True
        assert worker.spec.plugin_id == PLUGIN_ID and worker.spec.plugin_version == "1.0.0"
        assert worker.spec.quota.memory_mb == 96
        assert worker.spec.quota.timeout_seconds == 6
        assert worker.spec.quota.max_output_bytes == 3072
        assert worker.spec.cpu_seconds == 4.0 and worker.spec.max_concurrency == 2
        assert worker.spec.hard_limits is True

    # 开关关闭：拿到的 Worker 是纯透传（不判定配额）
    with state_dir() as path, manager(path, flag=False) as mgr:
        _run(mgr.install(_manifest()))
        assert mgr.worker_for(PLUGIN_ID).enabled() is False
