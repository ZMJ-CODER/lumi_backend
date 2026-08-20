import asyncio

from app.agents.orchestration.models import Job, JobStatus, TaskNode, TaskStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.planner import Planner, TaskTree
from app.agents.orchestration.review import NoopReviewer
from app.agents.orchestration.state import InMemoryStateStore
from app.agents.orchestration.tca import ComplexityLevel
from app.agents.orchestration.validation import FailureCategory, validate_job_outcome


class LevelPlanner(Planner):
    def __init__(self):
        self.calls = []

    async def plan(self, *args, **kwargs):
        return TaskTree(nodes=[])

    async def plan_for_level(self, level, *args, **kwargs):
        self.calls.append(
            {
                "level": ComplexityLevel(level),
                "prior_summaries": args[8] if len(args) > 8 else kwargs.get("prior_summaries", ""),
                "bypass_fast_paths": kwargs.get("bypass_fast_paths"),
            }
        )
        return TaskTree(nodes=[TaskNode(id="replacement", agent="worker")])


def _failed_job(error_code="CAPABILITY_UNAVAILABLE"):
    return Job(
        job_id="j1",
        user_id="u1",
        request="处理文档",
        status=JobStatus.FAILED,
        routing={"level": "m0", "upgrade_count": 0, "replan_count": 0},
        nodes=[
            TaskNode(
                id="failed",
                agent="worker",
                status=TaskStatus.FAILED,
                error="当前方法不可用",
                error_code=error_code,
            )
        ],
    )


def test_capability_failure_requests_level_upgrade():
    outcome = validate_job_outcome(_failed_job())
    assert outcome.category == FailureCategory.CAPABILITY
    assert outcome.may_upgrade is True


def test_parameter_error_does_not_blindly_upgrade():
    outcome = validate_job_outcome(_failed_job("INVALID_ARGS"))
    assert outcome.category == FailureCategory.PARAMETER
    assert outcome.may_upgrade is False


def test_m0_conversion_requires_expected_artifact():
    job = Job(
        job_id="j2",
        user_id="u1",
        request="把 scores.csv 转为 txt",
        status=JobStatus.COMPLETED,
        routing={"level": "m0"},
        nodes=[
            TaskNode(
                id="convert",
                agent="office_script",
                status=TaskStatus.COMPLETED,
                params={"conversion": {"output_filename": "scores.txt"}},
                result={"success": True, "outputs": []},
            )
        ],
    )
    outcome = validate_job_outcome(job)
    assert outcome.valid is False
    assert outcome.category == FailureCategory.VALIDATION
    assert outcome.may_upgrade is False


def test_m0_output_delivery_failure_does_not_replan():
    async def scenario():
        store = InMemoryStateStore()
        planner = LevelPlanner()
        orchestrator = AgentOrchestrator(
            store=store, planner=planner, workers={"office_script": object()},
            review=NoopReviewer(), temporal_enabled=False,
        )
        job = Job(
            job_id="m0-output-missing", user_id="u1", request="把 scores.csv 转为 txt",
            status=JobStatus.FAILED, routing={"level": "m0"},
            nodes=[TaskNode(id="convert", agent="office_script", status=TaskStatus.FAILED,
                            error="脚本已结束，但未生成预期文件：scores.txt", error_code="OUTPUT_MISSING")],
        )
        await store.create_job(job)
        changed = await orchestrator._maybe_replan_failed_job(job, None)
        return changed, await store.get_job(job.job_id), planner.calls

    changed, saved, calls = asyncio.run(scenario())
    assert changed is False
    assert saved.status == JobStatus.FAILED
    assert saved.routing["automatic_replan_blocked"] == "non_replanable_validation_failure"
    assert calls == []


def test_legacy_job_replans_with_different_method():
    async def scenario():
        store = InMemoryStateStore()
        planner = LevelPlanner()
        orchestrator = AgentOrchestrator(
            store=store,
            planner=planner,
            workers={"worker": object()},
            review=NoopReviewer(),
            temporal_enabled=False,
        )
        job = _failed_job()
        job.plan_text = "先使用原方法处理文档"
        job.nodes.insert(
            0,
            TaskNode(
                id="completed",
                name="定位目标文件",
                agent="worker",
                status=TaskStatus.COMPLETED,
                result={"content": "已定位 scores.csv", "outputs": [{"name": "scores.csv"}]},
            ),
        )
        job.nodes[1].params["preferred_tool"] = "office_doc_read"
        await store.create_job(job)
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
        saved = await store.get_job(job.job_id)
        return changed, saved, planner.calls

    changed, saved, calls = asyncio.run(scenario())
    assert changed is True
    assert calls[0]["level"] == ComplexityLevel.M2
    assert calls[0]["bypass_fast_paths"] is True
    assert '"step": "定位目标文件"' in calls[0]["prior_summaries"]
    assert '"method": "office_doc_read"' in calls[0]["prior_summaries"]
    assert saved.status == JobStatus.RUNNING
    assert saved.routing["level"] == "m2"
    assert saved.routing["upgrades"][0]["reason"] == "capability_error"
    assert saved.routing["plan_revision"] == 2
    assert saved.routing["plan_history"][0]["plan_text"] == "先使用原方法处理文档"
    assert saved.nodes[0].metadata["plan_revision"] == 2


def test_effectful_job_is_not_replayed():
    async def scenario():
        store = InMemoryStateStore()
        planner = LevelPlanner()
        orchestrator = AgentOrchestrator(
            store=store,
            planner=planner,
            workers={"worker": object()},
            review=NoopReviewer(),
            temporal_enabled=False,
        )
        job = _failed_job()
        job.nodes[0].idempotency_key = "effect-key"
        await store.create_job(job)
        changed = await orchestrator._maybe_replan_failed_job(job, None)
        return changed, await store.get_job(job.job_id), planner.calls

    changed, saved, calls = asyncio.run(scenario())
    assert changed is False
    assert saved.routing["automatic_replan_blocked"] == "effectful_task"
    assert calls == []
