"""读取结果交给下游模型节点的**交接形态**回归测试。

复现并锁定用户报告的现象：模型读到文档却声称"看不到具体内容"，回答里出现
"复述文档文字 + 声明看不到文档内容"。根因是原子步骤把**整包 JSON 信封**塞给下游
（正文埋在 limits/skipped_dirs/parser 等实现噪音里），以及缺少"有资料时直接用"的
正面契约。这里同时锁定两侧：

* 交接文本必须是可读正文（含正文事实、不含实现字段）；
* ``direct_llm`` 在有真实正文时必须被明确要求"直接用、不得声明看不到"。
"""

from __future__ import annotations

import asyncio
import json

from app.workspace.read.navigator import handoff_text


def _read_envelope(*, sections: int = 2, has_more: bool = False) -> dict:
    return {
        "status": "ok",
        "action": "read",
        "summary": f"已读取 答辩.pptx（{sections} 段）",
        "data": {
            "path": "答辩.pptx",
            "sections": [
                {
                    "source": "答辩.pptx",
                    "location": f"slide-{i}",
                    "title": f"第{i}页",
                    "text": f"第{i}页正文：系统由三层架构组成。",
                }
                for i in range(1, sections + 1)
            ],
        },
        "has_more": has_more,
        "cursor": "reader-next" if has_more else None,
        "meta": {
            "workspace_id": "ws1",
            "server_name": "lumi_pc",
            "parser": "workspace_content_extract",
            "limits": {"depth": 4, "max_results": 200},
            "skipped_dirs": [".git", "node_modules"],
        },
        "error": None,
    }


def test_handoff_is_readable_text_not_json_envelope():
    text = handoff_text(_read_envelope(sections=2))
    # 正文事实必须在
    assert "第1页正文" in text and "第2页正文" in text
    assert "slide-1" in text and "答辩.pptx" in text
    # 实现噪音必须不在（这正是"模型看不到内容"的成因）
    for noise in ("workspace_id", "server_name", "skipped_dirs", "limits", "parser", '"sections"'):
        assert noise not in text, f"交接文本里不应出现实现字段：{noise}"
    assert not text.lstrip().startswith("{")


def test_handoff_accepts_nested_execution_envelope():
    """执行信封形态 {"data": <信封>} 也要能渲染（判据写反会剥掉真 payload）。"""
    nested = {"call_id": "c1", "status": "success", "data": _read_envelope(sections=1)}
    text = handoff_text(nested)
    assert "第1页正文" in text


def test_handoff_keeps_plain_envelope():
    direct = handoff_text(_read_envelope(sections=1))
    assert "第1页正文" in direct


def test_handoff_reports_error_and_hint():
    error_payload = {
        "status": "error",
        "action": "read",
        "summary": "docs 是目录，不能作为文件读取。",
        "data": {"path": "docs", "sections": []},
        "has_more": False,
        "cursor": None,
        "meta": {},
        "error": {
            "code": "WORKSPACE_PATH_NOT_DIRECTORY",
            "message": "docs 是目录",
            "suggested_action": "先 list",
        },
    }
    text = handoff_text(error_payload)
    assert "WORKSPACE_PATH_NOT_DIRECTORY" in text
    assert "先 list" in text


def test_handoff_marks_unfinished_read():
    text = handoff_text(_read_envelope(sections=1, has_more=True))
    assert "未读完" in text
    assert "reader-next" in text


def test_handoff_respects_caller_budget():
    big = _read_envelope(sections=50)
    big["data"]["sections"][0]["text"] = "内容" * 5000
    text = handoff_text(big, limit=1000)
    assert len(text) <= 1000


def test_handoff_renders_search_and_list_actions():
    matches = {
        "status": "ok", "action": "search", "summary": "命中 1 处",
        "data": {"matches": [{"path": "src/a.py", "location": "line-3", "context": "def login"}]},
        "has_more": False, "cursor": None, "meta": {}, "error": None,
    }
    listing = {
        "status": "ok", "action": "list", "summary": "根目录 1 个条目",
        "data": {"entries": [{"path": "src", "kind": "directory"}]},
        "has_more": False, "cursor": None, "meta": {}, "error": None,
    }
    assert "src/a.py" in handoff_text(matches) and "def login" in handoff_text(matches)
    assert "src" in handoff_text(listing)


# ── 原子步骤 → direct_llm 的实际交接 ───────────────────────

