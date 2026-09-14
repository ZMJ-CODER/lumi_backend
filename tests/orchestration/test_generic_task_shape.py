from __future__ import annotations

import asyncio

from app.agents.orchestration.planning.task_shape import assess_task_shape


def test_document_question_is_context_only():
    shape = assess_task_shape("请概括这份材料的主要结论")
    assert shape.requires_orchestration is False


def test_non_document_external_capability_enters_generic_orchestration():
    shape = assess_task_shape("帮我查一下最近的行业政策，并整理成三条建议")
    assert shape.requires_orchestration is True
    assert "external_capability" in shape.reasons


def test_user_goal_with_runtime_decision_enters_orchestration_without_business_route():
    shape = assess_task_shape("把这个项目跑起来，遇到依赖错误就处理并验证通过")
    assert shape.requires_orchestration is True
    assert "runtime_decision" in shape.reasons


def test_generic_skill_assessor_is_async_and_does_not_require_document():
    shape = asyncio.run(
        __import__("app.agents.orchestration.planning.task_shape", fromlist=["assess_task_shape_with_skills"])
        .assess_task_shape_with_skills("解释这段文字", user_id="u1")
    )
    assert shape.requires_orchestration is False
