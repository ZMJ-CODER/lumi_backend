"""路由快照权威规则：唯一生成器 + 两个开关互不覆盖。

固定语义（见 ``app/agents/orchestration/planning/route_snapshot.py``）：
  - Router v2 权威：`route_decision` + 严格 M0-M3 `task_profile` + `policy_version="router_v2"`；
  - 旧执行策略只提供兼容字段（`compat.execution_policy_v2` / `compat.legacy_task_profile`），
    不再写顶层 `task_profile` / `policy_version`；
  - 两个开关都关时策略字段全部清空。

矩阵 D/E/F 是**旧执行策略单独开启**时的回归（Router v2 显式关闭）。
"""

from __future__ import annotations

import asyncio

from app.agents.orchestration.models import JobStatus
from app.agents.orchestration.orchestrator import AgentOrchestrator
from app.agents.orchestration.planning.planner import Planner, TaskTree
from app.agents.orchestration.execution.review import NoopReviewer
from app.agents.orchestration.runtime.state import InMemoryStateStore

STRICT_PROFILE_FIELDS = {
    "complexity", "confidence", "intent_type", "side_effects", "info_sources",
    "output_target", "execution_target", "required_capabilities",
    "path_determinism", "estimated_steps", "risk_level", "data_sensitivity",
    "context_size_estimate",
}


class _RecordingWorker:
    def __init__(self):
        self.calls = []

    async def execute(self, node, ctx):
        self.calls.append(node.id)
        return {"success": True, "content": f"result-{node.id}", "tool": "fake"}


def _node(nid, dep=None):
    from app.agents.orchestration.models import TaskNode

    return TaskNode(id=nid, name=nid, agent="w1", depends_on=[dep] if dep else [])


def _fixed_planner(nodes, profile=None):
    class FakePlanner(Planner):
        async def plan(self, *args, **kwargs):
            return TaskTree(nodes=list(nodes), plan_text="计划文本")

        async def plan_for_level(self, *_args, **_kwargs):
            return TaskTree(nodes=list(nodes), plan_text="计划文本")

    return FakePlanner()


