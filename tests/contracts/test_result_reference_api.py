"""结果引用接口与"不确定状态不被吞掉"的回归（对齐前端 §1.3 / §2.2 / §7.2）。

三条前端依赖的后端契约：

1. ``GET /agents/jobs/{id}/results/{result_id}``：正文按需加载、越权 403、
   过期 410 + ``data.error_code=RESULT_REF_EXPIRED``、不存在 404（统一错误体，
   与"接口未上线"的 FastAPI 默认 ``{"detail": ...}`` 区分）；
2. ``GET /agents/jobs/{id}/steps``：分页 + ``display_summary`` + 最小 ``result_ref``；
3. ``uncertain`` 在**实时与刷新两条路径**上都必须原样保留（此前会被兜底成
   completed / pending，导致验收场景在 UI 上消失）。
"""

from __future__ import annotations

import asyncio

import pytest

from lumi_contracts import ProcessLogEntry, ProcessStatus
from app.agents.orchestration.models import Job, JobStatus, TaskNode
from app.api.v1 import agents as agents_api
from app.contracts.process_log import _process_status, process_log_from_job
from app.core.exceptions import AppException, NotFoundException
from app.services import result_store as store_module
from app.services.result_store import LocalBlobPort, SaveResultRequest, set_result_store_for_tests
from app.services.step_checkpoint import (
    InMemoryCheckpointStore,
    StepCheckpointCoordinator,
    set_coordinator_for_tests,
)
from tests.contracts.test_result_store import _MemoryKv


def _job(job_id: str = "ref-job-1", *, user_id: str = "u-owner") -> Job:
    return Job(
        job_id=job_id,
        user_id=user_id,
        request="读一个结果",
        scene="office",
        status=JobStatus.COMPLETED,
        nodes=[TaskNode(id="s1", name="步骤 s1", agent="w1", status="completed")],
        routing={"execution_state": "completed", "plan_revision": 1, "steps": []},
    )


class _OrchestratorStub:
    def __init__(self, job: Job | None) -> None:
        self._job = job

    async def get_job(self, job_id: str):
        return self._job if self._job is not None and self._job.job_id == job_id else None


@pytest.fixture()
def env(monkeypatch):
    """受控 ResultStore + 任务桩（不依赖 Redis/DB）。"""
    instance = store_module.ResultStore(
        kv=_MemoryKv(), local_blob=LocalBlobPort(".ptmp/result-ref-api")
    )
    previous = store_module.get_result_store()
    set_result_store_for_tests(instance)
    job = _job()
    monkeypatch.setattr(agents_api, "orchestrator", _OrchestratorStub(job))
    try:
        yield instance, job
    finally:
        set_result_store_for_tests(previous)


def _save(instance, job: Job, *, ttl_seconds: int = 3600):
    receipt = asyncio.run(
        instance.save(
            SaveResultRequest(
                result={"success": True, "content": "结果正文", "items": [{"index": i} for i in range(12)]},
                user_id=job.user_id,
                job_id=job.job_id,
                step_id="s1",
                ttl_seconds=ttl_seconds,
            )
        )
    )
    assert receipt is not None
    return receipt


def _fetch_result(job: Job, result_id: str, **overrides) -> dict:
    """直接调用路由函数（绕开 FastAPI 注入，故所有 Query 默认值显式给出）。

    参数名与前端 ``resultRefs.js`` 逐字一致：``mode`` / ``offset`` / ``limit`` /
    ``max_chars`` / ``fields``。
    """
    params = {
        "mode": "full",
        "offset": 0,
        "limit": 0,
        "max_chars": 0,
        "fields": "",
        "payload": {"sub": job.user_id},
    }
    params.update(overrides)
    payload = params.pop("payload")
    return asyncio.run(agents_api.get_agent_job_result(job.job_id, result_id, payload=payload, **params))


def _fetch_steps(job: Job, **overrides) -> dict:
    params = {"offset": 0, "limit": 50}
    params.update(overrides)
    payload = params.pop("payload", {"sub": job.user_id})
    return asyncio.run(agents_api.list_agent_job_steps(job.job_id, payload=payload, **params))


# ── /results/{result_id} ────────────────────────────────────


def test_result_endpoint_returns_body_and_metadata_by_reference(env):
    instance, job = env
    receipt = _save(instance, job)
    payload = _fetch_result(job, receipt.ref.id)["data"]
    assert payload["result_id"] == receipt.ref.id
    assert payload["sha256"] == receipt.ref.sha256
    assert payload["content"] == "结果正文"
    assert payload["truncated"] is False
    assert payload["schema_version"] == 1


def test_result_endpoint_honours_frontend_budget_and_paging(env):
    instance, job = env
    receipt = _save(instance, job)
    payload = _fetch_result(job, receipt.ref.id, limit=5, offset=0, fields="items")["data"]
    assert len(payload["items"]) == 5
    assert payload["truncated"] is True
    # 分页元数据必须回传：前端据此决定要不要继续翻页（否则分页信息会被丢掉）。
    assert payload["limit"] == 5 and payload["offset"] == 0
    assert payload["total"] == 12