def _run_atomic_read(monkeypatch):
    from contextlib import asynccontextmanager

    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.atomic import AtomicStepAgent
    from app.agents.skills import executor as executor_module
    from app.agents.skills.loader import load_skill_plugins
    from app.workspace.read import navigator as wn
    from tests.acceptance.workspace_navigator_sanitize import _install_fake_electron

    _install_fake_electron({"calls": []}, monkeypatch)

    @asynccontextmanager
    async def _no_lock(tool_name, execution_scope):
        yield

    monkeypatch.setattr(executor_module, "_claim_tool_execution", _no_lock)
    load_skill_plugins()

    async def fake_reader_read(self, request, *, path="", cursor="", max_chars=12000):
        return {
            "status": "success",
            "summary": f"已读取 {path}",
            "content": [{
                "source": path, "location": "slide-1", "title": "第1页",
                "text": "PPTX 正文：三层架构与调用流程。",
            }],
            "has_more": False,
            "cursor": None,
            "meta": {"format": "pptx", "parser": "workspace_content_extract"},
        }

    from app.workspace.read import reader as wr

    monkeypatch.setattr(wr.WorkspaceReader, "read", fake_reader_read)

    node = TaskNode(
        id="read_workspace",
        name="读取工作区文件",
        agent="atomic_step",
        params={
            "instruction": "读取工作区文件 答辩.pptx",
            "preferred_tool": "workspace_navigator",
            "inputs": {"action": "read", "path": "答辩.pptx"},
        },
        metadata={"fast_path": "workspace_m1_read"},
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="ws-acceptance")
    return asyncio.run(AtomicStepAgent().execute(node, ctx)), wn


def test_atomic_read_hands_readable_evidence_to_downstream(monkeypatch):
    result, _wn = _run_atomic_read(monkeypatch)
    assert result["success"] is True
    assert result.get("read_evidence") is True
    content = str(result["content"])
    # 下游必须直接可读，而不是自己去 JSON 里挖
    assert "PPTX 正文" in content
    assert "slide-1" in content
    assert "skipped_dirs" not in content and "workspace_id" not in content
    assert not content.lstrip().startswith("{")
    # execution 仍保留完整结构化信封供审计
    assert json.dumps(result["execution"], ensure_ascii=False).count("sections") >= 1


def test_nested_execution_envelope_preserves_workspace_evidence():
    """回滚后的持久化/Temporal 交接不能把 navigator 正文裁成空证据。"""
    from app.agents.orchestration.execution.context import sanitize_dependency_result

    body = "第 1 页正文。" + ("\n关键结论：系统采用三层架构。" * 1200)
    dependency = {
        "success": True,
        "tool": "workspace_navigator",
        "read_evidence": True,
        "execution": {
            "status": "success",
            "content_type": "structured",
            "data": {
                "status": "ok",
                "action": "read",
                "data": {
                    "sections": [{"source": "答辩.pptx", "location": "slide-1", "text": body}]
                },
            },
        },
    }
    cleaned = sanitize_dependency_result(dependency)
    assert "execution" in cleaned
    assert "sections" in cleaned["execution"]["data"]["data"]
    assert "关键结论" in cleaned["execution"]["data"]["data"]["sections"][0]["text"]


def test_workspace_metadata_marks_raw_coverage_as_evidence_and_keeps_paging_facts():
    """覆盖读取结果即使没有顶层 tool/read_evidence，也不能被普通预算吞掉。"""
    from app.agents.orchestration.execution.context import sanitize_dependency_result

    dependency = {
        "success": True,
        "content": "正文" * 4000,
        "tool_metadata": {
            "tool": "workspace_navigator",
            "workspace_coverage": {"coverage": "PARTIAL", "truncated_files": ["a.pptx"]},
        },
        "error_code": None,
        "has_more": True,
        "cursor": "next-page",
    }
    cleaned = sanitize_dependency_result(dependency, max_chars=12000)
    assert len(cleaned["content"]) > 6000
    assert cleaned["has_more"] is True
    assert cleaned["cursor"] == "next-page"
    assert cleaned["tool_metadata"]["workspace_coverage"]["coverage"] == "PARTIAL"


