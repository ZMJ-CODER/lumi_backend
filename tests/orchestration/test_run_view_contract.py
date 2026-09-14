"""阶段四回归：运行视图（JobRunView）契约校验。

``GET /agents/jobs/{id}`` 的 ``run_view`` 是前端刷新后恢复的唯一数据源。
这里断言：正常快照能通过校验并映射成契约对象；形状漂移会被**报出来**，
而不是等到前端白屏。
"""

from __future__ import annotations

from lumi_contracts import JobRunView, RunState
from lumi_orch.run_view import run_view

from app.agents.orchestration.models import Job
from app.contracts.run_view import run_view_problems, to_job_run_view


def _job(**routing_overrides) -> Job:
    routing = {
        "execution_state": "completed",
        "plan_revision": 2,
        "current_step_index": 1,
        "steps": [
            {"id": "n1", "title": "读取 README", "status": "completed"},
            {"id": "n2", "title": "总结", "status": "completed"},
        ],
        "plan_text": "1. 读取 2. 总结",
    }
    routing.update(routing_overrides)
    return Job(
        job_id="job-1",
        user_id="u1",
        request="读一下 README",
        scene="office",
        status="completed",
        routing=routing,
        result={"final_answer": "回答正文"},
    )


def test_kernel_run_view_passes_the_contract_check():
    view = run_view(_job())
    assert run_view_problems(view, expected_job_id="job-1") == []


def test_run_view_maps_to_contract_object():
    view = run_view(_job())
    contract = to_job_run_view(view, conversation_id="c1", routing={"execution_state": "completed"})
    assert isinstance(contract, JobRunView)
    assert contract.job_id == "job-1"
    assert contract.conversation_id == "c1"
    assert contract.status is RunState.COMPLETED
    assert contract.is_terminal is True
    assert contract.plan_revision == 2
    assert [step.id for step in contract.steps] == ["n1", "n2"]
    assert contract.final_answer == "回答正文"
    assert contract.next_action == view["next_action"]


def test_waiting_next_snapshot_is_not_terminal():
    contract = to_job_run_view(run_view(_job(execution_state="waiting_next")))
    assert contract.status is RunState.WAITING_NEXT
    assert contract.is_terminal is False
    assert contract.next_action == "run_next"


def test_unknown_status_and_missing_ids_are_reported():
    broken = {
        "job_id": "job-1",
        "status": "some_new_state",
        "next_action": "",
        "steps": [{"title": "没有 id"}],
    }
    problems = run_view_problems(broken, expected_job_id="job-1")
    assert any("未知运行状态" in item for item in problems)
    assert any("next_action" in item for item in problems)
    assert any("缺少 id" in item for item in problems)


def test_job_id_mismatch_and_non_object_view_are_reported():
    view = run_view(_job())
    problems = run_view_problems(view, expected_job_id="job-2")
    assert any("不一致" in item for item in problems)
    assert run_view_problems(None) == ["run_view 不是对象"]


def test_unknown_status_does_not_crash_projection():
    contract = to_job_run_view({"job_id": "j", "status": "weird", "steps": "not-a-list"})
    # 契约模型不接受任意状态字符串：折叠为 PENDING，但原值由校验函数报出。
    assert contract.status is RunState.PENDING
    assert contract.steps == []
