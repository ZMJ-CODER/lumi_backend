"""技能体系单元测试：注册/场景过滤/工具定义/执行循环/沙箱."""

import asyncio
import uuid

import pytest

from app.agents.sandbox.local import LocalSandbox
from app.agents.sandbox.registry import available_sandboxes
from app.agents.skills.base import Skill, SkillResult
from app.agents.skills.executor import (
    execute_tool_call,
    is_explicit_user_delete_request,
    get_skills_for_scene,
    get_office_react_capabilities_for_request,
    get_tools_for_scene,
    run_skill_loop,
    select_capabilities_for_request,
    skills_to_tools,
)
from app.agents.skills.capability import ToolCapability
from app.agents.skills.registry import SkillRegistry


@pytest.fixture(autouse=True)
def _skills():
    """加载真实插件目录（plugins/skills），测试结束后清理."""
    from app.agents.skills import loader

    SkillRegistry.clear()
    loader.unload_skill_plugins()
    loader.load_skill_plugins()
    yield
    loader.unload_skill_plugins()
    SkillRegistry.clear()


def test_skills_registered_and_scene_filtered():
    names = {s.name for s in SkillRegistry.list()}
    assert {"web_search", "query_knowledge", "get_datetime", "python_exec"} <= names
    assert {"list_project", "read_project_file", "write_project_file", "run_project_command"} <= names
    chat = {s.name for s in get_skills_for_scene("chat")}
    assert "python_exec" not in chat  # 危险技能不进 chat 场景
    assert "web_search" in chat
    office = {s.name for s in get_skills_for_scene("office")}
    assert "python_exec" in office
    assert {"list_project", "write_project_file", "run_project_command"} <= office


def test_project_skills_metadata():
    ws = SkillRegistry.get("write_project_file")
    assert ws.environment == "client"
    assert ws.requires_confirmation is True
    assert ws.scenes == ["office"]
    rc = SkillRegistry.get("run_project_command")
    assert rc.environment == "client"
    assert rc.requires_confirmation is False  # 白名单命令免确认（npm/pytest 等）
    rp = SkillRegistry.get("read_project_file")
    assert rp.environment == "client"
    assert rp.requires_confirmation is False


def test_todo_confirmation_and_write_policy_are_action_scoped():
    todo = SkillRegistry.get("todo_manager")
    assert todo is not None
    assert todo.requires_confirmation_for({"action": "list"}) is False
    assert todo.is_write_operation({"action": "list"}) is False
    for action in ("add", "complete", "delete"):
        assert todo.requires_confirmation_for({"action": action}) is True
        assert todo.is_write_operation({"action": action}) is True


def test_todo_list_does_not_enter_confirmation_gate(monkeypatch):
    import app.agents.mcp.manager as manager
    import app.agents.skills.executor as executor

    async def fake_call_skill(*_args, **_kwargs):
        return {"success": True, "content": "（暂无待办）", "metadata": {}, "is_error": False}

    async def noop_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "call_skill", fake_call_skill)
    monkeypatch.setattr(executor, "_record_skill_log", noop_log)
    result = asyncio.run(execute_tool_call(
        {"function": {"name": "todo_manager", "arguments": {"action": "list"}}},
        "u1", "office",
    ))
    assert result.success is True
    assert result.error_code is None


def test_todo_list_is_not_marked_effectful_by_dag_safety():
    from app.agents.orchestration.models import TaskNode
    from app.agents.orchestration.safety import is_effectful, prepare_node_safety

    node = TaskNode(
        id="todo-list",
        agent="atomic_step",
        params={
            "instruction": "查看我的待办",
            "preferred_tool": "todo_manager",
            "inputs": {"action": "list"},
        },
    )
    prepare_node_safety(node, "u1", "job1")
    assert is_effectful(node) is False
    assert node.idempotency_key is None


