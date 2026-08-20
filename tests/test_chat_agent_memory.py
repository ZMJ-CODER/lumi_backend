"""兼容 ChatAgent 的 Redis 短期记忆测试。"""

import asyncio
import json

from app.agents.base import AgentContext
from app.agents.chat_agent import ChatAgent


class _FakeRedis:
    def __init__(self):
        self.values: dict[str, list[str]] = {}

    async def lrange(self, key, start, end):
        return list(self.values.get(key, []))

    async def rpush(self, key, *values):
        self.values.setdefault(key, []).extend(values)

    async def ltrim(self, key, start, end):
        self.values[key] = self.values.get(key, [])[start : None if end == -1 else end + 1]

    async def expire(self, key, seconds):
        return True

    async def delete(self, key):
        self.values.pop(key, None)

    def pipeline(self, transaction=True):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def rpush(self, key, *values):
        self.commands.append(("rpush", key, values))

    def ltrim(self, key, start, end):
        self.commands.append(("ltrim", key, start, end))

    def expire(self, key, seconds):
        self.commands.append(("expire", key, seconds))

    async def execute(self):
        for command in self.commands:
            method = getattr(self.redis, command[0])
            if command[0] == "rpush":
                await method(command[1], *command[2])
            else:
                await method(*command[1:])


def test_chat_agent_persists_history_in_redis_and_isolates_users(monkeypatch):
    import app.agents.chat_agent as module

    redis = _FakeRedis()
    calls = []

    async def fake_chat(_self, messages, **kwargs):
        calls.append(messages)
        return "回复"

    monkeypatch.setattr(module, "get_redis", lambda: redis)
    monkeypatch.setattr(module.LLMClient, "chat", fake_chat)
    agent = ChatAgent()
    first = AgentContext(user_id="u1", session_id="s1", scene="chat")
    second = AgentContext(user_id="u2", session_id="s1", scene="chat")

    assert asyncio.run(agent.execute("第一句", first)) == "回复"
    assert asyncio.run(agent.execute("第二句", first)) == "回复"
    assert asyncio.run(agent.execute("别人的消息", second)) == "回复"

    assert any(item.get("content") == "第一句" for item in calls[1])
    assert not any(item.get("content") == "第一句" for item in calls[2])
    assert len(redis.values) == 2
    assert any(json.loads(item)["content"] == "第一句" for item in next(iter(redis.values.values())))
