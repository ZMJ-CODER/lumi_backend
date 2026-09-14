"""恢复流程与完整单步时序回归（方案 §2.3 / §5）。

两条主线：

1. **时序铁律**：一次真实的 ``run_next`` 单步执行里，"结果引用落盘 + Job 状态写入"
   必须发生在完成事件被消费之前，且检查点与 Job 快照分属不同键（快照不膨胀）；
2. **恢复核对**：`JobRecoveryService` 只读地给出恢复计划——``confirmed`` 跳过、
   ``pending`` 可核对（先查实际状态）、``uncertain`` / 日志不可用一律交人工。
"""

from __future__ import annotations

import asyncio

from lumi_contracts.persistence.checkpoint import StepCheckpoint, StepCheckpointState
from app.agents.orchestration.recovery.job_recovery_service import (
    JobRecoveryService,
    dependency_refs_from_checkpoints,
)
from app.agents.orchestration.models import Job, JobStatus, ResourceClaim, TaskNode, TaskStatus
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore
from app.agents.orchestration.step_run_service import EVENT_STEP_COMPLETED, StepRunService
from app.repositories.job_repository import StateStoreJobRepository
from app.services import step_checkpoint as checkpoint_module
from app.services.step_checkpoint import (
    InMemoryCheckpointStore,
    StepCheckpointCoordinator,
    set_coordinator_for_tests,
)


class _FakeWorker:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, node: TaskNode, ctx) -> dict:
        self.calls += 1
        return {"success": True, "content": "写好了", "tool": "workspace_write_file"}


class _OrderedStore(InMemoryStateStore):
    """记录 save_job 的顺序点，用来断言"完成事件晚于状态落盘"。"""

    def __init__(self) -> None:
        super().__init__()
        self.saves = 0

    async def save_job(self, job: Job) -> None:
        self.saves += 1
        await super().save_job(job)


