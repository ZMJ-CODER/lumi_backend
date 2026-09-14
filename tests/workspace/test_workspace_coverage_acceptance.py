"""工作区复杂任务验收：文件未知自主发现（Acceptance 2）+ 全目录覆盖（Acceptance 3）。

用假 Electron 原子工具驱动真实的 ``WorkspaceCoverageAgent``，检查：

* 文件未知任务：先 ``search`` → 选候选 → ``read``，搜索无命中时用 ``list`` 兜底；
* 全目录任务：``list`` 建清单 → 逐个 ``read`` → 记录 completed/failed/skipped；
* 覆盖度：只有 ``coverage=ALL`` 才允许声称"全部完成"，超预算必须显式部分完成；
* 计划接入：目标未知/全目录请求会生成 workspace_coverage 节点，且 direct_llm
  带 require_workspace_read_result；单文件快路径不会重复注入。
"""

from __future__ import annotations

import asyncio

import pytest

from app.agents.core.base import WorkerContext
from app.agents.orchestration.planning.office_plan_selection_service import OfficePlanSelectionService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.tca import TaskComplexityAssessor
from app.agents.roles.knowledge.workspace_coverage import (
    COVERAGE_ALL,
    COVERAGE_PARTIAL,
    COVERAGE_SELECTED,
    WorkspaceCoverageAgent,
    rank_candidates,
)
from app.agents.skills.loader import load_skill_plugins

READY_SUMMARY = (
    "当前对话绑定了一个本地工作区「项目」（workspace_id=ws1）。\n"
    "工作区已注册且可访问（当前版本 v3）。\n"
    "根目录包含：\n- README.md（文件）\n- src（目录）\n"
)

FILES = {
    "src/auth_service.py": "def login(user, password):\n    token = issue_token(user)\n    return token\n",
    "src/token_handler.py": "def issue_token(user):\n    return build_jwt(user)\n",
    "src/billing.py": "def charge(order):\n    return gateway.charge(order)\n",
    "docs/说明.md": "# 资料\n这是一个说明文档。\n",
    "docs/合同.txt": "合同正文：付款期限为 30 天。\n",
    "docs/附件2.txt": "附件二正文。\n",
}


