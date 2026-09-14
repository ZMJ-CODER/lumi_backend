"""app 适配层回归：TaskAssessor / InformationResolver / plan_and_route。

全部使用确定性路径（use_llm=False），无需真实模型即可验证修订版契约。
"""

from __future__ import annotations

import asyncio

from lumi_orch.execution_router import ExecutionMode
from lumi_orch.safety_policy import SafetyAction
from lumi_orch.task_assessment import TaskProfile
from lumi_orch.upgrade_policy import ContextFitStatus

from app.knowledge.information_resolver import InformationResolver, smart_slice
from app.services.task_assessor import AssessmentContext, assessor_prompt, heuristic_profile
from app.services.task_router_adapter import plan_and_route


def test_heuristic_profiles_follow_strict_schema():
    # 场景 1：纯文本整理 → M0 / 无副作用 / 纯模型（只读任务不再被低置信度抬升）
    simple = heuristic_profile(AssessmentContext(request="把这段会议记录整理成待办事项：……"))
    assert simple.complexity == "M0"
    assert simple.side_effects == []
    assert simple.execution_target in {"NONE", "BACKEND"}

    # 场景 2：工作区只读 → M1（多来源不再自动抬到 M2，避免误编排成空计划）
    workspace = heuristic_profile(AssessmentContext(
        request="看一下项目里的 README 说了什么",
        workspace_id="w1", workspace_bound=True,
        has_conversation_memory=True,
    ))
    assert "WORKSPACE" in workspace.info_sources
    assert "CONVERSATION_MEMORY" not in workspace.info_sources  # 未回指上文
    assert workspace.complexity == "M1"
    assert workspace.side_effects == []

    # 场景 3：沙箱执行
    sandbox = heuristic_profile(AssessmentContext(request="跑一下这个 Python 脚本"))
    assert "EXECUTE" in sandbox.side_effects
    assert sandbox.execution_target == "SANDBOX"

    # 场景 4：真机写文件
    desktop = heuristic_profile(AssessmentContext(
        request="把工作区里的 config.json 端口改成 8080",
        workspace_id="w1", workspace_bound=True,
    ))
    assert "WRITE" in desktop.side_effects
    assert desktop.execution_target == "DESKTOP"
    assert desktop.output_target == "WORKSPACE"

    # 严格 schema：任何画像都必须能被 TaskProfile 重新校验
    for profile in (simple, workspace, sandbox, desktop):
        TaskProfile.model_validate(profile.model_dump())


def test_assessor_prompt_distinguishes_user_provided_and_workspace():
    prompt = assessor_prompt(AssessmentContext(request="读一下工作区 README"))
    assert "USER_PROVIDED" in prompt and "WORKSPACE" in prompt
    assert "禁止填写具体工具名" in prompt
    assert "side_effects" in prompt and "output_target" in prompt


def test_plan_and_route_scenarios_without_llm():
    async def scenario():
        # 场景 1 → DIRECT_CHAT
        routed = await plan_and_route(
            request="把这句话改得更正式一些。",
            context=AssessmentContext(request="把这句话改得更正式一些。"),
            use_llm=False,
        )
        assert routed.decision.mode == ExecutionMode.DIRECT_CHAT
        assert routed.safety_action == SafetyAction.ALLOW
        assert routed.meta()["policy_version"] == "router_v2"

        # 场景 2 → M1_ATOMIC_READ（工作区）
        routed2 = await plan_and_route(
            request="看一下项目里的 README 说了什么",
            context=AssessmentContext(
                request="看一下项目里的 README 说了什么",
                workspace_id="w1", workspace_bound=True,
            ),
            use_llm=False,
        )
        assert routed2.decision.mode == ExecutionMode.M1_ATOMIC_READ

        # 场景 4 → 需要用户审批（真机写）
        routed4 = await plan_and_route(
            request="把工作区里的 config.json 端口改成 8080",
            context=AssessmentContext(
                request="把工作区里的 config.json 端口改成 8080",
                workspace_id="w1", workspace_bound=True,
            ),
            use_llm=False,
        )
        assert routed4.safety_action in {SafetyAction.REQUIRE_USER_APPROVAL, SafetyAction.REQUIRE_ADMIN_APPROVAL}

        # 场景 3 → 沙箱侧副作用不拦截
        routed3 = await plan_and_route(
            request="跑一下这个 Python 脚本",
            context=AssessmentContext(request="跑一下这个 Python 脚本"),
            use_llm=False,
        )
        assert routed3.decision.mode in {
            ExecutionMode.M1_ATOMIC_ACTION, ExecutionMode.SEQUENTIAL_WORKFLOW,
        }
        assert not routed3.blocked

        # 依赖缺失 → 拦截
        blocked = await plan_and_route(
            request="读取工作区里的配置",
            context=AssessmentContext(request="读取工作区里的配置", workspace_id=""),
            use_llm=False,
            workspace_bound=False,
        )
        if "WORKSPACE" in blocked.profile.info_sources:
            assert blocked.blocked and "工作区" in blocked.blocked_reason

    asyncio.run(scenario())


