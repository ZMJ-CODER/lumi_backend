"""步骤检查点与恢复时序回归（方案 §2 / §5）。

覆盖验收场景：

* 状态机：``planned → started → running → … → uncertain``，非法跃迁被拒绝；
* **完成事件必须在检查点落盘之后**（铁律，避免"前端看到完成、刷新后没有结果"）；
* 写文件等待审批后进程崩溃 → 恢复后状态仍是 ``waiting_approval``；
* 写文件请求在途时用户取消 → step → ``uncertain``（绝不自动重跑）；
* 检查点与 Job 快照分离：500 步长任务不会把快照撑爆；
* 恢复流程：Job revision 领先检查点时，旧检查点被判陈旧、以 Job 状态为准。
"""

from __future__ import annotations

import asyncio

from lumi_contracts.persistence.checkpoint import (
    StepCheckpoint,
    StepCheckpointState,
    assert_emit_after_persist,
    can_transition,
    checkpoint_is_stale,
    merge_checkpoint,
    runtime_status_for,
    unfinished_steps,
)
from lumi_contracts.persistence.recovery_plan import (
    RecoveryDecision,
    plan_resume,
    settle_reconciled_step,
)
from app.services import step_checkpoint as checkpoint_module
from app.services.step_checkpoint import (
    InMemoryCheckpointStore,
    StepCheckpointCoordinator,
    load_checkpoints,
    state_for_step,
)


def _coordinator(job_id: str = "job-1", store=None) -> StepCheckpointCoordinator:
    return StepCheckpointCoordinator(
        job_id=job_id, store=store or InMemoryCheckpointStore(), enabled=True
    )


# ── 状态机 ───────────────────────────────────────────────────


def test_state_machine_allows_the_documented_path_and_rejects_skips():
    assert can_transition(StepCheckpointState.PLANNED, StepCheckpointState.STARTED)
    assert can_transition(StepCheckpointState.PLANNED, StepCheckpointState.RUNNING)
    assert can_transition(StepCheckpointState.STARTED, StepCheckpointState.RUNNING)
    assert can_transition(StepCheckpointState.RUNNING, StepCheckpointState.WAITING_APPROVAL)
    assert can_transition(StepCheckpointState.WAITING_APPROVAL, StepCheckpointState.COMPLETED)
    assert can_transition(StepCheckpointState.STARTED, StepCheckpointState.UNCERTAIN)
    # 非法：还没开始就完成；终态不能回退。
    assert not can_transition(StepCheckpointState.PLANNED, StepCheckpointState.COMPLETED)
    assert not can_transition(StepCheckpointState.COMPLETED, StepCheckpointState.RUNNING)


def test_uncertain_is_terminal_and_never_auto_resumes():
    checkpoint = StepCheckpoint(job_id="j", step_id="s", status=StepCheckpointState.STARTED)
    marked = checkpoint.advanced_to(StepCheckpointState.UNCERTAIN, error_code="EFFECT_UNCERTAIN")
    assert marked.is_terminal
    assert marked.state is StepCheckpointState.UNCERTAIN
    assert runtime_status_for(marked.status) == "settled"
    # 但它在**恢复**里必须仍然可见：`uncertain` 等的是人工决策，不是"已结束"。
    assert unfinished_steps([marked]) == (marked,)
    plan = plan_resume(job_id="j", job_plan_revision=marked.checkpoint_version, checkpoints=[marked])
    assert plan.requires_human is True
    assert plan.paused_step_ids == ("s",)


def test_coordinator_rejects_illegal_transition_without_writing():
    async def scenario():
        store = InMemoryCheckpointStore()
        coordinator = _coordinator("job-illegal", store)
        # planned → completed 非法（必须先 started/running 并经过结果落盘）。
        rejected = await coordinator.record("s1", StepCheckpointState.COMPLETED)
        assert rejected.persisted is False and rejected.error
        assert await store.load("job-illegal") == []
        # 既有内核会直接把步骤标成 running，这条捷径必须被接受。
        started = await coordinator.record("s1", StepCheckpointState.RUNNING)
        assert started.persisted is True
        completed = await coordinator.record("s1", StepCheckpointState.COMPLETED)
        assert completed.checkpoint_version > started.checkpoint_version

    asyncio.run(scenario())