def test_raw_tool_output_envelope_survives_dependency_sanitization():
    """lineage 直接保存 ToolOutput 时，data.sections 仍可交给下游。"""
    from app.agents.orchestration.execution.context import sanitize_dependency_result

    raw = {
        "status": "success",
        "content_type": "structured",
        "call_id": "call-1",
        "data": _read_envelope(sections=1),
    }
    cleaned = sanitize_dependency_result(raw)
    assert cleaned["data"]["action"] == "read"
    assert cleaned["data"]["data"]["sections"][0]["text"]


def test_direct_llm_extracts_raw_tool_output_envelope(monkeypatch):
    """下游 direct_llm 能从未经过 AtomicStep 包装的 data.sections 取正文。"""
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.direct_llm import DirectLlmAgent
    import app.agents.roles.direct_llm as module

    captured: dict = {}

    async def fake_office_llm(_ctx, _system, prompt, **_kwargs):
        captured["prompt"] = prompt
        return "OK"

    monkeypatch.setattr(module, "office_llm", fake_office_llm)
    node = TaskNode(
        id="answer", agent="direct_llm", params={"instruction": "回答问题"},
        metadata={"dependency_results": {
            "read_any_id": {"status": "success", "data": _read_envelope(sections=1)}
        }},
    )
    asyncio.run(DirectLlmAgent().execute(node, WorkerContext(user_id="u1", job_id="j1", scene="office")))
    assert "第1页正文" in captured["prompt"]


def test_direct_llm_is_told_to_use_available_evidence(monkeypatch):
    """有真实正文时，提示词必须明确禁止"我看不到文档内容"。"""
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.direct_llm import DirectLlmAgent
    import app.agents.roles.direct_llm as module

    captured: dict = {}

    async def fake_office_llm(_ctx, system, _prompt, **_kwargs):
        captured["system"] = system
        return "根据文档：三层架构。"

    monkeypatch.setattr(module, "office_llm", fake_office_llm)
    node = TaskNode(
        id="answer",
        name="根据工作区文件回答",
        agent="direct_llm",
        params={"instruction": "回答用户问题"},
        depends_on=["read_workspace"],
        metadata={"require_workspace_read_result": True, "fast_path": "workspace_m1_read"},
    )
    node.metadata["dependency_results"] = {
        "read_workspace": {
            "success": True,
            "read_evidence": True,
            "content": "[read 摘要] 已读取 答辩.pptx\n===== 答辩.pptx · slide-1 =====\nPPTX 正文",
        }
    }
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office")
    asyncio.run(DirectLlmAgent().execute(node, ctx))

    system = captured["system"]
    assert "不要声明" in system and "看不到" in system
    # 不能误判成"没有正文"而降级
    assert "尚未读取到文件正文" not in system


def test_direct_llm_workspace_budget_does_not_depend_on_node_id(monkeypatch):
    """Planner 任意命名读取节点时，长正文仍完整进入下游提示词预算。"""
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.direct_llm import DirectLlmAgent
    import app.agents.roles.direct_llm as module

    captured: dict = {}

    async def fake_office_llm(_ctx, _system, prompt, **_kwargs):
        captured["prompt"] = prompt
        return "OK"

    monkeypatch.setattr(module, "office_llm", fake_office_llm)
    body = "正文尾部关键结论" + ("。" * 10000)
    node = TaskNode(
        id="planner_generated_read_7",
        name="读取资料",
        agent="direct_llm",
        params={"instruction": "根据资料回答"},
        depends_on=["planner_generated_read_1"],
        metadata={
            "require_workspace_read_result": True,
            "dependency_results": {
                "planner_generated_read_1": {
                    "success": True,
                    "tool": "workspace_navigator",
                    "read_evidence": True,
                    "content": body,
                }
            },
        },
    )
    asyncio.run(DirectLlmAgent().execute(node, WorkerContext(user_id="u1", job_id="j1", scene="office")))
    assert "正文尾部关键结论" in captured["prompt"]
    assert len(captured["prompt"]) > 6000


# ── 不变量：正文在提示词里时，绝不能同时说"没读到正文" ──────

COVERAGE_EVIDENCE = (
    "已读取 1 个工作区文件（候选 1，失败 0，跳过 0，coverage=SELECTED）\n\n"
    "===== 工作区文件：智慧物业管理系统毕业设计答辩.pptx =====\n"
    "XX大学 计算机与信息工程学院 基于SSM+Vue的智慧物业管理系统设计与实现 本科毕业设计答辩"
)