def test_assessor_llm_payload_is_strictly_sanitized_and_degraded(monkeypatch):
    """LLM 返回脏数据（未知字段/单值写成数组/低置信度）时的严格化与保守降级。"""
    import app.services.task_assessor as assessor

    async def fake_invoke(prompt, **kwargs):
        assert "USER_PROVIDED" in prompt and "WORKSPACE" in prompt
        return {
            "complexity": "M1",
            "confidence": 0.2,
            "intent_type": "EXECUTE_ACTION",
            "side_effects": ["WRITE"],
            "info_sources": 'WORKSPACE',            # 字符串 → 单元素数组
            "output_target": ["WORKSPACE", "CHAT"],  # 数组 → 首值
            "execution_target": "DESKTOP",
            "required_capabilities": "WORKSPACE_MANIPULATION",
            "path_determinism": "KNOWN",
            "risk_level": "REVERSIBLE",
            "data_sensitivity": "NORMAL",
            "context_size_estimate": "SMALL",
            "unknown_field": "should be dropped",
        }

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", fake_invoke)
    profile, source = asyncio.run(assessor.assess_task_profile(
        AssessmentContext(request="把工作区里的 config.json 端口改成 8080", workspace_id="w1"),
        use_llm=True,
    ))
    assert source == "llm"
    # 数组/单值混用被规范化
    assert profile.output_target == "WORKSPACE"
    assert profile.info_sources == ["WORKSPACE"]
    assert profile.required_capabilities == ["WORKSPACE_MANIPULATION"]
    # 低置信度 → 保守降级（复杂度≥M2、路径 UNKNOWN、风险≥REQUIRES_APPROVAL）
    assert profile.complexity in {"M2", "M3"}
    assert profile.path_determinism == "UNKNOWN"
    assert profile.risk_level in {"REQUIRES_APPROVAL", "HIGH_RISK"}
    # 未知字段被 schema 丢弃
    assert "unknown_field" not in profile.model_dump()


def test_assessor_llm_failure_falls_back_to_heuristic(monkeypatch):
    import app.services.task_assessor as assessor

    async def boom(prompt, **kwargs):
        raise RuntimeError("model down")

    monkeypatch.setattr("app.agents.langchain.planning.invoke_json_object", boom)
    profile, source = asyncio.run(assessor.assess_task_profile(
        AssessmentContext(request="看一下项目里的 README 说了什么", workspace_id="w1"),
        use_llm=True,
    ))
    assert source == "heuristic"
    assert "WORKSPACE" in profile.info_sources


def test_information_resolver_adapters_and_slicing():
    calls: list[str] = []

    async def workspace_reader(query: str) -> str:
        calls.append("workspace")
        return "README 内容：" + "很长" * 3000

    async def web_searcher(query: str) -> str:
        calls.append("web")
        return "公开资料片段"

    async def failing_service(query: str) -> str:
        raise RuntimeError("service down")

    resolver = InformationResolver(
        workspace_reader=workspace_reader,
        web_searcher=web_searcher,
        service_querier=failing_service,
        total_size_limit=2000,
    )

    async def scenario():
        resolved = await resolver.resolve(["USER_PROVIDED", "WORKSPACE"], "README 说了什么")
        assert "workspace" in calls
        # USER_PROVIDED 已在上下文：不触发外部调用
        assert resolved.per_source_chars["USER_PROVIDED"] == 0
        # 超长 → SLICED（绝不抛 CONTEXT_TOO_LARGE）
        assert resolved.status == ContextFitStatus.SLICED
        assert len(resolved.text) <= 2000 + 200

        multi = await resolver.resolve(
            ["WORKSPACE"], "总结整本书", requires_cross_segment_analysis=True,
        )
        assert multi.status == ContextFitStatus.MULTI_STEP_REQUIRED
        assert multi.needs_multi_step

        # 适配器异常 → 记录 error 但不中断
        degraded = await resolver.resolve(["PRIVATE_SERVICE"], "查一下邮箱")
        assert any(record["status"].startswith("error") for record in degraded.records)

    asyncio.run(scenario())


def test_smart_slice_keeps_query_relevant_part():
    text = "前言" * 500 + "KEYWORD_SECTION 关键内容在此" + "后记" * 500
    sliced = smart_slice(text, "KEYWORD_SECTION", keep_chars=200)
    assert "KEYWORD_SECTION" in sliced
    assert len(sliced) <= 220