class _Clock:
    """可推进时钟：让乱序/过期判定可控。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _build_job() -> Job:
    node = TaskNode(
        id="write",
        name="写文件",
        agent="w1",
        params={"instruction": "写文件", "preferred_tool": "workspace_write_file"},
        # 只声明读声明：让资源协调走本地协调器（测试里没有 Redis），
        # 写声明在 fail-closed 策略下会要求分布式租约，与检查点无关。
        resource_claims=[ResourceClaim(key="document:1", mode="read")],
    )
    return Job(
        job_id="recovery-job-1",
        user_id="u1",
        user_role="user",
        request="写一个文件",
        scene="office",
        status=JobStatus.PENDING,
        nodes=[node],
        routing={
            "execution_mode": "step_confirm",
            "execution_state": "waiting_run",
            "plan_revision": 1,
            "current_step_index": 0,
            "steps": [
                {
                    "id": "write",
                    "title": "写文件",
                    "description": "",
                    "domain": "w1",
                    "status": "pending",
                    "result_ref": None,
                }
            ],
        },
    )


def test_completion_event_arrives_after_state_and_checkpoint_are_persisted():
    """铁律：完成事件被消费时，Job 状态与步骤检查点**都已经落盘**。"""

    async def scenario():
        store = _OrderedStore()
        repo = StateStoreJobRepository(store)
        job = _build_job()
        await repo.create_job(job)
        checkpoint_store = InMemoryCheckpointStore()
        coordinator = StepCheckpointCoordinator(
            job_id=job.job_id, store=checkpoint_store, enabled=True
        )
        set_coordinator_for_tests(job.job_id, coordinator)
        try:
            service = StepRunService(
                store=repo,
                workers={"w1": _FakeWorker()},
                review=NoopReviewer(),
                finalizer=object(),
                llm_configs={},
                poll_interval=0.02,
            )
            seen_completed: list[dict] = []
            async for event in service.run_next_stream(
                job_id=job.job_id, idempotency_key="k1", workspace_bound=True
            ):
                if str(event.get("type")) != EVENT_STEP_COMPLETED:
                    continue
                # 事件到达时：Job 状态已保存，检查点已落盘且带 result_ref。
                assert store.saves > 0
                rows = await checkpoint_store.load(job.job_id)
                assert rows, "完成事件到达时检查点必须已经落盘"
                assert rows[0].state is StepCheckpointState.COMPLETED
                assert rows[0].result_ref and rows[0].result_ref.get("id")
                seen_completed.append(event)
            assert seen_completed, "必须真的产生过完成事件"
            # 检查点键与 Job 快照键分离：写检查点不会让快照膨胀。
            assert checkpoint_module.checkpoint_key(job.job_id) != (
                "multiagent:job_snapshot:" + job.job_id
            )
        finally:
            set_coordinator_for_tests(job.job_id, None)

    asyncio.run(scenario())


def test_recovery_service_skips_confirmed_and_pauses_uncertain():
    async def scenario():
        rows = [
            StepCheckpoint(
                job_id="j",
                step_id="done",
                status=StepCheckpointState.WAITING_APPROVAL,
                effect_status="pending",
                checkpoint_version=1,
                result_ref={"id": "r-done", "sha256": "a"},
            ),
            StepCheckpoint(
                job_id="j",
                step_id="lost",
                status=StepCheckpointState.UNCERTAIN,
                error_code="EFFECT_UNCERTAIN",
                checkpoint_version=2,
            ),
        ]

        async def journal_reader(_job_id: str):
            return (
                {
                    "k1": {"status": "confirmed", "step_id": "done", "effect_type": "file_create"},
                    "k2": {"status": "uncertain", "step_id": "lost", "effect_type": "file_delete"},
                },
                True,
            )

        service = JobRecoveryService(journal_reader=journal_reader)
        report = await service.plan(
            job_id="j",
            job_plan_revision=2,
            checkpoints=rows,
            idempotency_keys={"done": "k1", "lost": "k2"},
        )
        assert report.plan.skip_step_ids == ("done",)
        assert report.plan.paused_step_ids == ("lost",)
        assert report.resume_allowed is False
        assert report.journal_available is True

    asyncio.run(scenario())


def test_recovery_service_reconciles_pending_effect_when_verifiable():
    async def scenario():
        rows = [
            StepCheckpoint(
                job_id="j",
                step_id="write",
                status=StepCheckpointState.STARTED,
                effect_status="pending",
                effect_type="file_create",
                checkpoint_version=1,
            )
        ]

        async def journal_reader(_job_id: str):
            return (
                {"k1": {"status": "pending", "step_id": "write", "effect_type": "file_create"}},
                True,
            )

        class _Reconciler:
            async def effect_applied(self, record):
                return True  # 文件确实已经在磁盘上

        service = JobRecoveryService(journal_reader=journal_reader, reconciler=_Reconciler())
        report = await service.plan(
            job_id="j",
            job_plan_revision=1,
            checkpoints=rows,
            idempotency_keys={"write": "k1"},
        )
        assert report.reconciled_step_ids == ("write",)
        assert report.plan.skip_step_ids == ("write",), "核对到已发生 → 跳过，不重跑"
        assert report.resume_allowed is True

    asyncio.run(scenario())


def test_recovery_service_fails_closed_when_journal_unavailable():
    async def scenario():
        rows = [
            StepCheckpoint(
                job_id="j",
                step_id="write",
                status=StepCheckpointState.STARTED,
                effect_status="pending",
                checkpoint_version=1,
            )
        ]

        async def journal_reader(_job_id: str):
            return {}, False

        service = JobRecoveryService(journal_reader=journal_reader)
        report = await service.plan(
            job_id="j",
            job_plan_revision=1,
            checkpoints=rows,
            idempotency_keys={"write": "k1"},
        )
        assert report.journal_available is False
        assert report.resume_allowed is False, "日志不可用时不得把'没有记录'当'没有执行'"

    asyncio.run(scenario())


def test_dependency_refs_are_resolved_by_reference_with_explicit_errors():
    async def scenario():
        from lumi_contracts.persistence.result_store import LoadBudget
        from app.agents.orchestration.recovery.job_recovery_service import load_dependency_results
        from app.services import result_store as store_module
        from app.services.result_store import LocalBlobPort, ResultStore, SaveResultRequest
        from tests.contracts.test_result_store import _MemoryKv

        kv = _MemoryKv()
        instance = ResultStore(kv=kv, local_blob=LocalBlobPort(".ptmp/recovery-deps"))
        previous = store_module.get_result_store()
        store_module.set_result_store_for_tests(instance)
        try:
            receipt = await instance.save(
                SaveResultRequest(
                    result={"success": True, "content": "前序正文" * 50},
                    user_id="u1",
                    job_id="j",
                    step_id="prior",
                )
            )
            assert receipt is not None
            loaded = await load_dependency_results(
                {"prior": receipt.minimal_ref}, user_id="u1", budget=LoadBudget(max_chars=60)
            )
            assert loaded[0].ok is True
            assert loaded[0].truncated is True
            # 过期/不存在的引用要给明确错误码，不能静默当空上下文。
            missing = await load_dependency_results(
                {"prior": {"id": "nope", "sha256": "0" * 64}}, user_id="u1"
            )
            assert missing[0].ok is False and missing[0].error_code
        finally:
            store_module.set_result_store_for_tests(previous)

    asyncio.run(scenario())


def test_dependency_refs_from_checkpoints_keeps_only_real_refs():
    rows = [
        StepCheckpoint(job_id="j", step_id="a", result_ref={"id": "r1", "sha256": "x"}),
        StepCheckpoint(job_id="j", step_id="b"),
    ]
    assert dependency_refs_from_checkpoints(rows) == {"a": {"id": "r1", "sha256": "x"}}


def test_recovery_plan_for_job_reads_checkpoints_from_store():
    async def scenario():
        store = InMemoryCheckpointStore()
        coordinator = StepCheckpointCoordinator(job_id="j2", store=store, enabled=True)
        await coordinator.record("s1", StepCheckpointState.STARTED)
        await coordinator.record("s1", StepCheckpointState.COMPLETED)
        await coordinator.record("s2", StepCheckpointState.STARTED)
        job = Job(
            job_id="j2",
            user_id="u1",
            request="x",
            status=JobStatus.FAILED,
            nodes=[
                TaskNode(id="s1", agent="w1", status=TaskStatus.COMPLETED, idempotency_key="k1"),
                TaskNode(id="s2", agent="w1", status=TaskStatus.RUNNING, idempotency_key="k2"),
            ],
            routing={"plan_revision": 2},
        )

        async def journal_reader(_job_id: str):
            return ({}, True)

        service = JobRecoveryService(journal_reader=journal_reader)
        report = await service.plan_for_job(job, store=store)
        # s1 已完成不进未完成集合；s2 没有副作用记录 → 可重排。
        assert report.plan.reschedule_step_ids == ("s2",)
        assert report.resume_allowed is True

    asyncio.run(scenario())