def test_result_endpoint_supports_summary_mode(env):
    instance, job = env
    receipt = _save(instance, job)
    payload = _fetch_result(job, receipt.ref.id, mode="summary")["data"]
    assert payload["truncated"] is True
    assert "items" not in payload


def test_expired_reference_is_gone_with_explicit_error_code(env):
    instance, job = env
    receipt = _save(instance, job, ttl_seconds=1)
    instance._clock = lambda: receipt.ref.created_at + 10  # noqa: SLF001 - 可控时钟
    with pytest.raises(AppException) as excinfo:
        _fetch_result(job, receipt.ref.id)
    assert excinfo.value.status_code == 410
    assert excinfo.value.error_code == "RESULT_REF_EXPIRED"
    assert excinfo.value.data["error_code"] == "RESULT_REF_EXPIRED"


def test_missing_result_is_404_with_unified_error_body(env):
    _instance, job = env
    with pytest.raises(AppException) as excinfo:
        _fetch_result(job, "does-not-exist")
    assert excinfo.value.status_code == 404
    assert excinfo.value.data["error_code"] == "RESULT_REF_UNAVAILABLE"


def test_other_users_result_is_not_readable(env):
    instance, job = env
    receipt = _save(instance, job)
    # 知道 result_id 也不够：接口先解析任务归属，"不是我的任务"直接 404，
    # 不会因为猜中 id 就绕过 owner 校验。
    with pytest.raises(NotFoundException):
        _fetch_result(job, receipt.ref.id, payload={"sub": "u-attacker"})


# ── /steps 分页 + 展示摘要 ───────────────────────────────────


def test_steps_endpoint_paginates_and_exposes_display_summary(env, monkeypatch):
    _instance, job = env
    store = InMemoryCheckpointStore()
    coordinator = StepCheckpointCoordinator(job_id=job.job_id, store=store, enabled=True)
    set_coordinator_for_tests(job.job_id, coordinator)
    try:
        for index in range(5):
            step_id = f"st{index}"
            asyncio.run(coordinator.record(step_id, "running"))
            asyncio.run(
                coordinator.record(
                    step_id,
                    "completed",
                    output_summary=f"第 {index} 步已完成",
                    result_ref={"id": f"r{index}", "sha256": "x"},
                )
            )
        from app.services import step_checkpoint as checkpoint_module

        monkeypatch.setattr(checkpoint_module, "default_checkpoint_store", lambda: store)
        first = _fetch_steps(job, offset=0, limit=2)["data"]
        assert first["total"] == 5 and first["has_more"] is True
        assert len(first["steps"]) == 2
        assert first["steps"][0]["display_summary"] == "第 0 步已完成"
        assert set(first["steps"][0]["result_ref"]) == {"id", "sha256"}
        assert "result_summary" not in first["steps"][0]
        second = _fetch_steps(job, offset=4, limit=2)["data"]
        assert second["has_more"] is False and len(second["steps"]) == 1
    finally:
        set_coordinator_for_tests(job.job_id, None)


# ── uncertain 不被吞掉（实时 + 刷新两条路径）────────────────


def test_uncertain_status_survives_both_live_and_refresh_paths():
    live = ProcessLogEntry.from_event(
        {"type": "step_completed", "status": "uncertain", "step_id": "write", "summary": "写文件"}
    )
    assert live.status is ProcessStatus.UNCERTAIN
    assert str(live.status) == "uncertain"

    # 刷新路径：步骤状态 uncertain → 过程条目状态必须还是 uncertain。
    assert _process_status("uncertain") is ProcessStatus.UNCERTAIN
    job = _job("uncertain-job")
    job.routing["steps"] = [
        {
            "id": "write",
            "title": "写文件",
            "status": "uncertain",
            "description": "",
            "domain": "w1",
            "error": "副作用状态不确定",
        }
    ]
    entries = process_log_from_job(job)
    row = next(item for item in entries if item.entry_id == "step:write")
    assert row.status is ProcessStatus.UNCERTAIN
    assert "不确定" in row.summary


def test_unknown_future_status_is_preserved_not_collapsed():
    """将来新增的状态（或旧别名）原样透传，绝不兜底成 completed。"""
    entry = ProcessLogEntry.from_event({"type": "step_completed", "status": "needs_review"})
    assert str(entry.status) == "needs_review"
    assert _process_status("needs_review") == "needs_review"


def test_cancelled_steps_are_not_reported_as_completed():
    job = _job("cancel-job")
    job.routing["steps"] = [
        {"id": "s", "title": "步骤", "status": "cancelled", "description": "", "domain": "w1"}
    ]
    row = next(item for item in process_log_from_job(job) if item.entry_id == "step:s")
    assert row.status is ProcessStatus.CANCELLED


def test_process_status_contract_exports_new_states():
    from lumi_contracts import ProcessStatus as ContractStatus

    assert {str(item) for item in ContractStatus} >= {
        "running", "completed", "failed", "pending", "uncertain", "cancelled", "expired",
    }
