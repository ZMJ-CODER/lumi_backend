"""工作区读取快路径（B 方案）+ direct_llm 诚实兜底（A 方案）回归测试。

覆盖产品方案的三条分流规则：

* 工作区 + 单文件目标明确 → M1：atomic_step(workspace_navigator, read) → direct_llm
* 工作区 + 目标明确但文件未知 → 不走快路径（交回 M0/M2/M3 自行 search/list）
* 工作区 + 全目录/多文件 → 不走快路径（需要顺序逐个读取）

以及硬规则：没有真实读取结果时，文本节点不得假装读到了正文、不得输出内部路由标记。
"""

from __future__ import annotations

import asyncio

from app.agents.orchestration.planning.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import TaskTree
from app.agents.orchestration.planning.tca import ComplexityLevel, TaskComplexityAssessor
from app.agents.orchestration.office.workspace_content import (
    workspace_read_available,
    workspace_read_target,
)

READY_SUMMARY = (
    "当前对话绑定了一个本地工作区「项目」（workspace_id=ws1）。\n"
    "工作区已注册且可访问（当前版本 v3）。\n"
    "根目录包含：\n"
    "- README.md（文件）\n"
    "- config.yaml（文件）\n"
    "- src（目录）\n"
    "当前没有暂存修改。"
)


class _NeverPlanner:
    async def plan_for_level(self, *_args, **_kwargs):  # pragma: no cover - 快路径不应触发规划
        raise AssertionError("工作区单文件快路径不应调用 Planner")


class _EmptyPlanner:
    """给"不该走快路径"的用例用：返回空计划，便于断言路由级别。"""

    supports_context_planning = True

    def __init__(self) -> None:
        self.called = False

    async def plan_for_level(self, *_args, **_kwargs):
        self.called = True
        return TaskTree(nodes=[], error="empty", error_code="PLANNER_EMPTY")


def _context(request: str, *, workspace_id: str = "ws1", summary: str = READY_SUMMARY) -> PlanRequestContext:
    return PlanRequestContext.from_legacy_args(
        "u1", request, "office", None, None,
        workspace_id=workspace_id, workspace_summary=summary,
    )


def _select(request: str, *, workspace_id: str = "ws1", summary: str = READY_SUMMARY,
            planner=None):
    service = OfficePlanSelectionService(
        planner=planner or _NeverPlanner(), workers={}, assessor=TaskComplexityAssessor()
    )
    return asyncio.run(service.select(
        user_id="u1", request=request, user_role="user",
        project_id=None, project_ids=None, clarification_answer=None,
        office_docs=[], prior_summaries="",
        planning_context=_context(request, workspace_id=workspace_id, summary=summary),
        routing_model={},
    ))


# ── 目标判定 ───────────────────────────────────────────────

def test_explicit_file_reference_is_recognized():
    target = workspace_read_target("读取工作区里的 config.yaml", READY_SUMMARY)
    assert target is not None
    assert target.path == "config.yaml"
    assert target.source == "explicit_reference"


def test_nested_path_reference_is_recognized():
    target = workspace_read_target("看一下 src/main.py 里做了什么", READY_SUMMARY)
    assert target is not None and target.path == "src/main.py"


def test_unique_summary_entry_is_recognized():
    target = workspace_read_target("README 里讲了什么", READY_SUMMARY)
    assert target is not None
    # 无扩展名的常见文件名按摘要里的真实条目名回填（README → README.md）。
    assert target.path == "README.md"
    assert target.source == "unique_summary_entry"


def test_summary_entry_name_without_extension_is_recognized_as_summary_hit():
    """摘要里的条目本身没有扩展名时，只能靠"命中摘要唯一条目"判定。"""
    summary = "工作区已注册且可访问（当前版本 v3）。\n根目录包含：\n- README（文件）\n- Makefile（文件）\n"
    target = workspace_read_target("readme 里讲了什么", summary)
    assert target is not None
    assert target.path == "README"
    assert target.source == "unique_summary_entry"


def test_unique_summary_entry_by_full_name():
    target = workspace_read_target("config.yaml 里的超时时间是多少", READY_SUMMARY)
    assert target is not None and target.path == "config.yaml"


def test_goal_only_request_is_not_a_single_file_target():
    """用户只给目标、没给文件名：不能猜，应交给 Agent 自行 search/list。"""
    assert workspace_read_target("找出项目中负责登录的代码并解释调用流程", READY_SUMMARY) is None


def test_bulk_request_is_not_a_single_file_target():
    assert workspace_read_target("总结这个资料文件夹里的所有文档", READY_SUMMARY) is None
    assert workspace_read_target("把每个文件都读一遍", READY_SUMMARY) is None


def test_write_intent_is_not_a_read_target():
    assert workspace_read_target("修改 config.yaml 里的超时时间", READY_SUMMARY) is None


def test_multiple_explicit_files_are_not_a_single_target():
    assert workspace_read_target("对比 a.py 和 b.py 的差异", READY_SUMMARY) is None


def test_version_like_token_is_not_a_file_target():
    assert workspace_read_target("升级到 v1.2 怎么做", READY_SUMMARY) is None


def test_read_available_requires_binding_and_ready_status():
    assert workspace_read_available("ws1", READY_SUMMARY) is True
    assert workspace_read_available("", READY_SUMMARY) is False
    assert workspace_read_available("ws1", "工作区：设备离线，无法读取。") is False


# ── 计划形状 ───────────────────────────────────────────────

