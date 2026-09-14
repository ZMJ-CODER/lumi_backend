"""Regression tests for business-neutral office task dispatch."""

from __future__ import annotations

import asyncio

from app.agents.skills.base import WorkflowSkill
from app.agents.orchestration.planning.task_profiles import AbstractTaskNode, TaskProfile, compile_abstract_tasks


def _compile(tasks: list[AbstractTaskNode], request: str = "完成用户目标"):
    return asyncio.run(
        compile_abstract_tasks(tasks, user_id="abstract-dispatch-test", user_request=request)
    )


def test_generic_generation_stays_direct_llm_not_business_named_skill(monkeypatch):
    class BusinessWritingSkill(WorkflowSkill):
        name = "compose_business_letter"
        provided_goals = ["GENERATE"]
        provided_sources = ["USER_INPUT"]

    async def visible(_user_id):
        return [BusinessWritingSkill()]

    monkeypatch.setattr("app.services.user_workflow_skills.get_visible_workflow_skills", visible)
    nodes = _compile([
        AbstractTaskNode(
            id="draft", name="形成初稿", instruction="根据背景写一份说明",
            profile=TaskProfile(goal="GENERATE", required_sources=["USER_INPUT"]),
        )
    ])

    assert [node.agent for node in nodes] == ["direct_llm"]
    assert nodes[0].metadata["is_fallback"] is False
    assert "未安装" not in nodes[0].params["instruction"]


def test_declared_capability_binds_skill_without_request_keyword_matching(monkeypatch):
    class PublicResearch(WorkflowSkill):
        name = "public_facts_flow"
        provided_goals = ["RETRIEVE"]
        provided_sources = ["PUBLIC_WEB"]
        safety_level = "READ_ONLY"

    async def visible(_user_id):
        return [PublicResearch()]

    monkeypatch.setattr("app.services.user_workflow_skills.get_visible_workflow_skills", visible)
    nodes = _compile([
        AbstractTaskNode(
            id="facts", name="获得可验证事实", instruction="获取有关该主题的公开事实",
            profile=TaskProfile(goal="RETRIEVE", required_sources=["PUBLIC_WEB"]),
        )
    ])

    assert [node.agent for node in nodes] == ["workflow_skill"]
    assert nodes[0].params["skill_name"] == "public_facts_flow"


def test_unmatched_capability_fallback_includes_goal_upstream_and_downstream(monkeypatch):
    async def visible(_user_id):
        return []

    monkeypatch.setattr("app.services.user_workflow_skills.get_visible_workflow_skills", visible)
    nodes = _compile([
        AbstractTaskNode(
            id="read", name="读取私有材料", instruction="从受保护资料中取得事实",
            profile=TaskProfile(goal="RETRIEVE", required_sources=["ATTACHED_FILE"]),
        ),
        AbstractTaskNode(
            id="answer", name="形成结论", instruction="根据前置事实形成结论",
            profile=TaskProfile(goal="ANALYZE", required_sources=["USER_INPUT"]),
            depends_on=["read"],
        ),
    ], request="请基于我提供的材料给出结论")

    assert [node.agent for node in nodes] == ["direct_llm", "direct_llm"]
    fallback = nodes[0]
    assert fallback.metadata["is_fallback"] is True
    assert fallback.metadata["allow_missing_capability_answer"] is True
    assert fallback.metadata["critical_capability_missing"] is True
    assert "请基于我提供的材料给出结论" in fallback.params["instruction"]
    assert "形成结论" in fallback.params["instruction"]
    assert "不得伪称已访问外部来源、文件或系统" in fallback.params["instruction"]


def test_planner_contract_has_no_concrete_implementation_names():
    from app.agents.orchestration.planning.prompting import build_planner_prompt

    prompt = build_planner_prompt()
    assert "web_search" not in prompt
    assert "workflow_skill" not in prompt
    assert "preferred_tool" not in prompt
    assert "abstract_tasks" in prompt


def test_llm_planner_compiles_abstract_contract_before_legacy_routes(monkeypatch):
    from app.agents.orchestration.planning.planner import LlmPlanner

    async def structured(*_args, **_kwargs):
        return {
            "plan": "先获取公开事实，再形成建议",
            "clarification": "",
            "abstract_tasks": [
                {
                    "id": "facts", "name": "获得公开事实", "instruction": "获取公开事实",
                    "profile": {
                        "goal": "RETRIEVE", "required_sources": ["PUBLIC_WEB"],
                        "complexity": "ATOMIC", "safety_level": "READ_ONLY", "confidence": 0.9,
                    },
                    "depends_on": [], "is_critical": True,
                },
                {
                    "id": "advice", "name": "形成建议", "instruction": "基于前置事实形成建议",
                    "profile": {
                        "goal": "ANALYZE", "required_sources": ["USER_INPUT"],
                        "complexity": "ATOMIC", "safety_level": "READ_ONLY", "confidence": 0.9,
                    },
                    "depends_on": ["facts"], "is_critical": True,
                },
            ],
        }

    async def no_skills(_user_id):
        return []

    planner = LlmPlanner()
    monkeypatch.setattr(planner, "_call_structured_planner", structured)
    monkeypatch.setattr("app.services.user_workflow_skills.get_visible_workflow_skills", no_skills)
    tree = asyncio.run(planner.plan("abstract-user", "给我一份基于公开事实的建议"))

    assert [node.agent for node in tree.nodes] == ["direct_llm", "direct_llm"]
    assert tree.nodes[1].depends_on == ["facts"]
    assert tree.nodes[0].metadata["is_fallback"] is True
    assert tree.nodes[1].metadata["is_fallback"] is False
