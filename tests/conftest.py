"""pytest 公共 fixtures."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.core.redis import init_redis, close_redis


@pytest.fixture
async def client():
    """创建测试用的异步 HTTP 客户端.

    TestClient 不会触发 lifespan 事件，因此手动初始化 Redis.
    """
    await init_redis()
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await close_redis()