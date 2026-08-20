"""LangChain 适配层不绕过 Lumi 安全执行器。"""

import asyncio

from app.agents.langchain import tools as lc_tools
from app.agents.skills.base import SkillResult


def test_langchain_structured_tool_delegates_to_authorized_executor(monkeypatch):
    class Capability:
        name = "safe_echo"
        description = "echo"
        parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    captured = {}

    async def fake_capability(*args, **kwargs):
        return Capability()

    async def fake_execute(call, user_id, scene, conversation_id, **kwargs):
        captured.update({"call": call, "user_id": user_id, "scene": scene, "conversation_id": conversation_id})
        return SkillResult(success=True, output="ok")

    monkeypatch.setattr(lc_tools, "get_tool_capability", fake_capability)
    monkeypatch.setattr(lc_tools, "execute_tool_call", fake_execute)
    tool = asyncio.run(lc_tools.make_skill_tool("safe_echo", user_id="u1", scene="office", conversation_id="j1"))
    assert asyncio.run(tool.ainvoke({"text": "hello"})) == "ok"
    assert captured["user_id"] == "u1"
    assert captured["call"]["function"]["name"] == "safe_echo"