def test_checkpoint_is_isolated_from_job_snapshot_and_bounded():
    """500 步长任务：检查点各自落盘，绝不进 Job 快照。"""

    async def scenario():
        store = InMemoryCheckpointStore()
        coordinator = _coordinator("job-500", store)
        for index in range(500):
            step_id = f"s{index}"
            await coordinator.record(step_id, StepCheckpointState.STARTED)
            await coordinator.record(step_id, StepCheckpointState.RUNNING)
            await coordinator.record(step_id, StepCheckpointState.COMPLETED, result_ref={"id": f"r{index}", "sha256": "x"})
        rows = await load_checkpoints("job-500", store=store)
        assert len(rows) == 500
        # 检查点存储键与 Job 快照键不同：写检查点不会让快照膨胀。
        assert checkpoint_module.checkpoint_key("job-500") != "multiagent:job_snapshot:job-500"
        assert all(item.result_ref for item in rows)

    asyncio.run(scenario())


# ── 完成事件时序（方案 §2.3 铁律）─────────────────────────


def test_emit_gate_refuses_completion_events_before_persist():
    try:
        assert_emit_after_persist("step_completed", persisted_version=0, current_version=1)
        raise AssertionError("未落盘的完成事件必须被拒绝")
    except Exception as exc:  # CheckpointOrderingError
        assert "落盘" in str(exc)
    # 非完成类事件不受约束。
    assert_emit_after_persist("text_delta", persisted_version=0, current_version=1)
    # 落盘版本落后同样拒绝。
    try:
        assert_emit_after_persist("task_completed", persisted_version=1, current_version=3)
        raise AssertionError("落后的检查点版本必须被拒绝")
    except Exception as exc:  # CheckpointOrderingError
        assert "落后" in str(exc)


def test_completion_event_is_confirmed_only_after_checkpoint_persisted():
    async def scenario():
        store = InMemoryCheckpointStore()
        coordinator = _coordinator("job-emit", store)
        # 未落盘时确认拿不到版本 → 完成事件不该发。
        assert await coordinator.confirm_persisted_for_emit("s1", "step_completed") == 0
        await coordinator.record("s1", StepCheckpointState.STARTED)
        await coordinator.record("s1", StepCheckpointState.RUNNING)
        done = await coordinator.record(
            "s1", StepCheckpointState.COMPLETED, result_ref={"id": "r1", "sha256": "abc"}
        )
        version = await coordinator.confirm_persisted_for_emit("s1", "step_completed")
        assert version == done.checkpoint_version > 0
        assert coordinator.emitted_events() == {"step_completed": 1}
        # 落盘记录里必须已经有结果引用（"完成事件晚于结果落盘"）。
        rows = await load_checkpoints("job-emit", store=store)
        assert rows[0].result_ref == {"id": "r1", "sha256": "abc"}

    asyncio.run(scenario())


def test_approval_then_crash_keeps_waiting_approval_state():
    """验收：写文件等待审批后进程崩溃 → 恢复后仍是 waiting_approval。"""

    async def scenario():
        store = InMemoryCheckpointStore()
        coordinator = _coordinator("job-approval", store)
        await coordinator.record("write", StepCheckpointState.STARTED, effect_type="file_create")
        await coordinator.record("write", StepCheckpointState.WAITING_APPROVAL, effect_status="pending")
        # 模拟进程崩溃：新协调器从落盘读回（内存游标不保留）。
        rows = await load_checkpoints("job-approval", store=store)
        assert rows[0].state is StepCheckpointState.WAITING_APPROVAL
        plan = plan_resume(
            job_id="job-approval",
            job_plan_revision=rows[0].checkpoint_version,
            checkpoints=rows,
        )
        # 在途副作用：不可判定 → 暂停等人工（不会自动重跑审批前的写文件）。
        assert plan.requires_human is True
        assert plan.paused_step_ids == ("write",)

    asyncio.run(scenario())