class _FakeElectron:
    """记录每一次原子工具调用，便于断言 action 序列。"""

    def __init__(
        self,
        *,
        fail_paths: set[str] | None = None,
        search_hits: list[str] | None = None,
        paged_paths: set[str] | None = None,
        huge_paths: set[str] | None = None,
        list_failure: str = "",
    ):
        self.calls: list[dict] = []
        self.fail_paths = set(fail_paths or ())
        self.search_hits = search_hits
        # 这些文件正文远大于一页预算：会被 WorkspaceReader 分页，最终整份读完。
        self.paged_paths = set(paged_paths or ())
        # 这些文件长到单次页数上限都读不完（模拟超长文档）。
        self.huge_paths = set(huge_paths or ())
        # 目录枚举失败码（模拟"客户端离线/未绑定工作区"这类读取链路故障）。
        self.list_failure = list_failure

    async def list_tools(self, _server):
        return [
            {"name": "workspace_list"},
            {"name": "workspace_stat"},
            {"name": "workspace_search"},
            {"name": "workspace_content_extract"},
        ]

    async def call_tool(self, _server, tool_name, args, **_kwargs):
        args = dict(args or {})
        self.calls.append({"tool": tool_name, "args": args})
        if tool_name == "workspace_list":
            if self.list_failure:
                return {"status": "failed", "error_code": self.list_failure}
            base = str(args.get("path") or "")
            entries = []
            for path in FILES:
                if base and not path.startswith(base.rstrip("/") + "/"):
                    continue
                entries.append({"path": path, "type": "file", "size": len(FILES[path])})
            if not base:
                entries.append({"path": "src", "type": "directory"})
                entries.append({"path": "docs", "type": "directory"})
            return {"status": "success", "data": {"entries": entries}, "has_more": False, "cursor": ""}
        if tool_name == "workspace_stat":
            path = str(args.get("path") or "")
            if path not in FILES:
                return {"status": "failed", "error_code": "ENOENT"}
            return {"status": "success", "data": {"kind": "file", "size": len(FILES[path])}}
        if tool_name == "workspace_search":
            hits = self.search_hits
            if hits is None:
                hits = [p for p in FILES if "auth" in p or "token" in p]
            return {
                "status": "success",
                "data": {"matches": [
                    {"path": p, "line": 1, "text": FILES[p].splitlines()[0], "match_type": "content"}
                    for p in hits
                ]},
                "has_more": False,
                "cursor": "",
            }
        if tool_name == "workspace_content_extract":
            path = str(args.get("path") or "")
            if path in self.fail_paths:
                return {"status": "failed", "error_code": "WORKSPACE_READ_FAILED"}
            if path not in FILES:
                return {"status": "failed", "error_code": "ENOENT"}
            if path in self.huge_paths:
                # 超长文档：单次 read 的页数上限都读不完 → has_more 必须为真。
                huge_text = "\n".join(
                    f"第{i}段正文" + "内容" * 1200 for i in range(1, 25)
                )
                return {
                    "status": "success",
                    "data": {"text": huge_text},
                }
            if path in self.paged_paths:
                # 模拟"长文件"：正文超过单页预算，WorkspaceReader 会分页给出
                # 多页 + 最终 has_more=false（真实的 25 页 PPT 就是这个形态）。
                long_text = "\n".join(
                    f"第{i}段正文" + "内容" * 1200 for i in range(1, 6)
                )
                return {
                    "status": "success",
                    "data": {"text": long_text},
                }
            return {"status": "success", "data": {"text": FILES[path]}, "has_more": False, "cursor": ""}
        return {"status": "failed", "error_code": "UNSUPPORTED"}

    def actions(self) -> list[str]:
        """把原子工具调用翻译成 workspace_navigator 的 action 序列。"""
        mapping = {
            "workspace_search": "search",
            "workspace_list": "list",
            "workspace_content_extract": "read",
            "workspace_read": "read",
        }
        return [mapping.get(item["tool"], item["tool"]) for item in self.calls]

    def read_paths(self) -> list[str]:
        return [
            str(item["args"].get("path") or "")
            for item in self.calls
            if item["tool"] in {"workspace_content_extract", "workspace_read"}
        ]


@pytest.fixture(autouse=True)
def _plugins():
    load_skill_plugins()
    yield


def _install(monkeypatch, electron: _FakeElectron) -> None:
    import app.agents.mcp.manager as manager
    import app.workspace.context as wc

    async def list_tools(server):
        return await electron.list_tools(server)

    async def call_tool(server, tool_name, args, **kwargs):
        return await electron.call_tool(server, tool_name, args, **kwargs)

    monkeypatch.setattr(manager, "list_tools", list_tools)
    monkeypatch.setattr(manager, "call_tool", call_tool)
    route = {"status_code": "WORKSPACE_READY", "server_name": "lumi_pc", "device_id": "dev-1"}
    monkeypatch.setattr(wc, "resolve_workspace_desktop", lambda user_id, workspace_id: dict(route))
    import app.workspace.read.reader as wr

    monkeypatch.setattr(wr.WorkspaceReader, "_route", lambda self: dict(route))


def _run_agent(mode: str, query: str, monkeypatch, electron: _FakeElectron, **params):
    from app.agents.orchestration.models import TaskNode

    _install(monkeypatch, electron)
    node = TaskNode(
        id="discover", agent="workspace_coverage",
        params={"query": query, "mode": mode, **params},
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="ws1")
    return asyncio.run(WorkspaceCoverageAgent().execute(node, ctx))


# ── Acceptance 2：目标明确、文件未知 ───────────────────────

def test_unknown_file_task_searches_then_reads(monkeypatch):
    electron = _FakeElectron()
    result = _run_agent("selected", "找出项目中负责登录的代码并解释调用流程", monkeypatch, electron)

    assert result["success"] is True
    actions = electron.actions()
    # 必须真的先 search 再 read；不允许要求用户提供路径。
    assert "search" in actions and "read" in actions
    assert actions.index("search") < actions.index("read")
    read_paths = electron.read_paths()
    assert "src/auth_service.py" in read_paths
    assert "src/token_handler.py" in read_paths
    coverage = result["tool_metadata"]["workspace_coverage"]
    assert coverage["coverage"] == COVERAGE_SELECTED
    assert coverage["match_count"] >= 2
    assert coverage["read_count"] == len(coverage["read_files"]) >= 1
    # 正文进入后续步骤
    assert "src/auth_service.py" in result["content"]
    assert "login" in result["content"]