def _build(monkeypatch, *, request_text, planner, router_v2=False):
    from app.core.config import settings

    # 矩阵 D/E/F 验证的是**旧执行策略单独开启**的兼容语义：显式关掉 Router v2，
    # 否则 Router v2 才是权威（那是另一条用例）。
    monkeypatch.setattr(settings, "EXECUTION_POLICY_V2_ENABLED", True)
    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", router_v2)
    store = InMemoryStateStore()
    worker = _RecordingWorker()
    orch = AgentOrchestrator(
        store=store,
        planner=planner,
        workers={"w1": worker},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(orch, "_record_office_summary", noop)
    monkeypatch.setattr(orch, "_record_office_task_index", noop)
    return orch, store, worker


def test_matrix_f_atomic_single_side_effect_skill(monkeypatch):
    orch, store, worker = _build(
        monkeypatch,
        request_text="把这段内容保存成一个新的文本文件 workspace note.txt",
        planner=_fixed_planner([_node("write")]),
    )

    async def scenario():
        job = await orch.submit_job("u1", "把这段内容保存成一个新的文本文件 workspace note.txt",
                                    conversation_id="c1")
        return job

    job = asyncio.run(scenario())
    # 兼容模式（仅旧执行策略）：策略映射仍然生效，但**不占**权威字段。
    assert job.routing["execution_policy"] == "single_action_skill"
    assert job.routing["complexity"] == "ATOMIC"
    assert "policy_version" not in job.routing
    assert "task_profile" not in job.routing
    assert "route_decision" not in job.routing
    assert job.routing["compat"]["execution_policy_version"] == "v2"
    assert job.routing["compat"]["execution_policy_v2"]["execution_policy"] == "single_action_skill"
    legacy_profile = job.routing["compat"]["legacy_task_profile"]
    assert legacy_profile["goal"] in {"GENERATE", "EXECUTE"}
    assert legacy_profile["has_side_effect"] is True
    # 无工作区授权时被安全前置检查拦截为澄清：未启动执行/未拆多 Agent
    assert worker.calls == []
    assert (job.result or {}).get("type") == "clarification"


def test_matrix_d_sequential_uses_planner_dag(monkeypatch):
    orch, store, worker = _build(
        monkeypatch,
        request_text="阅读这份文档，提取关键问题，然后查找资料并整理成报告",
        planner=_fixed_planner([_node("read"), _node("report", "read")]),
    )

    async def scenario():
        job = await orch.submit_job("u1", "阅读这份文档，提取关键问题，然后查找资料并整理成报告",
                                    conversation_id="c1")
        await asyncio.gather(*orch._tasks.values())
        final = await orch.get_job(job.job_id)
        return final

    final = asyncio.run(scenario())
    assert final.routing["execution_policy"] == "planner_dag"
    assert final.routing["complexity"] in {"SEQUENTIAL", "ATOMIC"}
    assert final.status == JobStatus.COMPLETED
    # DAG 执行了全部计划节点（先 read 后 report）
    assert worker.calls == ["read", "report"]


def test_matrix_e_dynamic_tagged_react(monkeypatch):
    orch, store, worker = _build(
        monkeypatch,
        request_text="项目最近经常构建失败，请自己检查原因并修复，最后运行测试确认",
        planner=_fixed_planner([_node("diagnose"), _node("fix", "diagnose")]),
    )

    async def scenario():
        job = await orch.submit_job("u1", "项目最近经常构建失败，请自己检查原因并修复，最后运行测试确认",
                                    conversation_id="c1")
        return job

    job = asyncio.run(scenario())
    assert job.routing["execution_policy"] == "react"
    assert job.routing["complexity"] == "DYNAMIC"
    # 旧画像只能出现在 compat，不再占用顶层 task_profile。
    assert "task_profile" not in job.routing
    assert job.routing["compat"]["legacy_task_profile"]["needs_runtime_decision"] is True


def _submit(monkeypatch, request: str = "读一下 README 并总结"):
    store = InMemoryStateStore()
    worker = _RecordingWorker()
    orch = AgentOrchestrator(
        store=store,
        planner=_fixed_planner([_node("s1")]),
        workers={"w1": worker},
        review=NoopReviewer(),
        temporal_enabled=False,
    )

    async def scenario():
        job = await orch.submit_job("u1", request, conversation_id="c1")
        await asyncio.gather(*orch._tasks.values())
        return await orch.get_job(job.job_id)

    return asyncio.run(scenario())


def test_task_router_v2_records_strict_profile(monkeypatch):
    """场景 1：Router v2 开启（旧执行策略关闭）→ 严格画像 + route_decision。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_POLICY_V2_ENABLED", False)
    job = _submit(monkeypatch, "把这段会议记录整理成待办事项")

    profile = job.routing["task_profile"]
    assert set(profile) >= STRICT_PROFILE_FIELDS
    assert profile["complexity"] in {"M0", "M1", "M2", "M3"}
    assert "info_sources" in profile and "side_effects" in profile
    assert job.routing["policy_version"] == "router_v2"
    assert job.routing["route_mode"] in {
        "direct_chat", "m1_atomic_read", "m1_atomic_action",
        "sequential_workflow", "dynamic_agent", "",
    }
    assert job.routing["safety_action"] in {
        "ALLOW", "ALLOW_SANDBOX_ONLY", "REQUIRE_USER_APPROVAL",
        "REQUIRE_ADMIN_APPROVAL", "BLOCK",
    }
    # route_decision 是权威来源，顶层镜像与其同源。
    decision = job.routing["route_decision"]
    assert decision["schema_name"] == "lumi.route_decision"
    # 方案 4 §2.3：schema v2（新增动作意图/目标/预检位；旧字段仍是加法兼容）。
    assert decision["schema_version"] == 2
    assert "action_intents" in decision and "target_clarity" in decision
    assert decision["policy_version"] == "router_v2"
    assert decision["task_profile"] == profile
    assert job.routing["route_mode"] == decision["route_mode"]
    if decision["route_mode"]:
        assert job.routing["execution_policy"] == decision["route_mode"]
    else:
        # 被依赖校验拦截时没有路由模式：不写空策略名。
        assert "execution_policy" not in job.routing
    # 严格画像之外没有旧画像字段混进来
    assert not ({"goal", "required_sources", "has_side_effect", "needs_runtime_decision"} & set(profile))


def test_legacy_policy_only_keeps_compat_region(monkeypatch):
    """场景 2：只有旧执行策略 → 无 route_decision / 顶层画像，兼容区存在。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", False)
    monkeypatch.setattr(settings, "EXECUTION_POLICY_V2_ENABLED", True)
    job = _submit(monkeypatch)

    assert "route_decision" not in job.routing
    assert "task_profile" not in job.routing
    assert "policy_version" not in job.routing
    assert job.routing["compat"]["execution_policy_version"] == "v2"
    assert job.routing["compat"]["execution_policy_v2"]["execution_policy"] == job.routing["execution_policy"]
    assert job.routing["compat"]["legacy_task_profile"]


def test_both_switches_on_router_v2_wins(monkeypatch):
    """场景 3：两个开关同时开启 → 顶层只有一份权威画像，旧画像只在 compat。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", True)
    monkeypatch.setattr(settings, "EXECUTION_POLICY_V2_ENABLED", True)
    job = _submit(monkeypatch)

    assert job.routing["policy_version"] == "router_v2"
    profile = job.routing["task_profile"]
    assert profile == job.routing["route_decision"]["task_profile"]
    assert profile["complexity"] in {"M0", "M1", "M2", "M3"}
    assert not ({"goal", "required_sources", "has_side_effect"} & set(profile))
    legacy = job.routing["compat"]["legacy_task_profile"]
    assert set(legacy) >= {"goal", "required_sources", "has_side_effect", "needs_runtime_decision"}
    assert legacy["has_side_effect"] == bool(profile["side_effects"])
    assert legacy["needs_runtime_decision"] is (
        profile["path_determinism"] == "UNKNOWN" or profile["complexity"] == "M3"
    )
    # 兼容信息不会反向覆盖权威决策
    route_mode = job.routing["route_decision"]["route_mode"]
    if route_mode:
        assert job.routing["execution_policy"] == route_mode
    else:
        assert "execution_policy" not in job.routing


def test_both_switches_off_clears_policy_fields(monkeypatch):
    """场景 4：两个开关都关 → 策略字段全部干净。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "TASK_ROUTER_V2_ENABLED", False)
    monkeypatch.setattr(settings, "EXECUTION_POLICY_V2_ENABLED", False)
    final = _submit(monkeypatch)

    for key in ("route_decision", "task_profile", "policy_version", "execution_policy", "compat"):
        assert key not in final.routing, f"{key} 不应存在"
    assert final.status == JobStatus.COMPLETED