def _run_direct_with_dependency(monkeypatch, dependency: dict) -> tuple[str, str]:
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.direct_llm import DirectLlmAgent
    import app.agents.roles.direct_llm as module

    captured: dict = {}

    async def fake_office_llm(_ctx, system, prompt, **_kwargs):
        captured["system"] = system
        captured["prompt"] = prompt
        return "OK"

    monkeypatch.setattr(module, "office_llm", fake_office_llm)
    node = TaskNode(
        id="answer",
        name="根据工作区文件回答",
        agent="direct_llm",
        params={"instruction": "这个 PPT 说了什么？"},
        depends_on=["discover_workspace"],
        metadata={
            "require_workspace_read_result": True,
            "allow_missing_capability_answer": True,
            "fast_path": "workspace_coverage",
            "dependency_results": {"discover_workspace": dependency},
        },
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office")
    asyncio.run(DirectLlmAgent().execute(node, ctx))
    return captured.get("system", ""), captured.get("prompt", "")


def test_evidence_in_prompt_never_comes_with_honesty_rule(monkeypatch):
    """覆盖 Agent 的证据形态（含各种状态拼写）都不得触发"没读到正文"。"""
    variants = {
        "plain": {"success": True, "content": COVERAGE_EVIDENCE},
        "with_flag": {"success": True, "status": "ok", "content": COVERAGE_EVIDENCE, "read_evidence": True},
        "status_failed_but_has_content": {"status": "failed", "content": COVERAGE_EVIDENCE},
        "output_only": {"success": True, "output": COVERAGE_EVIDENCE},
        "no_status_key": {"content": COVERAGE_EVIDENCE},
    }
    for name, dependency in variants.items():
        system, prompt = _run_direct_with_dependency(monkeypatch, dependency)
        assert "智慧物业" in prompt, f"{name}: 证据没进提示词"
        assert "尚未读取到文件正文" not in system, f"{name}: 正文在提示词里却仍要求声明没读到"
        assert "不要声明" in system, f"{name}: 缺少正面契约（有正文就直说）"


def test_no_evidence_still_degrades_honestly(monkeypatch):
    system, prompt = _run_direct_with_dependency(
        monkeypatch, {"success": False, "error": "设备离线", "error_code": "WORKSPACE_DEVICE_OFFLINE"}
    )
    assert "尚未读取到文件正文" in system
    assert "智慧物业" not in prompt


def test_prompt_never_contains_the_literal_route_marker_when_evidence_ready(monkeypatch):
    """有正文时提示词里**不能出现** [[ROUTE_UPGRADE_RAG]] 字面量。

    模型会把提示词里的标记当动作回吐，用户就会看到"正在切换检索通道"这种内部文案
    （用户实测就是这个现象）。
    """
    for name, dependency in {
        "plain": {"success": True, "content": COVERAGE_EVIDENCE},
        "flag": {"success": True, "status": "ok", "content": COVERAGE_EVIDENCE, "read_evidence": True},
    }.items():
        system, prompt = _run_direct_with_dependency(monkeypatch, dependency)
        assert "ROUTE_UPGRADE" not in system, f"{name}: 提示词里出现了内部路由标记字面量"
        assert "禁用" not in system
        assert "[[...]]" in system or "不要输出任何形如" in system


def _run_direct_with_model_reply(monkeypatch, dependency: dict, replies: list[str]):
    """让模型按给定脚本回复，检查节点最终交付什么。"""
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.direct_llm import DirectLlmAgent
    import app.agents.roles.direct_llm as module

    seen_systems: list[str] = []
    queue = list(replies)

    async def fake_office_llm(_ctx, system, _prompt, **_kwargs):
        seen_systems.append(system)
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(module, "office_llm", fake_office_llm)
    node = TaskNode(
        id="answer", name="根据工作区文件回答", agent="direct_llm",
        params={"instruction": "这个 PPT 说了什么？"},
        depends_on=["discover_workspace"],
        metadata={
            "require_workspace_read_result": True,
            "allow_missing_capability_answer": True,
            "dependency_results": {"discover_workspace": dependency},
        },
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office")
    result = asyncio.run(DirectLlmAgent().execute(node, ctx))
    return result, seen_systems


def test_sentinel_with_evidence_retries_and_delivers_answer(monkeypatch):
    """拿到正文却回吐标记时：严格重试一次，交付真正的答案而不是"切换通道"。"""
    result, systems = _run_direct_with_model_reply(
        monkeypatch,
        {"success": True, "content": COVERAGE_EVIDENCE},
        ["[[ROUTE_UPGRADE_RAG]]", "这份 PPT 讲的是智慧物业管理系统。"],
    )
    assert result["success"] is True
    assert result["error_code"] if "error_code" in result else True
    assert "智慧物业管理系统" in str(result["content"])
    assert "检索通道" not in str(result["content"])
    assert len(systems) == 2, "应当触发一次严格重试"
    assert "ROUTE_UPGRADE" not in systems[1]


def test_sentinel_with_evidence_falls_back_to_evidence(monkeypatch):
    """重试仍回吐标记时，直接把已读到的正文证据交给用户（不丢资料、不谎报）。"""
    result, systems = _run_direct_with_model_reply(
        monkeypatch,
        {"success": True, "content": COVERAGE_EVIDENCE},
        ["[[ROUTE_UPGRADE_RAG]]", "[[ROUTE_UPGRADE_RAG]]"],
    )
    assert result.get("answered_from_evidence") is True
    assert "智慧物业" in str(result["content"])
    assert "检索通道" not in str(result["content"])
    assert len(systems) == 2


def test_sentinel_without_evidence_still_reports_route_upgrade(monkeypatch):
    """真的没有正文时，保持原行为（诚实说明缺资料，不假装读到）。"""
    result, _systems = _run_direct_with_model_reply(
        monkeypatch,
        {"success": False, "error": "设备离线", "error_code": "WORKSPACE_DEVICE_OFFLINE"},
        ["[[ROUTE_UPGRADE_RAG]]"],
    )
    assert result["success"] is False
    assert result["error_code"] == "ROUTE_UPGRADE_RAG"


def test_atomic_step_preserves_coverage_agent_evidence(monkeypatch):
    """覆盖 Agent 自己渲染好的正文必须原样交给下游，不能被通用 2200 预算截断。"""
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.atomic import AtomicStepAgent
    from app.agents.skills import executor as executor_module
    import app.agents.skills.executor as ex
    from app.agents.skills.capability import ToolCapability

    async def fake_capability(name, scene, user_role="user", user_id="", **_kwargs):
        return ToolCapability(
            name=name, version="1.0.0", status="stable", description="",
            category="workspace", domain="workspace",
            parameters={"type": "object", "properties": {}},
            source="mcp", environment="server", server="lumi_skill",
            raw_name="workspace_coverage", permission="user",
            write_op=False, requires_confirmation=False,
            confirmation_mode="client", idempotent=True,
            annotations={"provider": "builtin"},
        )

    long_evidence = "已读取 1 个工作区文件（coverage=SELECTED）\n\n" + COVERAGE_EVIDENCE + "尾段正文" * 800
    monkeypatch.setattr(ex, "get_tool_capability", fake_capability)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _no_lock(tool_name, execution_scope):
        yield

    monkeypatch.setattr(executor_module, "_claim_tool_execution", _no_lock)

    from app.agents.skills.registry import ToolRegistry
    from app.agents.skills.output_contract import ToolOutput

    class _FakeCoverageTool:
        name = "workspace_coverage"
        environment = "server"

        async def execute(self, params, context=None):
            return ToolOutput(
                status="success",
                output=long_evidence,
                data={"workspace_coverage": {"coverage": "SELECTED"}},
                content_type="text",
                metadata={"tool": "workspace_coverage"},
            )

    registry = ToolRegistry
    original_get = registry.get
    monkeypatch.setattr(
        registry, "get", classmethod(lambda cls, name: _FakeCoverageTool() if name == "workspace_coverage" else original_get(name))
    )
    monkeypatch.setattr(registry, "list", classmethod(lambda cls, include_internal=False: []))

    node = TaskNode(
        id="discover_workspace",
        name="搜索并读取相关工作区文件",
        agent="atomic_step",
        params={
            "instruction": "搜索并读取相关工作区文件",
            "preferred_tool": "workspace_coverage",
            "inputs": {},
        },
        metadata={"fast_path": "workspace_coverage"},
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="ws1")
    result = asyncio.run(AtomicStepAgent().execute(node, ctx))
    content = str(result.get("content") or "")
    assert len(long_evidence) > 2200
    if result.get("success"):
        assert len(content) > 2200, "证据被通用预算截断了"
        assert "尾段正文" in content
