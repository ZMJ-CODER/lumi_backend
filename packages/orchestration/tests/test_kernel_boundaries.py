"""内核包边界回归：编排语义在 lumi_orch，单步执行契约/驱动在 lumi_execution。

架构断言：
  - lumi_orch 不得导入 app.*（业务层）与 lumi_execution 之外的执行实现；
  - lumi_execution 不得导入 app.*，也不得反向依赖 lumi_orch。
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from pydantic import ValidationError

from lumi_execution import step_engine, step_resume
from lumi_orch import execution_mode, execution_policy, protocol, run_view, step_sequence, task_profile

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "lumi_orch"
EXEC_ROOT = pathlib.Path(__file__).resolve().parents[2] / "execution" / "src" / "lumi_execution"
APP_PREFIX = "app."


def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_kernel_packages_do_not_import_application():
    offenders: list[str] = []
    for root in (PKG_ROOT, EXEC_ROOT):
        for file in root.rglob("*.py"):
            for module in _imported_modules(file):
                if module.startswith(APP_PREFIX):
                    offenders.append(f"{file.name}: {module}")
    assert offenders == []


def test_execution_package_does_not_depend_on_orchestration_policy():
    """执行内核仅可依赖共享规格（job_spec/dag），不得依赖编排策略/状态/视图模块。"""
    allowed = {"lumi_orch.job_spec", "lumi_orch.dag"}
    offenders: list[str] = []
    for file in EXEC_ROOT.rglob("*.py"):
        for module in _imported_modules(file):
            if module.startswith("lumi_orch") and module not in allowed:
                offenders.append(f"{file.name}: {module}")
    assert offenders == []


def test_execution_policy_mapping_is_kernel_owned():
    assert execution_policy.execution_policy_for_profile({
        "goal": "ANSWER",
        "required_sources": ["USER_INPUT"],
        "complexity": "ATOMIC",
        "safety_level": "READ_ONLY",
    }) == execution_policy.POLICY_DIRECT_STREAM
    assert execution_policy.execution_policy_for_profile({
        "goal": "RETRIEVE",
        "required_sources": ["WORKSPACE_READ"],
        "complexity": "ATOMIC",
        "safety_level": "READ_ONLY",
    }) == execution_policy.POLICY_SINGLE_TOOL_THEN_STREAM
    assert execution_policy.execution_policy_for_profile({
        "goal": "EXECUTE",
        "required_sources": ["USER_INPUT"],
        "complexity": "DYNAMIC",
        "safety_level": "SAFE_WRITE",
    }) == execution_policy.POLICY_REACT


def test_step_engine_locate_and_transitions():
    state = step_engine.StepRunState(
        job_id="j1",
        user_id="u1",
        canonical="waiting_run",
        current_step_index=0,
        steps=[
            {"id": "s1", "title": "第一步", "status": "pending"},
            {"id": "s2", "title": "第二步", "status": "pending", "dependencies_done": False},
        ],
    )
    candidate = step_engine.locate_next_step(state)
    assert candidate is not None and candidate.step_id == "s1"

    step_engine.mark_running(state, candidate)
    assert state.canonical == "running_step"
    assert state.steps[0]["status"] == "running"

    step_engine.apply_step_fields(state, 0, status="completed", result_summary="完成")
    waiting = step_engine.settle_success(state, candidate)
    assert state.canonical == "waiting_next"
    assert waiting["completed_step_id"] == "s1"
    assert waiting["next_step_id"] == "s2"

    # 最后一步完成 → completed
    candidate2 = step_engine.locate_next_step(state)
    assert candidate2 is not None and candidate2.step_id == "s2"
    step_engine.mark_running(state, candidate2)
    step_engine.apply_step_fields(state, 1, status="completed")
    assert step_engine.settle_success(state, candidate2) == {}
    assert state.canonical == "completed"

    # 失败与回滚
    step_engine.settle_failure(state, candidate, "boom", "STEP_FAILED")
    assert state.canonical == "failed" and state.job_status == "failed"
    step_engine.revert_to_waiting(state, 0)
    assert state.canonical == "waiting_run" and state.steps[0]["status"] == "pending"


def test_step_resume_validation_is_kernel_owned():
    allowed = step_resume.validate_resume_request(step_resume.ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_next",
        current_step_id="s2", expected_step_id="s2",
        plan_revision=2, current_revision=2, idempotency_key="k1",
    ))
    assert allowed.allowed is True
    denied = step_resume.validate_resume_request(step_resume.ResumeCheckInput(
        user_id="u1", job_owner="u1", job_state="waiting_next",
        current_step_id="s2", expected_step_id="s1",
        plan_revision=2, current_revision=2, idempotency_key="k1",
    ))
    assert denied.allowed is False and denied.error_code == step_resume.RESUME_ERROR_STEP_MISMATCH


def test_step_sequence_patch_is_kernel_owned():
    result = step_sequence.apply_step_patch(
        {"steps": [{"id": "s1", "title": "一", "status": "pending"}], "plan_revision": 1},
        step_sequence.StepPatch(base_revision=1, reason="补充步骤", steps=[
            {"id": "s2", "title": "二"},
        ]),
    )
    assert result["ok"] is True
    assert [s["id"] for s in result["steps"]] == ["s1", "s2"]
    assert result["plan_revision"] == 2


def test_protocol_stripper_is_kernel_owned():
    stripper = protocol.TextToolStripper()
    pieces: list[str] = []
    for chunk in [
        "我来读取这份 PPT 的内容。\n",
        '<workspace_read path="a.pptx">',
        "内容",
        "</workspace_read>\n",
        "总结如下。",
    ]:
        pieces.extend(stripper.feed(chunk))
    pieces.extend(stripper.flush())
    joined = "".join(pieces)
    assert "<" not in joined
    assert "workspace_read" not in joined
    assert "总结如下。" in joined


def test_run_view_and_payloads_are_kernel_owned():
    view = run_view.run_view({
        "job_id": "j1",
        "status": "pending",
        "routing": {
            "execution_mode": "step_confirm",
            "execution_state": "waiting_next",
            "plan_revision": 2,
            "current_step_index": 1,
            "steps": [{"id": "s1", "title": "一", "status": "completed"}],
        },
    })
    assert view["status"] == "waiting_next"
    assert view["next_action"] == "run_next"
    payload = run_view.waiting_next_payload(
        job_id="j1", view=view, completed_step_id="s1", next_step_id=""
    )
    assert payload["button_label"] == "运行下一步"
    assert payload["run_view"] is view
    assert execution_mode.SSE_EVENT_WAITING_NEXT == "waiting_next"
    assert run_view.next_action_for_state("waiting_approval") == "wait_approval"


def test_task_profile_contract_is_kernel_owned():
    profile = task_profile.TaskProfile(
        goal="ANSWER",
        required_sources=["ATTACHED_FILE", "WORKSPACE_READ"],
        complexity="ATOMIC",
        safety_level="READ_ONLY",
    )
    assert profile.has_side_effect is False
    assert profile.needs_runtime_decision is False
    assert set(task_profile.SAFETY_ORDER) >= {"READ_ONLY", "CRITICAL"}
    with pytest.raises(ValidationError):
        task_profile.TaskProfile(goal="NOT_A_GOAL")