def test_explicit_workspace_file_gets_read_then_answer_plan():
    selection = _select("读取工作区里的 config.yaml 并说明用途")
    assert selection.level == ComplexityLevel.M1
    assert selection.routing["planner_invoked"] is False
    assert selection.routing["fallback_action"] == "workspace_m1_read"
    assert [node.agent for node in selection.tree.nodes] == ["atomic_step", "direct_llm"]
    read = selection.tree.nodes[0]
    assert read.params["preferred_tool"] == "workspace_navigator"
    assert read.params["inputs"] == {"action": "read", "path": "config.yaml"}
    answer = selection.tree.nodes[1]
    assert answer.depends_on == ["read_workspace"]
    # 硬规则标记：必须有真实读取结果才能"根据工作区内容回答"。
    assert answer.metadata["require_workspace_read_result"] is True
    assert answer.metadata["allow_missing_capability_answer"] is True


def test_explicit_complete_document_request_marks_read_to_end():
    selection = _select("通读整份答辩.pptx 并总结全部页面")
    read = selection.tree.nodes[0]
    assert read.params["inputs"]["action"] == "read"
    assert read.params["inputs"]["read_to_end"] is True
    assert read.metadata["workspace_complete_read"] is True


def test_goal_only_workspace_request_does_not_take_fast_path():
    planner = _EmptyPlanner()
    selection = _select("找出项目中负责登录的代码并解释调用流程", planner=planner)
    assert selection.routing.get("fallback_action") != "workspace_m1_read"
    assert all(
        (node.params or {}).get("preferred_tool") != "workspace_navigator"
        for node in selection.tree.nodes
    )


def test_workspace_fast_path_requires_bound_workspace():
    planner = _EmptyPlanner()
    selection = _select("读取 config.yaml", workspace_id="", planner=planner)
    assert selection.routing.get("fallback_action") != "workspace_m1_read"
    assert all(
        (node.params or {}).get("preferred_tool") != "workspace_navigator"
        for node in selection.tree.nodes
    )


def test_workspace_fast_path_skipped_when_device_offline():
    planner = _EmptyPlanner()
    selection = _select("读取 config.yaml", summary="工作区：设备离线，无法读取。", planner=planner)
    assert selection.routing.get("fallback_action") != "workspace_m1_read"
    assert all(
        (node.params or {}).get("preferred_tool") != "workspace_navigator"
        for node in selection.tree.nodes
    )


# ── A：direct_llm 诚实兜底 ─────────────────────────────────

def _run_direct_llm(node, dependencies, captured: dict):
    from app.agents.roles.direct_llm import DirectLlmAgent
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode

    if not isinstance(node, TaskNode):
        node = TaskNode(**node)

    async def fake_office_llm(_ctx, system, _prompt, **_kwargs):
        captured["system"] = system
        return "未读取到正文。"

    import app.agents.roles.direct_llm as module

    original = module.office_llm
    module.office_llm = fake_office_llm
    try:
        ctx = WorkerContext(user_id="u1", job_id="j1", scene="office")
        node.metadata = {**(node.metadata or {}), "dependency_results": dependencies}
        return asyncio.run(DirectLlmAgent().execute(node, ctx))
    finally:
        module.office_llm = original


def test_direct_llm_refuses_to_invent_workspace_content_when_read_failed():
    captured: dict = {}
    result = _run_direct_llm(
        {
            "id": "answer",
            "name": "根据工作区文件回答",
            "agent": "direct_llm",
            "params": {"instruction": "根据上传读取的文件回答用户问题"},
            "metadata": {"require_workspace_read_result": True, "allow_missing_capability_answer": True},
            "depends_on": ["read_workspace"],
        },
        {"read_workspace": {
            "status": "failed",
            "error": "工作区未在后端注册",
            "error_code": "WORKSPACE_NOT_REGISTERED",
        }},
        captured,
    )
    assert result["success"] is True
    system = captured["system"]
    # 硬规则生效：明确禁止推测、禁止输出内部路由标记。
    assert "不得根据工作区目录摘要" in system
    assert "ROUTE_UPGRADE" not in system
    assert "尚未读取到文件正文" in system


def test_direct_llm_allows_answer_when_workspace_read_succeeded():
    captured: dict = {}
    _run_direct_llm(
        {
            "id": "answer",
            "name": "根据工作区文件回答",
            "agent": "direct_llm",
            "params": {"instruction": "根据工作区文件回答用户问题"},
            "metadata": {"require_workspace_read_result": True},
            "depends_on": ["read_workspace"],
        },
        {"read_workspace": {"status": "completed", "content": "config.yaml 的正文事实"}},
        captured,
    )
    system = captured["system"]
    # 有真实正文：不得走降级规则；同时必须有"直接用正文回答"的正面契约。
    assert "不得根据工作区目录摘要" not in system
    assert "尚未读取到文件正文" not in system
    assert "不要声明" in system and "看不到" in system


def test_direct_llm_detects_workspace_failure_without_explicit_marker():
    """Planner 自发生成的工作区步骤没有标记，也要按依赖错误码兜底。"""
    captured: dict = {}
    _run_direct_llm(
        {
            "id": "answer",
            "name": "根据工作区回答",
            "agent": "direct_llm",
            "params": {"instruction": "回答用户关于工作区的问题"},
            "metadata": {},
            "depends_on": ["read"],
        },
        {"read": {"status": "failed", "error": "设备离线", "error_code": "WORKSPACE_DEVICE_OFFLINE"}},
        captured,
    )
    assert "不得根据工作区目录摘要" in captured["system"]