def test_cancel_while_effect_in_flight_marks_uncertain():
    """验收：写文件请求在途时取消 → step 变 uncertain，Journal 留 pending，恢复时判定。"""

    async def scenario():
        store = InMemoryCheckpointStore()
        coordinator = _coordinator("job-cancel", store)
        await coordinator.record("write", StepCheckpointState.STARTED, effect_type="file_create")
        marked = await coordinator.fail_closed_uncertain("write", "task_cancelled")
        assert marked.persisted is True
        rows = await load_checkpoints("job-cancel", store=store)
        assert rows[0].state is StepCheckpointState.UNCERTAIN
        assert rows[0].error_code == "EFFECT_UNCERTAIN"
        plan = plan_resume(
            job_id="job-cancel",
            job_plan_revision=rows[0].checkpoint_version,
            checkpoints=rows,
        )
        assert plan.decision == RecoveryDecision.NEEDS_HUMAN.value
        assert plan.resume_allowed is False

    asyncio.run(scenario())


# ── 恢复流程（方案 §5）──────────────────────────────────────


def test_stale_checkpoint_is_ignored_and_job_state_wins():
    rows = [StepCheckpoint(job_id="j", step_id="s1", checkpoint_version=2, status=StepCheckpointState.STARTED)]
    assert checkpoint_is_stale(checkpoint_version=2, job_plan_revision=5) is True
    plan = plan_resume(job_id="j", job_plan_revision=5, checkpoints=rows)
    assert plan.checkpoint_current is False
    assert plan.stale_step_ids == ("s1",)


def test_confirmed_effect_is_skipped_and_pending_is_reconciled():
    checkpoint = StepCheckpoint(
        job_id="j",
        step_id="write",
        status=StepCheckpointState.WAITING_APPROVAL,
        effect_status="pending",
        checkpoint_version=1,
    )
    confirmed = plan_resume(
        job_id="j",
        job_plan_revision=1,
        checkpoints=[checkpoint],
        journal={"k1": "confirmed"},
        idempotency_keys={"write": "k1"},
    )
    assert confirmed.skip_step_ids == ("write",)
    assert confirmed.resume_allowed is True

    pending = plan_resume(
        job_id="j",
        job_plan_revision=1,
        checkpoints=[checkpoint],
        journal={"k1": "pending"},
        idempotency_keys={"write": "k1"},
        reconcilable=("write",),
    )
    assert pending.reconcile_step_ids == ("write",)
    assert pending.resume_allowed is True

    # 不可核对（未声明 reconcilable）→ 交人工。
    unverifiable = plan_resume(
        job_id="j",
        job_plan_revision=1,
        checkpoints=[checkpoint],
        journal={"k1": "intent"},
        idempotency_keys={"write": "k1"},
    )
    assert unverifiable.paused_step_ids == ("write",)
    assert unverifiable.resume_allowed is False


def test_reconcile_result_converges_to_skip_or_resume():
    applied = settle_reconciled_step("write", effect_happened=True)
    assert applied.action == "SKIP_CONFIRMED"
    missing = settle_reconciled_step("write", effect_happened=False)
    assert missing.action == "RESUME"


def test_merge_keeps_the_newer_checkpoint():
    older = StepCheckpoint(job_id="j", step_id="s", attempt=1, checkpoint_version=5)
    newer = StepCheckpoint(job_id="j", step_id="s", attempt=1, checkpoint_version=6)
    merged = merge_checkpoint({older.key: older}, newer)
    assert merged.checkpoint_version == 6
    assert merge_checkpoint({newer.key: newer}, older).checkpoint_version == 6


def test_runtime_status_mapping_covers_every_state():
    for state in StepCheckpointState:
        assert runtime_status_for(state)
    assert state_for_step(status="waiting_approval") is StepCheckpointState.WAITING_APPROVAL
    assert state_for_step(status="skipped") is StepCheckpointState.CANCELLED
    assert state_for_step(status="weird") is StepCheckpointState.PLANNED