def test_unknown_file_task_falls_back_to_list_when_search_is_empty(monkeypatch):
    electron = _FakeElectron(search_hits=[])
    result = _run_agent("selected", "找出项目中负责登录的代码", monkeypatch, electron)

    actions = electron.actions()
    assert "search" in actions
    # 搜索无命中 → 用目录清单兜底（仍然自主选文件，不问用户）
    assert "list" in actions
    assert electron.read_paths(), "搜索无命中时也必须真的读文件，而不是空手而归"
    assert result["tool_metadata"]["workspace_coverage"]["coverage"] == COVERAGE_SELECTED


def test_unknown_file_task_with_no_candidate_stops_honestly(monkeypatch):
    electron = _FakeElectron(search_hits=[])
    result = _run_agent("selected", "找出项目中负责登录的代码", monkeypatch, electron,
                        directory="empty-dir")
    # 目录里什么都没有：明确失败，不编造
    assert result["success"] is False
    assert result["error_code"] in {"WORKSPACE_NO_CANDIDATE", "WORKSPACE_READ_FAILED"}


def test_read_chain_failure_is_not_reported_as_missing_material(monkeypatch):
    """读取链路失败（客户端离线/未绑定工作区）不能说成"工作区里没有相关资料"。

    这是用户可见的归因错误：设备没连时界面说"没找到与目标相关的可读文件"，
    会让人以为读取工具坏了或资料不存在，而真正该做的是重连设备 / 绑对工作区。
    """
    electron = _FakeElectron(search_hits=[], list_failure="WORKSPACE_DEVICE_OFFLINE")
    result = _run_agent("selected", "找出项目中负责登录的代码", monkeypatch, electron)

    assert result["success"] is False
    # 客户端可用性问题必须落进既有恢复分类认识的码（否则只会走 execution 兜底）
    assert result["error_code"] == "CLIENT_OFFLINE"
    assert result["retryable"] is True
    assert "没有找到与目标相关的可读文件" not in result["error"]
    assert "WORKSPACE_DEVICE_OFFLINE" in result["error"], "原始诊断码必须保留在文案里"
    # 真实结局必须随出口带出，而不是被吞掉
    assert result["discovery"]["list"]["error_code"] == "WORKSPACE_DEVICE_OFFLINE"
    assert any("目录枚举不可用" in item for item in result["notes"])
    coverage = result["tool_metadata"]["workspace_coverage"]
    assert coverage["discovery"]["list"]["error_code"] == "WORKSPACE_DEVICE_OFFLINE"


def test_empty_workspace_keeps_the_honest_no_candidate_message(monkeypatch):
    """空工作区（不是故障）仍按原样如实说明，不能被改写成长篇链路故障。"""
    electron = _FakeElectron(search_hits=[])
    result = _run_agent("selected", "找出项目中负责登录的代码", monkeypatch, electron,
                        directory="empty-dir")
    assert result["error_code"] == "WORKSPACE_NO_CANDIDATE"
    assert "没有找到与目标相关的可读文件" in result["error"]
    assert result["retryable"] is False
    assert result["discovery"]["list"]["status"] in {"empty", "ok"}
    assert result["discovery"]["search"]["error_code"] == ""


# ── Acceptance 3：全目录处理 ───────────────────────────────

def test_bulk_directory_task_reads_every_file_and_reports_all(monkeypatch):
    electron = _FakeElectron()
    result = _run_agent("all", "总结这个资料文件夹里的全部文档", monkeypatch, electron)

    assert result["success"] is True
    actions = electron.actions()
    assert actions[0] == "list", "全目录任务必须先建立文件清单"
    assert actions.count("read") == len(FILES)
    assert set(electron.read_paths()) == set(FILES)
    coverage = result["tool_metadata"]["workspace_coverage"]
    assert coverage["coverage"] == COVERAGE_ALL
    assert coverage["skipped_count"] == 0
    assert coverage["failed_count"] == 0
    assert set(coverage["read_files"]) == set(FILES)
    assert "coverage=ALL" in result["content"]


