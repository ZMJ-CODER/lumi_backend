"""Read-view cache contract tests: user isolation, invalidation and fail-open behavior."""

import asyncio

from app.core import read_view_cache


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str):
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int):
        assert ex > 0
        self.values[key] = value

    async def delete(self, *keys: str):
        for key in keys:
            self.values.pop(key, None)

    async def scan_iter(self, *, match: str, count: int):
        del count
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key


def test_memory_view_cache_is_user_scoped_and_fail_open(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(read_view_cache, "get_redis", lambda: redis)

    async def run():
        first = read_view_cache.ReadViewTimer("memory_view")
        key_a = read_view_cache.memory_view_key("user-a")
        key_b = read_view_cache.memory_view_key("user-b")
        payload = {"code": 0, "data": {"facts": ["only-a"]}}
        await read_view_cache.set_read_view(key_a, payload, endpoint="memory_view", ttl_seconds=15, timer=first)
        assert await read_view_cache.get_read_view(key_a, endpoint="memory_view", timer=first) == payload
        assert await read_view_cache.get_read_view(key_b, endpoint="memory_view", timer=first) is None

        await read_view_cache.invalidate_memory_view("user-a")
        assert await read_view_cache.get_read_view(key_a, endpoint="memory_view", timer=first) is None

    asyncio.run(run())


def test_user_view_key_is_user_scoped():
    assert read_view_cache.user_view_key("user-a") != read_view_cache.user_view_key("user-b")
    assert read_view_cache.user_view_key("user-a") == "api:view:user:user-a"


def test_user_view_invalidation_clears_only_the_updated_user(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(read_view_cache, "get_redis", lambda: redis)

    async def run():
        timer = read_view_cache.ReadViewTimer("user_me")
        user_a = read_view_cache.user_view_key("user-a")
        user_b = read_view_cache.user_view_key("user-b")
        for key in (user_a, user_b):
            await read_view_cache.set_read_view(
                key, {"code": 0, "data": {"user_id": key}}, endpoint="user_me", ttl_seconds=5, timer=timer
            )

        await read_view_cache.invalidate_user_view("user-a")
        assert user_a not in redis.values
        assert user_b in redis.values

    asyncio.run(run())


def test_conversation_view_invalidation_clears_all_pages_for_one_user(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(read_view_cache, "get_redis", lambda: redis)

    async def run():
        timer = read_view_cache.ReadViewTimer("conversation_list")
        page_a = read_view_cache.conversation_view_key("user-a", scene="chat", limit=20, offset=0)
        page_b = read_view_cache.conversation_view_key("user-a", scene="all", limit=100, offset=20)
        other_user = read_view_cache.conversation_view_key("user-b", scene="chat", limit=20, offset=0)
        for key in (page_a, page_b, other_user):
            await read_view_cache.set_read_view(
                key, {"code": 0, "data": {"items": []}}, endpoint="conversation_list", ttl_seconds=10, timer=timer
            )

        await read_view_cache.invalidate_conversation_views("user-a")
        assert page_a not in redis.values
        assert page_b not in redis.values
        assert other_user in redis.values

    asyncio.run(run())


def test_cache_read_error_fails_open(monkeypatch):
    class BrokenRedis:
        async def get(self, key: str):
            raise ConnectionError(key)

    monkeypatch.setattr(read_view_cache, "get_redis", lambda: BrokenRedis())

    async def run():
        timer = read_view_cache.ReadViewTimer("memory_view")
        value = await read_view_cache.get_read_view("api:view:memory:user-a", endpoint="memory_view", timer=timer)
        assert value is None

    asyncio.run(run())
