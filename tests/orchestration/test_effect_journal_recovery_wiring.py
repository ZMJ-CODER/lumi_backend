"""阶段 3：恢复链路接入 Effect Journal（``EFFECT_JOURNAL_RECOVERY_V2``）。

覆盖：

* 开关关闭 → 与改造前逐字节一致（不读日志、不写审计、仍走 effectful_task 拦截）；
* 开关打开 → ``plan_recovery`` 接入：``confirmed`` 跳过、``uncertain``/``intent`` 暂停、
  日志不可用 fail-closed 暂停；
* 只有**只读步骤**与**带 idempotency_key 的幂等步骤**能自动恢复；
* 删除/提交/发布类高风险步骤与"有写声明但没有幂等键"的步骤永不自�动重放；
* 复用既有 Effect Journal（``effects`` 适配器 + ``EffectJournalRepository``），不建第二套。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.orchestration.recovery import effect_journal_recovery as recovery
from app.agents.orchestration.runtime import effects
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.planning.planner import Planner, TaskTree
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore
from app.core.config import settings
from app.repositories.effect_journal_repository import (
    EffectJournalUnavailable,
    InMemoryEffectJournalRepository,
)


class LevelPlanner(Planner):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def plan(self, *args, **kwargs):
        return TaskTree(nodes=[])

    async def plan_for_level(self, level, *args, **kwargs):
        self.calls.append({"level": level})
        return TaskTree(nodes=[TaskNode(id="replacement", agent="worker")])


def _orch():
    store = InMemoryStateStore()
    planner = LevelPlanner()
    orchestrator = AgentOrchestrator(
        store=store,
        planner=planner,
        workers={"worker": object()},
        review=NoopReviewer(),
        temporal_enabled=False,
    )
    return orchestrator, store, planner


def _failed_node(node_id: str = "failed", **kwargs) -> TaskNode:
    return TaskNode(
        id=node_id,
        agent="worker",
        status=TaskStatus.FAILED,
        error="当前方法不可用",
        error_code="CAPABILITY_UNAVAILABLE",
        **kwargs,
    )


def _job(nodes: list[TaskNode], *, scene: str = "office") -> Job:
    return Job(
        job_id="j1",
        user_id="u1",
        request="处理文档",
        scene=scene,
        status=JobStatus.FAILED,
        routing={"level": "m0", "upgrade_count": 0, "replan_count": 0},
        nodes=nodes,
    )


async def _run_recovery(orchestrator, job: Job) -> tuple[bool, Job]:
    await orchestrator._store.create_job(job)
    orchestrator._job_plan_context[job.job_id] = {
        "user_id": "u1",
        "request": job.request,
        "scene": "office",
        "project_id": None,
        "project_ids": None,
        "llm_api_key": None,
        "clarification_answer": None,
        "office_docs": None,
        "prior_summaries": "",
    }
    changed = await orchestrator._maybe_replan_failed_job(job, None)
    return changed, await orchestrator._store.get_job(job.job_id)


async def _seed_journal(key: str, status: str, *, node_id: str = "failed") -> None:
    """用既有 Effect Journal 适配器写入真实记录（不 mock 决策输入）。"""
    await effects.record_effect_intent(key, {"job_id": "j1", "node_id": node_id})
    if status == "confirmed":
        await effects.confirm_effect(key, {"ok": True})
    elif status == "uncertain":
        await effects.mark_effect_uncertain(key, "test_interrupted")


def _enable(monkeypatch, *, on: bool = True) -> None:
    monkeypatch.setattr(settings, "EFFECT_JOURNAL_RECOVERY_V2", on)


# ── 开关关闭：与改造前一致 ───────────────────────────────────


def test_flag_off_keeps_todays_behaviour_and_never_reads_the_journal(monkeypatch):
    _enable(monkeypatch, on=False)
    reads: list[str] = []

    async def _spy(key: str):
        reads.append(key)
        return {"status": "uncertain"}

    monkeypatch.setattr(effects, "get_effect", _spy)
    orchestrator, _store, planner = _orch()
    job = _job([_failed_node(idempotency_key="effect-key")])

    changed, saved = asyncio.run(_run_recovery(orchestrator, job))
    assert changed is False
    assert saved.routing["automatic_replan_blocked"] == "effectful_task", "关闭开关必须走旧拦截"
    assert "effect_recovery_plan" not in saved.routing
    assert reads == [], "关闭开关不得读取 Effect Journal"
    assert planner.calls == []


def test_flag_off_read_only_job_still_replans(monkeypatch):
    _enable(monkeypatch, on=False)
    orchestrator, _store, planner = _orch()
    changed, saved = asyncio.run(_run_recovery(orchestrator, _job([_failed_node()])))
    assert changed is True
    assert planner.calls, "关闭开关时只读失败任务照旧自动重排"
    assert "effect_recovery_plan" not in saved.routing


# ── 开关打开：契约决策表接进恢复链路 ─────────────────────────


def test_uncertain_effect_pauses_for_human(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, planner = _orch()
    job = _job([_failed_node(idempotency_key="k-uncertain")])

    async def scenario():
        await _seed_journal("k-uncertain", "uncertain")
        return await _run_recovery(orchestrator, job)

    changed, saved = asyncio.run(scenario())
    assert changed is False
    assert saved.status == JobStatus.PAUSED, "副作用不确定必须暂停等人工，而不是继续自动恢复"
    assert saved.routing["automatic_replan_blocked"] == "effect_journal_requires_human"
    plan = saved.routing["effect_recovery_plan"]
    assert plan["requires_human"] is True and plan["resume_allowed"] is False
    assert plan["paused_step_ids"] == ["failed"]
    assert "EFFECT_UNCERTAIN" in plan["reason_codes"]
    assert planner.calls == []


def test_in_flight_intent_pauses_for_human(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, _planner = _orch()
    job = _job([_failed_node(idempotency_key="k-intent")])

    async def scenario():
        await _seed_journal("k-intent", "intent")
        return await _run_recovery(orchestrator, job)

    changed, saved = asyncio.run(scenario())
    assert changed is False
    assert saved.status == JobStatus.PAUSED
    assert "EFFECT_IN_FLIGHT" in saved.routing["effect_recovery_plan"]["reason_codes"]


def test_confirmed_effect_is_skipped_and_never_replayed(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, _planner = _orch()
    node = _failed_node(idempotency_key="k-done")
    job = _job([node])

    async def scenario():
        await _seed_journal("k-done", "confirmed")
        return await _run_recovery(orchestrator, job)

    _changed, saved = asyncio.run(scenario())
    assert saved.routing["effect_recovery_plan"]["skip_step_ids"] == ["failed"]
    assert saved.routing["effect_recovery_plan"]["requires_human"] is False
    saved_node = next(item for item in saved.nodes if item.id == "failed")
    assert saved_node.status == TaskStatus.COMPLETED, "已确认的副作用步骤必须被跳过而不是重放"
    assert saved_node.effect_status == "committed"
    assert saved_node.metadata.get("recovery") == "effect_already_committed"


def test_read_only_step_is_auto_resumed(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, planner = _orch()
    changed, saved = asyncio.run(_run_recovery(orchestrator, _job([_failed_node()])))
    assert changed is True, "只读步骤未执行过副作用，可自动恢复"
    plan = saved.routing["effect_recovery_plan"]
    assert plan["resumable_step_ids"] == ["failed"]
    assert plan["reason_codes"] == ["READ_ONLY_STEP_NOT_STARTED"]


def test_idempotent_effect_without_record_is_auto_resumed(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, _planner = _orch()
    job = _job([_failed_node(idempotency_key="k-idem")])
    changed, saved = asyncio.run(_run_recovery(orchestrator, job))
    plan = saved.routing["effect_recovery_plan"]
    assert plan["resumable_step_ids"] == ["failed"]
    assert plan["reason_codes"] == ["IDEMPOTENT_EFFECT_NOT_STARTED"]
    assert plan["resume_allowed"] is True, "带幂等键的未开始副作用允许自动恢复"
    # 既有 effectful_task 拦截仍在（本阶段只改"恢复判定"，不改重排准入）。
    assert changed is False


@pytest.mark.parametrize(
    ("tool", "label"),
    [
        ("workspace_delete", "删除"),
        ("git_commit", "提交"),
        ("publish_release", "发布"),
        ("send_email", "外发"),
    ],
)
def test_high_risk_steps_are_never_auto_replayed(monkeypatch, tool, label):
    _enable(monkeypatch)
    orchestrator, _store, planner = _orch()
    node = _failed_node(idempotency_key=f"k-{tool}", params={"preferred_tool": tool})
    job = _job([node])

    changed, saved = asyncio.run(_run_recovery(orchestrator, job))
    assert changed is False, f"{label}类高风险步骤不得自动重放"
    assert saved.status == JobStatus.PAUSED
    plan = saved.routing["effect_recovery_plan"]
    assert plan["paused_step_ids"] == ["failed"]
    assert plan["reason_codes"] == ["HIGH_RISK_EFFECT_REQUIRES_HUMAN"]
    assert planner.calls == []


def test_write_step_without_idempotency_key_pauses(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, _planner = _orch()
    node = _failed_node(
        params={"preferred_tool": "workspace_write"},
        resource_claims=[ResourceClaim(key="user:u1:doc", mode="write")],
    )
    changed, saved = asyncio.run(_run_recovery(orchestrator, _job([node])))
    assert changed is False
    plan = saved.routing["effect_recovery_plan"]
    assert plan["paused_step_ids"] == ["failed"]
    assert plan["reason_codes"] == ["EFFECT_WITHOUT_IDEMPOTENCY_KEY"]


def test_journal_unavailable_is_fail_closed(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, _planner = _orch()
    job = _job([_failed_node(idempotency_key="k-broken")])

    async def _boom(key: str):
        raise EffectJournalUnavailable("副作用日志数据库不可用")

    monkeypatch.setattr(effects, "get_effect", _boom)
    changed, saved = asyncio.run(_run_recovery(orchestrator, job))
    assert changed is False, "日志不可用时不得把'没有记录'当成'没有执行'"
    plan = saved.routing["effect_recovery_plan"]
    assert plan["journal_available"] is False
    assert plan["reason_codes"] == ["EFFECT_JOURNAL_UNAVAILABLE"]
    assert saved.status == JobStatus.PAUSED


def test_non_office_scene_is_untouched_by_the_gate(monkeypatch):
    _enable(monkeypatch)
    orchestrator, _store, _planner = _orch()
    job = _job([_failed_node(idempotency_key="k-scene")], scene="code")
    changed, saved = asyncio.run(_run_recovery(orchestrator, job))
    assert changed is False
    assert "effect_recovery_plan" not in (saved.routing or {})


# ── 纯决策层（契约 + App 红绿灯）────────────────────────────


def test_decide_recovery_uses_the_contract_plan_and_app_gate():
    job = _job(
        [
            _failed_node("s1", idempotency_key="k1"),
            _failed_node("s2", idempotency_key="k2"),
            _failed_node("s3"),
            _failed_node("s4", idempotency_key="k4", params={"preferred_tool": "workspace_delete"}),
        ]
    )
    outcome = recovery.decide_recovery(
        job, {"k1": "confirmed", "k2": "uncertain", "k4": "confirmed"}
    )
    assert outcome.skip_step_ids == ("s1", "s4")
    assert outcome.paused_step_ids == ("s2",)
    assert outcome.resumable_step_ids == ("s3",)
    assert outcome.resume_allowed is False
    payload = outcome.as_dict()
    assert payload["skip_step_ids"] == ["s1", "s4"]
    assert set(payload["reason_codes"]) >= {
        "EFFECT_ALREADY_COMMITTED",
        "EFFECT_UNCERTAIN",
        "READ_ONLY_STEP_NOT_STARTED",
    }
    # 审计字段不含参数/任务正文
    assert "处理文档" not in str(payload)


def test_node_effect_status_is_a_fail_safe_fallback():
    """旧快照已带 effect_status 但日志缺记录时，宁可跳过也不重放。"""
    job = _job([_failed_node("s1", idempotency_key="k1", effect_status="committed")])
    outcome = recovery.decide_recovery(job, {})
    assert outcome.skip_step_ids == ("s1",)


def test_journal_statuses_are_read_through_the_existing_repository():
    repository = InMemoryEffectJournalRepository()
    effects.set_effect_journal_repository_for_tests(repository)
    try:

        async def scenario():
            await effects.record_effect_intent("k1", {"job_id": "j1", "node_id": "s1"})
            await effects.confirm_effect("k1", {"ok": True})
            job = _job([_failed_node("s1", idempotency_key="k1")])
            return await recovery.plan_job_recovery(job)

        outcome = asyncio.run(scenario())
        assert outcome.skip_step_ids == ("s1",)
        assert outcome.journal_available is True
    finally:
        effects.set_effect_journal_repository_for_tests(None)
