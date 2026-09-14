"""管理员策略面板 API 的接线测试：权限、校验、以及"写完后本进程立即生效"。

重点是**权限**：这个面板能改线上超时/并发/启停，必须 ``require_superadmin``
（2026-09 起**只需超管 JWT**，二次密码 ``X-Admin-Token`` 已移除）。
不能沿用 ``POST /capabilities/admin/revoke`` 那种只校验 ``require_auth`` 的写法
（那是一个"任何登录用户都能撤销别人租约"的越权面，本次一并修掉了）。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import admin_policies
from app.core.deps import require_superadmin
from app.core.exceptions import BadRequestException, ForbiddenException


def _status_of(exc: Exception) -> int:
    """异常 → HTTP 状态（本仓库的异常类自带 status_code）。"""
    return int(getattr(exc, "status_code", 500) or 500)


class _FakeRedis:
    """最小 Redis 替身。

    注意**故意不实现 ``eval``**：CAS 发布脚本在真实 Redis 上执行，替身里会走
    ``_publish_epoch`` 的 GET/SET 回退路径 —— 这样测试同时覆盖了"旧客户端不支持
    eval"的降级分支。``hlen`` 必须实现：发布前的桶完整性校验靠它。
    """

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def get(self, key: str):
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self.kv[key] = value
        return True

    async def incr(self, key: str) -> int:
        value = int(self.kv.get(key) or "0") + 1
        self.kv[key] = str(value)
        return value

    async def hset(self, key: str, mapping: dict | None = None, **kwargs):
        self.hashes.setdefault(key, {}).update(mapping or {})
        if kwargs:
            self.hashes[key].update(kwargs)
        return len(mapping or kwargs)

    async def hgetall(self, key: str):
        return dict(self.hashes.get(key, {}))

    async def hlen(self, key: str) -> int:
        return len(self.hashes.get(key, {}))

    async def expire(self, key: str, seconds: int) -> bool:
        return True


@pytest.fixture()
def client(monkeypatch):
    from app.core.config import settings
    from app.services.runtime_policy import PolicyStore

    monkeypatch.setattr(settings, "RUNTIME_POLICY_OVERRIDE", True)
    redis = _FakeRedis()
    store = PolicyStore(_redis_factory=lambda: redis)
    monkeypatch.setattr(admin_policies, "policy_store", store)

    app = FastAPI()
    app.include_router(admin_policies.router, prefix="/admin/policies")

    async def _superadmin():
        return {"sub": "admin-1", "role": "superadmin"}

    app.dependency_overrides[require_superadmin] = _superadmin
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, store, redis


async def _call(fn, *args, **kwargs):
    """调用端点并归一化异常 → ``(status_code, body_or_exception)``。

    测试不依赖应用级的异常处理器（那是 ``app.main`` 的装配细节），因此直接 ``await``
    端点函数并检查本仓库异常类自带的 ``status_code``。这样"400 校验"这件事在任何
    装配方式下都被断言到，而不是被 TestClient 的 500 掩盖。

    **必须是 async**：同步测试里用 ``asyncio.get_event_loop()`` 会拿到上一个用例留下的
    已关闭事件循环，导致单独跑通过、和别的用例一起跑就失败（实测踩到）。
    """
    try:
        return (200, await fn(*args, **kwargs))
    except (BadRequestException, ForbiddenException) as exc:
        return (_status_of(exc), exc)


@pytest.mark.asyncio
async def test_put_policy_validates_scope_and_target(client):
    _api, _store, _redis = client
    status, body = await _call(
        admin_policies.upsert_policy, {"scope": "nope", "target": "x"}, {"sub": "a"}
    )
    assert status == 400
    assert "未知策略主体" in str(body)
    status, body = await _call(
        admin_policies.upsert_policy, {"scope": "provider", "target": ""}, {"sub": "a"}
    )
    assert status == 400
    assert "target" in str(body)


@pytest.mark.asyncio
async def test_put_policy_rejects_absurd_timeout(client):
    """手滑写 0.001 秒超时会把线上全部 LLM 调用打死，必须在入口拦住。"""
    _api, _store, _redis = client
    status, _body = await _call(
        admin_policies.upsert_policy,
        {"scope": "provider", "target": "p", "timeout_seconds": 0.001},
        {"sub": "a"},
    )
    assert status == 400
    status, _body = await _call(
        admin_policies.upsert_policy,
        {"scope": "provider", "target": "p", "timeout_seconds": 99999},
        {"sub": "a"},
    )
    assert status == 400
    status, _body = await _call(
        admin_policies.upsert_policy,
        {"scope": "provider", "target": "p", "max_concurrent": 0},
        {"sub": "a"},
    )
    assert status == 400


@pytest.mark.asyncio
async def test_put_policy_takes_effect_immediately_in_this_process(client):
    _api, store, _redis = client
    status, body = await _call(
        admin_policies.upsert_policy,
        {
            "scope": "provider",
            "target": "lumi.local.workspace",
            "timeout_seconds": 12.5,
            "max_concurrent": 2,
        },
        {"sub": "a"},
    )
    assert status == 200
    assert body["data"]["epoch"] == 1
    resolved = store.resolve(provider_id="lumi.local.workspace")
    assert resolved.source == "runtime"
    assert resolved.timeout_seconds == pytest.approx(12.5)
    assert resolved.max_concurrent == 2


@pytest.mark.asyncio
async def test_get_policy_snapshot_exposes_freshness(client):
    _api, store, _redis = client
    await _call(
        admin_policies.upsert_policy,
        {"scope": "model", "target": "deepseek", "timeout_seconds": 5},
        {"sub": "a"},
    )
    body = store.snapshot()
    assert body["enabled"] is True
    assert body["epoch"] == 1
    assert body["count"] == 1
    entry = body["entries"][0]
    assert entry["field"] == "model:deepseek"
    assert entry["usable"] is True
    assert "cache_age_seconds" in entry
    assert store.resolve(model="deepseek").source == "runtime"


@pytest.mark.asyncio
async def test_delete_policy_restores_defaults(client):
    _api, store, _redis = client
    await _call(
        admin_policies.upsert_policy,
        {"scope": "provider", "target": "p", "timeout_seconds": 5},
        {"sub": "a"},
    )
    assert store.resolve(provider_id="p").source == "runtime"
    status, _body = await _call(admin_policies.delete_policy, "provider:p", {"sub": "a"})
    assert status == 200
    assert store.resolve(provider_id="p").source == "code", "删掉覆盖后必须回到代码默认值"


@pytest.mark.asyncio
async def test_refresh_endpoint_reports_epoch_state(client):
    _api, _store, _redis = client
    status, body = await _call(admin_policies.refresh_policies, {"sub": "a"})
    assert status == 200
    assert body["code"] == 0
    assert "epoch" in body["data"]


@pytest.mark.asyncio
async def test_tool_registry_view_exposes_entries_and_shadow_diff():
    """工具注册表视图：条目 + 影子差异 + "能不能安全切真相源"的结论。"""
    from app.api.v1 import admin_policies

    response = await admin_policies.tool_registry_view({"sub": "admin-1"})
    assert response["code"] == 0
    data = response["data"]
    assert data["count"] >= 20
    assert data["derived_enabled"] is False, "默认关闭（静态表仍是唯一真相源）"
    from app.agents.capabilities.views.tool_shadow import SHADOW_PARITY_DIMENSIONS

    assert set(SHADOW_PARITY_DIMENSIONS) <= set(data["shadow_diff"])
    assert data["switch_safe"] is True, "当前判定维度必须无差异，否则不能切真相源"
    assert data["shadow_diff_total"] == 0
    # 披露维度（声明档位 / 声明窗口补位 / 资源层窗口 / 模型可见面收敛）单独计数：
    # 它们预览"打开开关后会变成什么"，**有意为之**，不参与 switch_safe。
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_DECLARED_DIMENSIONS,
        SHADOW_PARITY_DIMENSIONS,
    )

    disclosed = sum(len(data["shadow_diff"].get(key) or []) for key in SHADOW_DECLARED_DIMENSIONS)
    assert data["shadow_declared_total"] == disclosed
    assert data["shadow_missing_dimensions"] == []
    # 维度清单由后端给（前端不该硬编码）：判定维度 + 披露维度分开列出。
    assert data["shadow_dimensions"]["parity"] == list(SHADOW_PARITY_DIMENSIONS)
    assert data["shadow_dimensions"]["declared"] == list(SHADOW_DECLARED_DIMENSIONS)
    assert data["shadow_dimensions"]["missing"] == []
    # 统一资源能力层（Phase 1）的迁移进度：绑定数 + 未绑定清单都要如实回报
    assert data["resource_bound_count"] >= 20
    assert isinstance(data["resource_unbound_tools"], list)
    assert data["resource_catalog"]["legacy_bindings"]["workspace.read"]["capability"] == "resource.read"
    assert any(spec["name"] == "workspace_provider" for spec in data["resource_catalog"]["providers"])
    names = {row["name"] for row in data["entries"]}
    assert {"workspace_navigator", "workspace_write"} <= names
    # 条目必须带生效档位与审批策略：前端管理面板据此展示"这个工具什么档"。
    tiers = {row["name"]: row["risk_tier"] for row in data["entries"]}
    assert tiers["workspace_navigator"] == "auto"
    assert tiers["workspace_commit"] == "routine"
    assert tiers["workspace_rollback"] == "critical"


@pytest.mark.asyncio
async def test_write_lease_is_denied_when_gate_is_on_and_redis_is_down(client, monkeypatch):
    """写闸打开 + Redis 不可达 → 明确 400（写侧 Fail-Closed，不是静默放行）。"""
    from app.core.config import settings
    from app.services.write_gate import write_gate

    monkeypatch.setattr(settings, "WRITE_GATE_ENFORCEMENT", True)
    monkeypatch.setattr(write_gate, "_redis_factory", lambda: None)
    write_gate.clear_for_tests()
    try:
        status, body = await _call(admin_policies.grant_write_lease, {"scope": "workspace"}, {"sub": "a"})
        assert status == 400
        assert "Redis" in str(body)
    finally:
        write_gate.clear_for_tests()

