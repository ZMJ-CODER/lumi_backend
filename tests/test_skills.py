"""技能体系单元测试：注册/场景过滤/工具定义/执行循环/沙箱."""

import asyncio
import uuid

import pytest

from app.agents.sandbox.local import LocalSandbox
from app.agents.skills.base import Skill, SkillResult
from app.agents.skills.executor import execute_tool_call, get_skills_for_scene, run_skill_loop, skills_to_tools
from app.agents.skills.registry import SkillRegistry, init_skills


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


def test_tool_definition_shape():
    tools = skills_to_tools("chat")
    by_name = {t["function"]["name"]: t["function"] for t in tools}
    ws = by_name["web_search"]
    assert ws["parameters"]["required"] == ["query"]
    assert "type" in ws["parameters"]


class _EchoSkill(Skill):
    name = "echo_test"
    description = "test"
    scenes = ["chat"]
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
        run_skill_loop(llm, str(uuid.uuid4()), [{"role": "user", "content": "hi"}], scene="chat")
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
