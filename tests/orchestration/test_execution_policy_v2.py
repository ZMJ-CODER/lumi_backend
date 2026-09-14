"""v2 统一任务画像 / 执行策略映射层回归测试。"""

from __future__ import annotations

from lumi_orch import execution_policy as ep


def _profile(**overrides) -> dict:
    base = {
        "goal": "ANSWER",
        "required_sources": ["USER_INPUT"],
        "complexity": "ATOMIC",
        "safety_level": "READ_ONLY",
        "has_side_effect": False,
        "needs_runtime_decision": False,
        "confidence": 0.9,
    }
    base.update(overrides)
    return base


def test_policy_mapping_rows():
    # ATOMIC + READ_ONLY + 仅用户材料 → direct_stream
    assert ep.execution_policy_for_profile(_profile()) == ep.POLICY_DIRECT_STREAM
    # ATOMIC + 单次只读外部能力（附件/工作区/联网）→ single_tool_then_stream
    assert ep.execution_policy_for_profile(
        _profile(required_sources=["ATTACHED_FILE"])
    ) == ep.POLICY_SINGLE_TOOL_THEN_STREAM
    assert ep.execution_policy_for_profile(
        _profile(required_sources=["WORKSPACE_READ"])
    ) == ep.POLICY_SINGLE_TOOL_THEN_STREAM
    assert ep.execution_policy_for_profile(
        _profile(required_sources=["PUBLIC_WEB"])
    ) == ep.POLICY_SINGLE_TOOL_THEN_STREAM
    # ATOMIC + 副作用 → single_action_skill
    assert ep.execution_policy_for_profile(
        _profile(has_side_effect=True, safety_level="SAFE_WRITE")
    ) == ep.POLICY_SINGLE_ACTION_SKILL
    assert ep.execution_policy_for_profile(
        _profile(safety_level="RISKY_WRITE")
    ) == ep.POLICY_SINGLE_ACTION_SKILL
    # SEQUENTIAL / DYNAMIC
    assert ep.execution_policy_for_profile(
        _profile(complexity="SEQUENTIAL")
    ) == ep.POLICY_PLANNER_DAG
    assert ep.execution_policy_for_profile(
        _profile(complexity="DYNAMIC", goal="EXECUTE")
    ) == ep.POLICY_REACT


def test_profile_literals_support_v2_fields():
    from app.agents.orchestration.planning.task_profiles import TaskProfile

    profile = TaskProfile(
        goal="ANSWER",
        required_sources=["ATTACHED_FILE", "WORKSPACE_READ"],
        complexity="ATOMIC",
        safety_level="READ_ONLY",
        has_side_effect=False,
        needs_runtime_decision=False,
    )
    assert ep.execution_policy_for_profile(profile) == ep.POLICY_SINGLE_TOOL_THEN_STREAM


def test_entry_signals_assessment():
    # 纯文本改写 → ATOMIC / direct_stream
    meta = ep.policy_meta_from_signals(
        ep.TaskEntrySignals(request="请把这句话改得更正式一些", scene="office"),
        enabled=True,
    )
    assert meta is not None
    assert meta["execution_policy"] == ep.POLICY_DIRECT_STREAM
    assert meta["task_profile"]["complexity"] == "ATOMIC"
    assert meta["task_profile"]["required_sources"] == ["USER_INPUT"]
    assert meta["policy_version"] == "v2"

    # 上传文档问答 → ATTACHED_FILE 单次只读
    meta2 = ep.policy_meta_from_signals(
        ep.TaskEntrySignals(
            request="这份文档主要讲了什么",
            scene="office",
            has_office_docs=True,
        ),
        enabled=True,
    )
    assert meta2["execution_policy"] == ep.POLICY_SINGLE_TOOL_THEN_STREAM
    assert "ATTACHED_FILE" in meta2["task_profile"]["required_sources"]

    # 工作区读取 → WORKSPACE_READ
    meta3 = ep.policy_meta_from_signals(
        ep.TaskEntrySignals(
            request="帮我看看工作区里的 README 写了什么",
            scene="office",
            workspace_available=True,
        ),
        enabled=True,
    )
    assert meta3["execution_policy"] == ep.POLICY_SINGLE_TOOL_THEN_STREAM
    assert "WORKSPACE_READ" in meta3["task_profile"]["required_sources"]

    # 动态多步（自行排查并修复+运行测试）→ DYNAMIC / react
    meta4 = ep.policy_meta_from_signals(
        ep.TaskEntrySignals(
            request="项目最近构建失败，请自己检查原因并修复，最后运行测试确认",
            scene="office",
            workspace_available=True,
            reasons=("runtime_decision", "dependency"),
        ),
        enabled=True,
    )
    assert meta4["task_profile"]["complexity"] == "DYNAMIC"
    assert meta4["execution_policy"] == ep.POLICY_REACT

    # 显式关闭开关 → None（保留旧语义）
    assert ep.policy_meta_from_signals(
        ep.TaskEntrySignals(request="你好", scene="chat"), enabled=False
    ) is None


def test_public_and_routing_subset():
    meta = ep.policy_meta_from_signals(
        ep.TaskEntrySignals(request="请把这句话改得更正式一些", scene="office"),
        enabled=True,
        fallback_action=None,
    )
    public = ep.policy_meta_public(meta)
    assert public["execution_policy"] == ep.POLICY_DIRECT_STREAM
    assert public["task_profile"]["goal"]
    assert "policy_version" in public
    update = ep.policy_routing_update(meta)
    assert update["execution_policy"] == ep.POLICY_DIRECT_STREAM
    assert update["policy_version"] == "v2"
    assert update["fallback_action"] is None
    assert set(update) <= set(ep.POLICY_ROUTING_KEYS)
