"""统一 SSE 载荷契约锁定（方案 #4 + 前端 RunNextEvent 契约）。

只测纯载荷构建层（job_run_view / execution_mode 常量），不依赖运行时：
  - 每个事件都必须带 type、job_id；
  - done 携带完整 run_view 与最终内容/元数据；
  - waiting_next 带 completed_step_id / next_step_id / next_step_index /
    plan_revision / button_label / run_view；
  - step_index 从 0 固定、plan_revision 为整数；
  - 终态事件后由调用方补发 done（这里校验 done 本身形状）。
"""

from __future__ import annotations

from lumi_orch.execution_mode import SSE_EVENTS
from lumi_orch.run_view import (
    done_payload,
    plan_ready_payload,
    run_view,
    waiting_next_payload,
)


def _view(**overrides) -> dict:
    view = run_view(_job(), **overrides)
    return view


def _job():
    from app.agents.orchestration.models import Job

    return Job(
        job_id="job-view-1",
        user_id="u1",
        request="测试",
        scene="office",
        routing={
            "execution_mode": "step_confirm",
            "execution_state": "waiting_next",
            "plan_revision": 3,
            "current_step_index": 1,
            "steps": [
                {"id": "s1", "title": "读取", "status": "completed", "result_ref": None},
                {"id": "s2", "title": "生成报告", "status": "pending", "result_ref": None},
            ],
            "plan_text": "计划",
        },
    )


def test_run_view_carries_full_contract_fields():
    view = _view()
    assert view["job_id"] == "job-view-1"
    assert view["status"] == "waiting_next"
    assert view["plan_revision"] == 3
    assert view["current_step_index"] == 1
    assert view["next_action"] == "run_next"
    assert view["execution_mode"] == "step_confirm"
    # 步骤 index 从 0 固定，步骤带契约字段
    assert [step["index"] for step in view["steps"]] == [0, 1]
    assert view["steps"][0]["id"] == "s1"
    assert "final_answer" in view and "updated_at" in view


def test_done_payload_has_job_id_status_and_run_view():
    done = done_payload(job_id="job-view-1", view=_view(), content="最终答案")
    assert done["type"] == "done"
    assert done["job_id"] == "job-view-1"
    assert done["status"] == "waiting_next"
    assert done["content"] == "最终答案"
    assert done["run_view"]["next_action"] == "run_next"
    assert isinstance(done["plan_revision"], int)


def test_waiting_next_payload_contract():
    view = _view()
    payload = waiting_next_payload(
        job_id="job-view-1",
        view=view,
        completed_step_id="s1",
        next_step_id="s2",
    )
    assert payload["job_id"] == "job-view-1"
    assert payload["status"] == "waiting_next"
    assert payload["completed_step_id"] == "s1"
    assert payload["next_step_id"] == "s2"
    assert payload["next_step_index"] == 1
    assert payload["button_label"] == "运行下一步"
    assert isinstance(payload["plan_revision"], int)
    assert payload["run_view"] is view


def test_plan_ready_payload_contract():
    view = _view(status_override="waiting_run")
    payload = plan_ready_payload(job_id="job-view-1", view=view)
    assert payload["job_id"] == "job-view-1"
    assert payload["status"] == "waiting_run"
    assert payload["run_view"] is view


def test_sse_event_names_are_shared_contract():
    # 事件类型常量（前端按这些字符串消费）
    required = {
        "plan_ready", "step_started", "process", "tool_started", "tool_completed",
        "step_completed", "waiting_next", "waiting_approval", "task_completed",
        "task_failed", "done",
    }
    assert required <= SSE_EVENTS
    from lumi_orch.execution_mode import SSE_EVENT_DONE

    assert SSE_EVENT_DONE == "done"