def test_bulk_directory_task_records_failed_files(monkeypatch):
    electron = _FakeElectron(fail_paths={"docs/附件2.txt"})
    result = _run_agent("all", "总结全部文档", monkeypatch, electron)

    coverage = result["tool_metadata"]["workspace_coverage"]
    assert coverage["failed_files"] == [
        {"path": "docs/附件2.txt", "error_code": "WORKSPACE_READ_FAILED"}
    ]
    assert coverage["failed_count"] == 1
    # 有失败文件时不能声称全部完成
    assert coverage["coverage"] == COVERAGE_PARTIAL
    assert "部分结果" in result["content"]


def test_bulk_directory_task_marks_over_budget_files_as_skipped(monkeypatch):
    from app.agents.roles.knowledge.workspace_coverage import MAX_COVERAGE_FILES

    electron = _FakeElectron()
    agent = WorkspaceCoverageAgent(max_read_files=MAX_COVERAGE_FILES)
    _install(monkeypatch, electron)
    from app.agents.orchestration.models import TaskNode

    node = TaskNode(id="d", agent="workspace_coverage", params={"query": "总结全部文档", "mode": "all"})
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="ws1")
    # 预算压到 2，检查超预算文件被显式记为 skipped 而不是静默丢弃
    agent._max_read_files = 2
    result = asyncio.run(agent.execute(node, ctx))

    coverage = result["tool_metadata"]["workspace_coverage"]
    reads = electron.actions().count("read")
    assert reads <= MAX_COVERAGE_FILES
    if len(FILES) > reads:
        assert coverage["skipped_count"] >= 1
        assert coverage["coverage"] == COVERAGE_PARTIAL
        assert all(item["reason"] == "budget_exceeded" for item in coverage["skipped_files"])
        assert "部分结果" in result["content"]


def test_bulk_directory_without_workspace_fails_closed(monkeypatch):
    electron = _FakeElectron()
    _install(monkeypatch, electron)
    from app.agents.orchestration.models import TaskNode

    node = TaskNode(id="d", agent="workspace_coverage", params={"query": "总结全部", "mode": "all"})
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="")
    result = asyncio.run(WorkspaceCoverageAgent().execute(node, ctx))
    assert result["success"] is False
    assert result["error_code"] == "WORKSPACE_NOT_BOUND"
    assert electron.calls == []


# ── 排序器（通用相关性，不做业务关键词路由）───────────────

def test_rank_candidates_prefers_query_relevance_and_text_files():
    matches = [{"path": "src/auth_service.py", "context": "def login", "match_type": "content"}]
    entries = [
        {"path": "src/auth_service.py", "kind": "file"},
        {"path": "docs/说明.md", "kind": "file"},
        {"path": "src", "kind": "directory"},
    ]
    ranked = [item["path"] for item in rank_candidates("登录 login 认证", matches, entries)]
    assert ranked[0] == "src/auth_service.py"
    assert "src" not in ranked  # 目录不是候选


# ── 计划接入 ───────────────────────────────────────────────

