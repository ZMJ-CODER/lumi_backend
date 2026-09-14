"""刷新令牌的**接口语义**：轮换、宽限期重放、以及 401 必须带机器可读错误码。

## 为什么钉这些

现场事故（`docs/ARCHITECTURE_BOUNDARIES.md` §8.14.12）：客户端启动时用持久化的
refresh_token 续期，服务端返回

```
POST /auth/refresh → 401: 刷新令牌无效
```

客户端随后打印"游客模式启动，保留本地会话记录"，接着 `/conversations`、`/user/models`、
`/user/llm-config` 全部 401 —— 用户看到的就是"几乎所有信息都不显示"。

两个真实缺口：

1. **轮换不宽容**：旧 token 一旦被消耗立刻失效。桌面端双开窗口/重复启动、或持久化的是
   上一次写盘的旧副本时，抢输的那次必然 401，客户端只能降级成游客态。现在轮换后
   ``_REFRESH_GRACE_SECONDS`` 秒内重放旧 token 会**照常签发新令牌对**（幂等兜底）。
2. **401 只有文案**：客户端无法区分"access 过期（该刷新）"和"refresh 无效（该重登）"，
   只能字符串匹配，很容易静默降级。现在 401 一律带 ``data.error_code`` / ``data.action``。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1 import auth as auth_api
from app.core.deps import require_auth
from app.core.exceptions import UnauthorizedException
from app.models.auth import TokenRefreshRequest
from app.models.db_models import RefreshToken, User
from app.platform.security.security import hash_refresh_token

_USER_ID = uuid.UUID("58b3f64f-0d22-4ef8-a79f-69c19e32b9b8")


class _FakeRedis:
    """最小 Redis 替身：只实现宽限期用到的 set/get/delete（记录 TTL 便于断言）。"""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}
        self.deleted: list[str] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = str(value)
        self.ttls[key] = ex
        return True

    async def get(self, key: str):
        return self.kv.get(key)

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        self.kv.pop(key, None)
        return 1


class _FakeResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """按查询实体分派的假会话（只覆盖 refresh 端点用到的四种操作）。"""

    def __init__(self, *, record: RefreshToken | None = None, user: User | None = None) -> None:
        self.record = record
        self.user = user
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        if entity is RefreshToken:
            return _FakeResult(self.record)
        if entity is User:
            return _FakeResult(self.user)
        return _FakeResult(None)

    async def get(self, entity, primary_key):
        return self.user if entity is User else None

    def add(self, obj) -> None:
        self.added.append(obj)

    async def delete(self, obj) -> None:
        self.deleted.append(obj)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture()
def redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("app.core.redis.get_redis", lambda: fake)
    return fake


def _user() -> User:
    return User(
        id=_USER_ID,
        account="1111111111@example.com",
        username="1111111111@example.com",
        password_hash="x",
        role="superadmin",
        status="active",
    )


def _record(*, expires_in: int = 3600) -> RefreshToken:
    return RefreshToken(
        user_id=_USER_ID,
        token_hash=hash_refresh_token("raw-old-token"),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
    )


async def _refresh(db: _FakeSession, raw_token: str) -> dict:
    return await auth_api.refresh(TokenRefreshRequest(refresh_token=raw_token), db=db)


def test_rotation_issues_a_new_pair_and_remembers_grace(redis):
    db = _FakeSession(record=_record(), user=_user())
    body = asyncio.run(_refresh(db, "raw-old-token"))

    assert body["code"] == 0
    data = body["data"]
    assert data["access_token"] and data["refresh_token"] != "raw-old-token"
    assert data["user"]["role"] == "superadmin"
    assert db.deleted, "旧 refresh_token 记录必须被废弃（轮换语义不变）"
    assert db.added, "必须写入新的 refresh_token 记录"

    grace_key = f"auth:refresh_grace:{hash_refresh_token('raw-old-token')}"
    assert redis.kv[grace_key] == str(_USER_ID)
    assert redis.ttls[grace_key] == auth_api._REFRESH_GRACE_SECONDS


def test_replayed_token_within_grace_window_still_gets_a_new_pair(redis):
    """**事故复现**：同一个旧 token 被第二个窗口/第二次启动再用一次，不许 401。"""
    first = asyncio.run(_refresh(_FakeSession(record=_record(), user=_user()), "raw-old-token"))
    # 第二次：DB 里已经没有这条记录了（被第一个窗口轮换掉）
    replay_db = _FakeSession(record=None, user=_user())
    second = asyncio.run(_refresh(replay_db, "raw-old-token"))

    assert second["code"] == 0
    assert second["data"]["access_token"]
    assert second["data"]["refresh_token"] != first["data"]["refresh_token"], "重放也要轮换，不返回同一对"
    assert second["data"]["user"]["user_id"] == str(_USER_ID)


def test_unknown_token_reports_machine_readable_401(redis):
    db = _FakeSession(record=None, user=_user())
    with pytest.raises(UnauthorizedException) as caught:
        asyncio.run(_refresh(db, "totally-unknown-token"))
    exc = caught.value
    assert exc.status_code == 401
    assert exc.error_code == "REFRESH_TOKEN_INVALID"
    assert exc.data == {"error_code": "REFRESH_TOKEN_INVALID", "action": "relogin"}
    assert "重新登录" in exc.message


def test_expired_token_reports_its_own_code(redis):
    db = _FakeSession(record=_record(expires_in=-60), user=_user())
    with pytest.raises(UnauthorizedException) as caught:
        asyncio.run(_refresh(db, "raw-old-token"))
    assert caught.value.error_code == "REFRESH_TOKEN_EXPIRED"
    assert caught.value.data["action"] == "relogin"


def test_grace_window_is_short(redis):
    """宽限期只兜"同一客户端重放"，不能变成"旧 token 长期可用"。"""
    assert 0 < auth_api._REFRESH_GRACE_SECONDS <= 300


def test_require_auth_surfaces_login_required_code():
    with pytest.raises(UnauthorizedException) as caught:
        require_auth({})
    exc = caught.value
    assert exc.status_code == 401
    assert exc.data["error_code"] == "LOGIN_REQUIRED"
    assert exc.data["action"] == "refresh_or_relogin"


def test_all_auth_401_bodies_expose_error_code_through_the_global_handler():
    """前端只读 ``data.error_code``：必须能从 401 响应体里拿到，而不是只能读文案。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.exception_handlers import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/needs-login")
    async def needs_login():
        return require_auth({})

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/needs-login")

    assert response.status_code == 401
    body = response.json()
    assert body["data"]["error_code"] == "LOGIN_REQUIRED"
    assert body["data"]["action"] == "refresh_or_relogin"
