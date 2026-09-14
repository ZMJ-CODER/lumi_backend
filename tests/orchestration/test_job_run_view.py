"""Job 运行视图投影与计划式事件载荷的回归测试。"""

from __future__ import annotations

from lumi_orch.execution_mode import SSE_EVENTS
from lumi_orch.run_view import (
    canonical_state_for,
    done_payload,
    plan_ready_payload,
    run_view,
    steps_from_nodes,
)


def test_steps_from_nodes_projection():
    class Node:
        id = "n1"
        name = "读取并解析演示文稿"
        agent = "atomic_step"
        params = {"instruction": "提取幻灯片标题与正文", "step_title": "读取PPT"}

    steps = steps_from_nodes([Node(), Node()])
    assert steps == [{
        "id": "n1", "title": "读取并解析演示文稿",
        "description": "提取幻灯片标题与正文", "domain": "atomic_step",
        "status": "pending", "result_ref": None,
    }, {
        "id": "n1", "title": "读取并解析演示文稿",
        "description": "提取幻灯片标题与正文", "domain": "atomic_step",
        "status": "pending", "result_ref": None,
    }]


def test_canonical_state_from_routing_and_fallback():
    job = type("Job", (), {
        "job_id": "j1",
        "status": "running",
        "routing": {"execution_state": "waiting_next", "execution_mode": "step_confirm"},
    })()
    assert canonical_state_for(job) == "waiting_next"
    legacy = type("Job", (), {"status": "waiting_approval", "routing": {}})()
    assert canonical_state_for(legacy) == "waiting_approval"


def test_run_view_projection_shape():
    job = type("Job", (), {
        "job_id": "j1",
        "status": "pending",
        "routing": {
            "execution_state": "waiting_run",
            "execution_mode": "step_confirm",
            "plan_revision": 2,
            "current_step_index": 0,
            "steps": [{"id": "s1", "title": "读取并解析", "domain": "workspace", "status": "pending"}],
            "plan_text": "先读取演示文稿",
        },
    })()
    view = run_view(job)
    assert view["job_id"] == "j1"
    assert view["execution_mode"] == "step_confirm"
    assert view["status"] == "waiting_run"
    assert view["plan_revision"] == 2
    assert view["steps"][0]["id"] == "s1"
    assert view["plan_text"] == "先读取演示文稿"
    assert view["task_completed"] is False


def test_plan_ready_and_done_payloads():
    view = {
        "execution_mode": "step_confirm", "status": "waiting_run", "plan_revision": 1,
        "steps": [], "plan_text": "计划文本", "dsml_pending": False,
    }
    ready = plan_ready_payload(job_id="job_1", view=view)
    assert ready["job_id"] == "job_1"
    assert ready["execution_mode"] == "step_confirm"
    assert ready["status"] == "waiting_run"
    done = done_payload(job_id="job_1", view=view, message_id="msg_1", content="完成")
    assert done["type"] == "done"
    assert done["job_id"] == "job_1"
    assert done["job_status"] == "waiting_run"
    assert done["plan_text"] == "计划文本"
    assert done["dsml_pending"] is False


def test_sse_event_contract_includes_task_events():
    assert {"task_completed", "task_failed", "waiting_approval", "waiting_next"} <= SSE_EVENTS