def test_skill_lifecycle_is_exposed_and_experimental_is_not_auto_routable():
    class _ExperimentalSkill(Skill):
        name = "experimental_lifecycle_test"
        description = "仅供显式灰度测试"
        status = "experimental"
        scenes = ["office"]

        async def execute(self, params, context=None):
            return SkillResult(success=True, output="ok")

    SkillRegistry.register(_ExperimentalSkill())
    skill = SkillRegistry.get("experimental_lifecycle_test")
    assert skill.version == "1.0.0"
    assert len(skill.schema_fingerprint) == 64
    assert "experimental_lifecycle_test" not in {
        item["function"]["name"] for item in asyncio.run(get_tools_for_scene("office"))
    }


def test_invalid_skill_lifecycle_is_rejected():
    class _BrokenSkill(Skill):
        name = "broken_lifecycle_test"
        version = "not-a-version"

        async def execute(self, params, context=None):
            return SkillResult(success=True)

    with pytest.raises(ValueError, match="semver"):
        SkillRegistry.register(_BrokenSkill())


def test_registered_skills_meet_minimum_contract():
    """Shared CI guard for breaking Skill API changes."""
    allowed_statuses = {"experimental", "stable", "deprecated", "disabled"}
    for skill in SkillRegistry.list():
        tool = skill.to_tool_definition()["function"]
        assert skill.name and tool["name"] == skill.name
        assert skill.status in allowed_statuses
        assert len(skill.schema_fingerprint) == 64
        assert isinstance(tool["description"], str) and tool["description"].strip()
        assert isinstance(tool["parameters"], dict)
        if tool["parameters"]:
            assert tool["parameters"].get("type") == "object"
            assert isinstance(tool["parameters"].get("properties", {}), dict)


def test_delete_confirmation_bypass_requires_current_explicit_single_file_request():
    args = {"path": "C:/Users/demo/scores.csv", "recursive": False}
    assert is_explicit_user_delete_request("请删除 scores.csv", "delete_file", args)
    assert is_explicit_user_delete_request("把这个文件删掉", "delete_file", args)
    assert not is_explicit_user_delete_request("清理临时文件", "delete_file", args)
    assert not is_explicit_user_delete_request("请删除 scores.csv", "delete_file", {**args, "recursive": True})
    assert not is_explicit_user_delete_request("请删除 scores.csv", "delete_project_file", args)


def test_executor_only_signs_delete_bypass_from_current_user_message(monkeypatch):
    import app.agents.mcp.manager as manager
    import app.agents.skills.executor as executor

    captured = []

    async def fake_call_skill(*_args, **kwargs):
        captured.append(kwargs.get("execution_policy"))
        return {"success": True, "content": "done", "metadata": {}, "is_error": False}

    async def noop_log(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "call_skill", fake_call_skill)
    monkeypatch.setattr(executor, "_record_skill_log", noop_log)
    tool_call = {"function": {"name": "delete_file", "arguments": {"path": "C:/demo/scores.csv"}}}
    assert asyncio.run(execute_tool_call(tool_call, "u1", "office", user_message="请删除 scores.csv")).success
    assert asyncio.run(execute_tool_call(tool_call, "u1", "office", user_message="整理一下资料")).success
    assert captured == [{"explicit_user_delete": True}, None]


def test_tool_definition_shape():
    tools = skills_to_tools("chat")
    by_name = {t["function"]["name"]: t["function"] for t in tools}
    ws = by_name["web_search"]
    assert ws["parameters"]["required"] == ["query", "max_results"]
    assert "type" in ws["parameters"]


def test_unified_tools_include_system_skills_but_not_global_desktop_mcp():
    """桌面能力通过当前用户专属请求队列，不暴露为全局 MCP 工具。"""
    tools = asyncio.run(get_tools_for_scene("office"))
    names = {t["function"]["name"] for t in tools}
    assert "get_datetime" in names
    assert "open_app" in names
    assert not any(name.startswith("mcp__") for name in names)


