"""步骤序列 PlanPatch 与精简单步上下文的回归测试（纯逻辑层）。"""

from __future__ import annotations

from lumi_orch.step_sequence import (
    StepPatch,
    apply_step_patch,
    step_execution_context,
)


def _base_state(steps, revision=1):
    return {"steps": list(steps), "plan_revision": revision}


def test_append_steps_and_bump_revision():
    state = _base_state([
        {"id": "s1", "title": "读取", "status": "completed", "result_ref": "r1"},
        {"id": "s2", "title": "提取", "status": "pending"},
    ])
    outcome = apply_step_patch(
        state,
        StepPatch(base_revision=1, reason="补充验证步骤", insert_after=None,
                  steps=[{"id": "s3", "title": "验证", "status": "pending"}]),
        execution_mode="step_confirm",
    )
    assert outcome["ok"] is True
    assert [s["id"] for s in outcome["steps"]] == ["s1", "s2", "s3"]
    assert outcome["plan_revision"] == 2
    assert outcome["should_display"] is True


def test_insert_after_target():
    state = _base_state([
        {"id": "s1", "title": "读取", "status": "completed"},
        {"id": "s2", "title": "提交", "status": "pending"},
    ])
    outcome = apply_step_patch(
        state,
        StepPatch(base_revision=1, reason="拆出 diff 查看",
                  insert_after="s1",
                  steps=[{"id": "s1b", "title": "查看差异", "status": "pending"}]),
        execution_mode="auto_routine",
    )
    assert outcome["ok"] is True
    assert [s["id"] for s in outcome["steps"]] == ["s1", "s1b", "s2"]
    assert outcome["plan_revision"] == 2
    assert outcome["should_display"] is False


def test_stale_revision_rejected():
    state = _base_state([{"id": "s1", "title": "读取", "status": "completed"}], revision=3)
    outcome = apply_step_patch(
        state, StepPatch(base_revision=2, reason="过期",
                         steps=[{"id": "s2", "title": "x"}]),
        execution_mode="step_confirm",
    )
    assert outcome["ok"] is False
    assert "过期" in outcome["error"]
    assert outcome["plan_revision"] == 3


def test_running_and_waiting_steps_are_not_anchors():
    state = _base_state([
        {"id": "s1", "title": "读取", "status": "completed"},
        {"id": "s2", "title": "运行", "status": "running"},
        {"id": "s3", "title": "审批", "status": "waiting_approval"},
    ])
    # 已完成步骤可作为锚点（新增后置步骤）
    ok = apply_step_patch(
        state, StepPatch(base_revision=1, reason="在完成后补充",
                         insert_after="s1",
                         steps=[{"id": "s1b", "title": "x"}]),
        execution_mode="step_confirm",
    )
    assert ok["ok"] is True
    # 进行中/等待审批步骤不可作为锚点
    for anchor in ("s2", "s3"):
        outcome = apply_step_patch(
            state, StepPatch(base_revision=1, reason="改",
                             insert_after=anchor,
                             steps=[{"id": f"sx_{anchor}", "title": "x"}]),
            execution_mode="step_confirm",
        )
        assert outcome["ok"] is False


def test_duplicate_ids_rejected():
    state = _base_state([{"id": "s1", "title": "读取", "status": "pending"}])
    outcome = apply_step_patch(
        state, StepPatch(base_revision=1, reason="冲突",
                         steps=[{"id": "s1", "title": "重复"}]),
        execution_mode="step_confirm",
    )
    assert outcome["ok"] is False


def test_step_execution_context_is_slim():
    step = {"id": "step_1", "title": "读取并解析", "description": "提取幻灯片", "domain": "workspace"}
    ctx = step_execution_context(
        user_request="梳理 PPT 要点",
        step=step,
        prior_results=[{"step_id": "s0", "summary": "已读取目录"}],
        workspace_summary="工作区摘要",
        skill_prompt="提示词",
        allowed_tools=["mcp__lumi_client__workspace_content_extract"],
    )
    assert ctx["step"]["id"] == "step_1"
    assert ctx["user_request"] == "梳理 PPT 要点"
    assert len(ctx["prior_results"]) == 1
    assert ctx["allowed_tools"] == ["mcp__lumi_client__workspace_content_extract"]
    # 不包含完整对话/工具历史键
    assert "messages" not in ctx
    assert "tool_history" not in ctx