class _NeverPlanner:
    async def plan_for_level(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("工作区兜底路径不应调用 Planner")


def _select(request: str, summary: str = READY_SUMMARY):
    service = OfficePlanSelectionService(
        planner=_NeverPlanner(), workers={}, assessor=TaskComplexityAssessor()
    )
    context = PlanRequestContext.from_legacy_args(
        "u1", request, "office", None, None, workspace_id="ws1", workspace_summary=summary,
    )
    return asyncio.run(service.select(
        user_id="u1", request=request, user_role="user",
        project_id=None, project_ids=None, clarification_answer=None,
        office_docs=[], prior_summaries="", planning_context=context, routing_model={},
    ))


def test_unknown_file_request_gets_coverage_plan_not_direct_answer():
    selection = _select("找出项目中负责登录的代码并解释调用流程")
    assert selection.routing["fallback_action"] == "workspace_coverage"
    assert [node.agent for node in selection.tree.nodes] == ["workspace_coverage", "direct_llm"]
    discover = selection.tree.nodes[0]
    assert discover.params["mode"] == "selected"
    assert discover.metadata["workspace_required"] is True
    answer = selection.tree.nodes[1]
    assert answer.depends_on == ["discover_workspace"]
    assert answer.metadata["require_workspace_read_result"] is True


def test_bulk_request_gets_all_coverage_plan():
    selection = _select("总结这个资料文件夹里的所有文档")
    assert selection.routing["fallback_action"] == "workspace_coverage"
    discover = selection.tree.nodes[0]
    assert discover.params["mode"] == "all"
    assert discover.metadata["coverage_target"] == "ALL"


def test_single_file_request_still_uses_m1_fast_path():
    selection = _select("读取工作区里的 README.md")
    assert selection.routing["fallback_action"] == "workspace_m1_read"
    assert [node.agent for node in selection.tree.nodes] == ["atomic_step", "direct_llm"]


# ── 结构化补偿：Planner 计划里没有读取节点时 ────────────────

def _context_for(request: str) -> PlanRequestContext:
    return PlanRequestContext.from_legacy_args(
        "u1", request, "office", None, None, workspace_id="ws1", workspace_summary=READY_SUMMARY,
    )


def _strategies():
    from app.agents.orchestration.planning import office_plan_strategies

    return office_plan_strategies


def test_workspace_required_compensation_injects_discovery_step():
    """Planner 生成了任务但没有注入工作区工具 → 结构化补偿加前置发现步骤。

    直接验证补偿这一层：它只在“工作区已绑定 + 请求面向工作区 + 计划里没有任何
    工作区读取节点”时触发，并且只加前置、不改写原有节点与依赖。
    """
    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.planning.contracts import TaskTree

    strategies = _strategies()
    request = "根据项目工作区里的代码整理一份架构说明"
    context = _context_for(request)
    tree = TaskTree(nodes=[
        TaskNode(id="draft", name="起草", agent="direct_llm",
                 params={"instruction": "根据工作区资料起草结论"}),
        TaskNode(id="polish", name="润色", agent="direct_llm",
                 params={"instruction": "润色上一版"}, depends_on=["draft"]),
    ], plan_text="起草并润色")

    assert strategies.needs_workspace_discovery(tree, request, context) is True
    injected = strategies.inject_workspace_discovery(tree, request, context)

    agents = [node.agent for node in injected.nodes]
    assert agents[0] == "workspace_coverage"
    discover = injected.nodes[0]
    assert discover.id == "discover_workspace"
    assert discover.metadata["compensation_reason"] == "WORKSPACE_REQUIRED"
    by_id = {node.id: node for node in injected.nodes}
    # 原根节点（draft）现在依赖发现步骤；draft → polish 的依赖关系不变
    assert "discover_workspace" in (by_id["draft"].depends_on or [])
    assert by_id["polish"].depends_on == ["draft"]
    assert set(by_id) == {"discover_workspace", "draft", "polish"}


def test_compensation_not_applied_when_plan_already_reads_workspace():
    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.planning.contracts import TaskTree

    request = "根据项目工作区里的代码整理一份架构说明"
    context = _context_for(request)
    tree = TaskTree(nodes=[
        TaskNode(id="read", name="读取", agent="atomic_step",
                 params={"instruction": "读工作区", "preferred_tool": "workspace_navigator",
                         "inputs": {"action": "read", "path": "README.md"}}),
        TaskNode(id="answer", name="回答", agent="direct_llm",
                 params={"instruction": "回答"}, depends_on=["read"]),
    ], plan_text="读取并回答")

    assert _strategies().needs_workspace_discovery(tree, request, context) is False


def test_compensation_not_applied_without_workspace_binding():
    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.planning.contracts import TaskTree

    request = "根据项目工作区里的代码整理一份架构说明"
    context = PlanRequestContext.from_legacy_args(
        "u1", request, "office", None, None, workspace_id="", workspace_summary=READY_SUMMARY,
    )
    tree = TaskTree(nodes=[
        TaskNode(id="draft", name="起草", agent="direct_llm", params={"instruction": "起草"}),
    ], plan_text="起草")
    assert _strategies().needs_workspace_discovery(tree, request, context) is False


def test_compensation_not_applied_for_non_workspace_request():
    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.planning.contracts import TaskTree

    request = "帮我把这段话改得更礼貌"
    context = _context_for(request)
    tree = TaskTree(nodes=[
        TaskNode(id="draft", name="改写", agent="direct_llm", params={"instruction": "改写"}),
    ], plan_text="改写")
    assert _strategies().needs_workspace_discovery(tree, request, context) is False