def test_unavailable_script_sandbox_is_hidden_from_runtime_tools(monkeypatch):
    """未部署隔离沙箱时，规划器不应选择 python_exec 后才失败。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "AGENT_ALLOW_UNSAFE_LOCAL_SANDBOX", False)
    tools = asyncio.run(get_tools_for_scene("office"))
    assert "python_exec" not in {tool["function"]["name"] for tool in tools}


def test_docker_sandbox_is_registered():
    assert "docker" in available_sandboxes()


def test_file_conversion_prefers_coarse_script_capability(monkeypatch):
    import app.agents.skills.executor as exec_mod

    capabilities = [
        ToolCapability(name="python_exec", description="运行脚本并生成真实文件"),
        ToolCapability(name="read_file", description="读取文件"),
        ToolCapability(name="write_file", description="写入文件"),
        ToolCapability(name="office_doc_read", description="读取办公文档"),
        ToolCapability(name="query_knowledge", description="查询知识库"),
    ]

    async def fake_capabilities(*args, **kwargs):
        return capabilities

    monkeypatch.setattr(exec_mod, "get_capabilities_for_scene", fake_capabilities)
    selected = asyncio.run(
        select_capabilities_for_request("把 scores.csv 转为 txt 并生成文件", "office", limit=2)
    )
    names = {item.name for item in selected}
    assert "python_exec" in names
    assert names.isdisjoint({"read_file", "write_file", "office_doc_read"})


def test_office_react_capabilities_exclude_development_and_generic_shell_tools():
    names = {
        item.name
        for item in asyncio.run(get_office_react_capabilities_for_request("分析上传文档并打开 WPS"))
    }
    assert {"office_doc_read", "open_app", "query_knowledge"} <= names
    assert names.isdisjoint({
        "git", "apply_patch", "install_new_dependencies", "run_tests",
        "write_project_file", "read_project_file", "read_file", "bash",
        "run_project_command", "curl", "env",
    })


def test_office_react_metadata_routes_conversion_to_script_before_document_read(monkeypatch):
    import app.agents.skills.executor as exec_mod

    capabilities = [
        ToolCapability(
            name="python_exec", description="脚本", category="shell", domain="data",
            intent_tags=["转换", "导出", "生成文件"], preferred_over=["office_doc_read"],
        ),
        ToolCapability(
            name="office_doc_read", description="读取", category="office", domain="document",
            intent_tags=["文档", "读取"],
        ),
        ToolCapability(
            name="compose_email", description="邮件", category="office", domain="writing",
            intent_tags=["邮件", "撰写"],
        ),
    ]

    async def fake_capabilities(*args, **kwargs):
        return capabilities

    monkeypatch.setattr(exec_mod, "get_capabilities_for_scene", fake_capabilities)
    selected = asyncio.run(
        select_capabilities_for_request("把 scores.csv 转为 txt 并生成文件", "office", limit=2)
    )
    assert [item.name for item in selected] == ["python_exec", "compose_email"]


def test_office_react_candidate_limit_is_eight_or_less():
    selected = asyncio.run(get_office_react_capabilities_for_request("分析一个复杂办公任务"))
    assert 1 <= len(selected) <= 8


def test_office_react_scopes_clear_schedule_request(monkeypatch):
    import app.agents.skills.executor as exec_mod

    capabilities = [
        ToolCapability(name="todo_manager", domain="schedule"),
        ToolCapability(name="calendar_manager", domain="schedule"),
        ToolCapability(name="query_knowledge", domain="research"),
        ToolCapability(name="web_search", domain="research"),
    ]

    async def fake_select(*args, **kwargs):
        from app.agents.skills.executor import CapabilitySelection

        return CapabilitySelection(capabilities=capabilities, candidates=[], scene="office")

    monkeypatch.setattr(exec_mod, "select_capabilities_with_trace", fake_select)
    selected = asyncio.run(get_office_react_capabilities_for_request("列一下当前待办事项"))
    assert {item.name for item in selected} == {"todo_manager", "calendar_manager"}


def test_semantic_score_is_only_a_legal_pool_tiebreaker(monkeypatch):
    import app.agents.skills.executor as exec_mod

    capabilities = [
        ToolCapability(name="legal", description="处理报销合同", category="office"),
        ToolCapability(name="other", description="处理图片", category="office"),
    ]

    async def fake_capabilities(*_args, **_kwargs):
        return capabilities

    async def fake_semantic(*_args, **_kwargs):
        return {"legal": 0.01, "forbidden": 1.0}

    monkeypatch.setattr(exec_mod, "get_capabilities_for_scene", fake_capabilities)
    monkeypatch.setattr("app.agents.skills.routing.semantic_scores", fake_semantic)
    selected = asyncio.run(select_capabilities_for_request("无关表达", "office", limit=1))
    assert [item.name for item in selected] == ["legal"]


def test_candidate_trace_records_margin_and_marks_near_tie(monkeypatch):
    import app.agents.skills.executor as exec_mod

    capabilities = [
        ToolCapability(name="first", description="查询资料", category="office"),
        ToolCapability(name="second", description="查询资料", category="office"),
    ]

    async def fake_capabilities(*_args, **_kwargs):
        return capabilities

    async def fake_semantic(*_args, **_kwargs):
        return {"first": 0.90, "second": 0.89}

    monkeypatch.setattr(exec_mod, "get_capabilities_for_scene", fake_capabilities)
    monkeypatch.setattr("app.agents.skills.routing.semantic_scores", fake_semantic)
    selection = asyncio.run(exec_mod.select_capabilities_with_trace("查询资料", "office", limit=2))

    assert selection.top_score > selection.second_score
    assert selection.score_margin == selection.top_score - selection.second_score
    assert selection.ambiguous is True
    assert selection.low_confidence is True
    metadata = selection.to_metadata()
    assert metadata["ambiguous"] is True
    assert metadata["second_score"] == selection.second_score


def test_candidate_margin_uses_runner_up_even_when_limit_is_one(monkeypatch):
    import app.agents.skills.executor as exec_mod

    capabilities = [
        ToolCapability(name="first", description="查询资料", category="office"),
        ToolCapability(name="second", description="查询资料", category="office"),
    ]

    async def fake_capabilities(*_args, **_kwargs):
        return capabilities

    async def fake_semantic(*_args, **_kwargs):
        return {"first": 0.90, "second": 0.89}

    monkeypatch.setattr(exec_mod, "get_capabilities_for_scene", fake_capabilities)
    monkeypatch.setattr("app.agents.skills.routing.semantic_scores", fake_semantic)
    selection = asyncio.run(exec_mod.select_capabilities_with_trace("查询资料", "office", limit=1))

    assert 1 <= len(selection.capabilities) <= 2
    assert selection.second_score > 0
    assert selection.ambiguous is True


def test_ambiguous_write_candidate_requires_escalation_without_keyword():
    import app.agents.skills.executor as exec_mod

    selection = exec_mod.CapabilitySelection(
        capabilities=[
            ToolCapability(name="send_email", domain="communication", requires_confirmation=True),
            ToolCapability(name="compose_email", domain="writing"),
        ],
        candidates=[
            {"name": "send_email", "score": 10.0},
            {"name": "compose_email", "score": 9.5},
        ],
        scene="office",
        top_score=10.0,
        second_score=9.5,
        score_margin=0.5,
        ambiguous=True,
        low_confidence=True,
    )
    assert exec_mod.selection_requires_escalation(selection, "帮我处理一下邮件") is True


class _EchoSkill(Skill):
    name = "echo_test"
    description = "test"
    scenes = ["chat", "office"]
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    async def execute(self, params, context=None):
        return SkillResult(success=True, output=f"ECHO:{params.get('text')}")


class _FakeLLM:
    def __init__(self, rounds):
        self.rounds = rounds
        self.calls = []

    async def chat_with_tools(self, messages, tools, **kw):
        self.calls.append(list(messages))
        content, tool_calls = self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)]
        return content, tool_calls

    async def chat(self, messages, **kw):
        return "fallback"


def test_skill_loop_feeds_results():
    SkillRegistry.register(_EchoSkill())
    tc = {"id": "c1", "type": "function", "function": {"name": "echo_test", "arguments": '{"text": "hi"}'}}
    llm = _FakeLLM([("working", [tc]), ("final answer", None)])
    final, records, _ = asyncio.run(
        run_skill_loop(llm, str(uuid.uuid4()), [{"role": "user", "content": "hi"}], scene="office")
    )
    assert final == "final answer"
    assert records[0]["skill"] == "echo_test"
    assert records[0]["success"] is True
    roles = [m["role"] for m in llm.calls[1]]
    assert roles == ["user", "assistant", "tool"]
    assert any("ECHO:hi" in str(m.get("content")) for m in llm.calls[1])


def test_skill_loop_unknown_skill():
    tc = {"id": "c1", "type": "function", "function": {"name": "nope", "arguments": "{}"}}
    llm = _FakeLLM([("", [tc]), ("done", None)])
    final, records, _ = asyncio.run(
        run_skill_loop(llm, str(uuid.uuid4()), [{"role": "user", "content": "x"}], scene="chat")
    )
    assert records[0]["success"] is False
    assert records[0]["error_code"] == "SKILL_NOT_FOUND"


class _FakeClientSkill(Skill):
    name = "fake_client"
    description = "test client skill"
    environment = "client"
    requires_confirmation = True
    scenes = ["office"]

    async def execute(self, params, context=None):
        return SkillResult(success=True, output="client-done")


class _AdminSkill(Skill):
    name = "admin_only_test"
    description = "admin only"
    permission = "admin"
    scenes = ["office"]

    async def execute(self, params, context=None):
        return SkillResult(success=True, output="admin-done")


def test_admin_skill_is_filtered_and_enforced(monkeypatch):
    SkillRegistry.register(_AdminSkill())
    assert "admin_only_test" not in {s.name for s in get_skills_for_scene("office", "user")}
    assert "admin_only_test" in {s.name for s in get_skills_for_scene("office", "admin")}
    tc = {"id": "a", "function": {"name": "admin_only_test", "arguments": {}}}
    denied = asyncio.run(execute_tool_call(tc, "u1", "office", user_role="user"))
    allowed = asyncio.run(execute_tool_call(tc, "u1", "office", user_role="admin"))
    assert denied.error_code == "FORBIDDEN"
    assert allowed.success is True


def test_client_skill_confirmation_not_blocked(monkeypatch):
    """client 环境的高危技能不应被执行器拦截（确认由用户端弹窗负责）."""
    SkillRegistry.register(_FakeClientSkill())
    import app.agents.skills.executor as exec_mod

    # 纯逻辑测试：不落审计日志、不连 Redis/DB，避免异步连接清理噪音
    async def noop_log(*args, **kwargs):
        pass

    monkeypatch.setattr(exec_mod, "_record_skill_log", noop_log)
    tc = {"id": "c1", "type": "function", "function": {"name": "fake_client", "arguments": "{}"}}
    result = asyncio.run(execute_tool_call(tc, str(uuid.uuid4()), scene="office"))
    assert result.success is True
    assert result.output == "client-done"


def test_local_sandbox():
    async def _run():
        sb = LocalSandbox()
        ok = await sb.run_script("print(6*7)", timeout=10)
        to = await sb.run_script("import time; time.sleep(5)", timeout=1)
        rej = await sb.run_command(["rm", "-rf", "/"], timeout=5)
        return ok, to, rej

    ok, to, rej = asyncio.run(_run())
    assert ok.status == "success"
    assert "42" in ok.stdout
    assert to.status == "timeout"
    assert rej.status == "rejected"
