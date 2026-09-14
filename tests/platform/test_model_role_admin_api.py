"""模型档位管理接口契约回归（前端 `LlmRolesSettings` ↔ 后端）。

前端已经按下面这套契约实现（`src/services/adminSystem.js::LLM_CONFIG_ENDPOINTS`）：

    GET  /admin/llm-config/models        读取档位 + 角色映射
    PUT  /admin/llm-config/models        保存（{profiles?, roles?}）
    POST /admin/llm-config/models/reset  重置（{profile?}）
    POST /admin/llm-config/models/test   连通性测试（{profile}）

管理面授权口径（2026-09 裁决）：**只需超管 JWT**。历史实现还要求
``X-Admin-Token``（``/admin/verify-password`` 用管理员密码换 5 分钟 token），现场体验是
"点一下测试连接还要再输一遍密码"，已整体移除——请求里若仍带该 header 会被忽略。
本文件把"形状与鉴权"钉死，前端不需要再改一行。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.api.v1 import admin as admin_api
from app.platform.model import model_roles as mr
from app.models.admin import (
    ModelRolesResetRequest,
    ModelRolesTestRequest,
    ModelRolesUpdateRequest,
)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value
        return True

    async def delete(self, *keys: str):
        for key in keys:
            self.store.pop(key, None)
        return len(keys)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import app.core.redis as redis_module

    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    mr.invalidate_role_cache()
    yield
    mr.invalidate_role_cache()


ADMIN_PAYLOAD = {"sub": "admin-1", "role": "superadmin", "username": "admin"}


def test_routes_match_frontend_endpoint_names():
    """路径必须与前端 ``LLM_CONFIG_ENDPOINTS`` 完全一致（否则前端要改代码）。"""
    routes = {(sorted(route.methods)[0], route.path) for route in admin_api.router.routes}
    assert ("GET", "/llm-config/models") in routes
    assert ("PUT", "/llm-config/models") in routes
    assert ("POST", "/llm-config/models/reset") in routes
    assert ("POST", "/llm-config/models/test") in routes
    # 旧路径保留为兼容别名
    assert ("GET", "/model-roles") in routes
    assert ("PUT", "/model-roles/profile/{profile}") in routes


def test_read_view_shape_matches_frontend_normalizer():
    """``normalizeProfileConfig`` 期望的字段一个都不能少，且 roles 是 role→档位字符串。"""
    response = asyncio.run(admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))
    data = response["data"]

    assert isinstance(data["profiles"], list), "profiles 必须是数组（前端用 .map）"
    assert isinstance(data["roles"], dict), "roles 必须是对象"
    names = {item["profile"] for item in data["profiles"]}
    assert names == set(mr.CHAT_PROFILES)

    for item in data["profiles"]:
        for key in ("profile", "provider", "model", "base_url", "has_api_key", "api_key_masked",
                    "api_key_last4", "connectivity", "last_error", "config_source", "capabilities"):
            assert key in item, key
        caps = item["capabilities"]
        # 前端用毫秒/token 命名（PROFILE_CAPABILITY_KEYS）
        assert "timeout_ms" in caps and caps["timeout_ms"] > 0
        assert "max_output_tokens" in caps and "max_context_tokens" in caps
        for flag in ("supports_tools", "supports_json", "supports_vision", "supports_reasoning"):
            assert flag in caps, flag
        assert isinstance(item["role_overrides"], dict), "配置页要显示'被哪些角色使用'"

    for role in mr.ALL_ROLES:
        assert isinstance(data["roles"][role], str), f"{role} 必须是档位名（字符串）"
        assert data["roles"][role] in mr.CHAT_PROFILES
    # 密钥绝不出现明文
    assert '"api_key"' not in json.dumps(data)


def test_write_accepts_profiles_and_roles_partials():
    """PUT：前端只提交被改动的部分（档位字段子集 / 角色映射）。"""

    async def main():
        result = await admin_api.update_llm_models(
            req=ModelRolesUpdateRequest(
                profiles={
                    mr.PROFILE_CHEAP: {
                        "model": "cheap-custom",
                        "timeout_ms": 30_000,
                        "max_output_tokens": 2048,
                        "supports_tools": False,
                    }
                },
                roles={mr.ROLE_TITLE: mr.PROFILE_MAIN},
            ),
            payload=ADMIN_PAYLOAD,
        )
        view = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        resolved_cheap = await mr.resolve_role(mr.ROLE_SUMMARY)
        resolved_title = await mr.resolve_role(mr.ROLE_TITLE)
        return result, view, resolved_cheap, resolved_title

    result, view, resolved_cheap, resolved_title = asyncio.run(main())
    assert "立即生效" in result["data"]["message"]
    assert result["data"]["profiles"] == [mr.PROFILE_CHEAP]
    assert result["data"]["roles"] == [mr.ROLE_TITLE]

    cheap_entry = next(item for item in view["profiles"] if item["profile"] == mr.PROFILE_CHEAP)
    assert cheap_entry["model"] == "cheap-custom"
    assert cheap_entry["config_source"] == "admin"
    assert cheap_entry["capabilities"]["timeout_ms"] == 30_000
    assert cheap_entry["capabilities"]["max_output_tokens"] == 2048
    assert cheap_entry["capabilities"]["supports_tools"] is False

    # 能力覆盖真的生效到解析结果（不只是展示）
    assert resolved_cheap.model == "cheap-custom"
    assert resolved_cheap.timeout == 30.0
    assert resolved_cheap.capabilities["max_tokens"] == 2048
    assert resolved_cheap.supports_tools is False
    # 角色映射也生效：title 现在指向 main
    assert view["roles"][mr.ROLE_TITLE] == mr.PROFILE_MAIN
    assert resolved_title.profile == mr.PROFILE_MAIN


def test_write_keeps_untouched_fields_and_api_key_when_omitted():
    """api_key 留空 = 不修改；其它字段缺失 = 保留（前端差异提交语义）。"""

    async def main():
        await admin_api.update_llm_models(
            req=ModelRolesUpdateRequest(profiles={
                mr.PROFILE_MAIN: {
                    "base_url": "https://api.deepseek.com",
                    "api_key": "sk-first-value",
                    "model": "main-first",
                }
            }),
            payload=ADMIN_PAYLOAD,
        )
        await admin_api.update_llm_models(
            req=ModelRolesUpdateRequest(profiles={mr.PROFILE_MAIN: {"model": "main-second"}}),
            payload=ADMIN_PAYLOAD,
        )
        view = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        resolved = await mr.resolve_role(mr.ROLE_DIRECT_ANSWER)
        return view, resolved

    view, resolved = asyncio.run(main())
    entry = next(item for item in view["profiles"] if item["profile"] == mr.PROFILE_MAIN)
    assert entry["model"] == "main-second"
    assert entry["api_key_last4"] == "alue", "未提交 api_key 时旧密钥必须保留"
    assert "sk-first-value" not in json.dumps(view), "只回末 4 位，不回明文"
    assert resolved.api_key == "sk-first-value"


def test_reset_single_profile_and_reset_all():
    async def main():
        await admin_api.update_llm_models(
            req=ModelRolesUpdateRequest(
                profiles={mr.PROFILE_CHEAP: {"model": "temp-cheap"}},
                roles={mr.ROLE_TITLE: mr.PROFILE_MAIN},
            ),
            payload=ADMIN_PAYLOAD,
        )
        one = await admin_api.reset_llm_models(
            req=ModelRolesResetRequest(profile=mr.PROFILE_CHEAP),
            payload=ADMIN_PAYLOAD,
        )
        after_one = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        all_ = await admin_api.reset_llm_models(
            req=ModelRolesResetRequest(), payload=ADMIN_PAYLOAD
        )
        after_all = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        return one, after_one, all_, after_all

    one, after_one, all_, after_all = asyncio.run(main())
    assert "cheap" in one["data"]["message"]
    cheap_entry = next(item for item in after_one["profiles"] if item["profile"] == mr.PROFILE_CHEAP)
    assert cheap_entry["model"] != "temp-cheap", "单档位重置必须回落 .env"
    assert after_one["roles"][mr.ROLE_TITLE] == mr.PROFILE_MAIN, "单档位重置不影响角色映射"
    assert after_all["roles"][mr.ROLE_TITLE] == mr.DEFAULT_ROLE_PROFILES[mr.ROLE_TITLE], "全部重置回落默认表"


def test_connectivity_test_endpoint_shape(monkeypatch):
    """POST /models/test：返回 {ok, error, latency_ms}，并写入连通性状态。"""
    calls: list[dict] = []

    async def fake_validate(cfg):
        calls.append(dict(cfg))
        return True, ""

    monkeypatch.setattr("app.platform.model.llm_config.validate_llm_config", fake_validate)

    async def main():
        ok = await admin_api.test_llm_model_profile(
            req=ModelRolesTestRequest(profile=mr.PROFILE_CHEAP),
            payload=ADMIN_PAYLOAD,
        )
        view = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        return ok, view

    ok, view = asyncio.run(main())
    assert ok["data"]["ok"] is True
    assert ok["data"]["error"] == ""
    assert isinstance(ok["data"]["latency_ms"], int)
    assert calls and calls[0]["model"], "测试必须带真实档位模型"
    entry = next(item for item in view["profiles"] if item["profile"] == mr.PROFILE_CHEAP)
    assert entry["connectivity"] == "ok"


def test_connectivity_test_failure_is_reported_not_raised(monkeypatch):
    async def fake_validate(cfg):
        return False, "MODEL_AUTH_ERROR: 401"

    monkeypatch.setattr("app.platform.model.llm_config.validate_llm_config", fake_validate)

    async def main():
        result = await admin_api.test_llm_model_profile(
            req=ModelRolesTestRequest(profile=mr.PROFILE_MAIN),
            payload=ADMIN_PAYLOAD,
        )
        view = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        return result, view

    result, view = asyncio.run(main())
    assert result["data"]["ok"] is False
    assert "401" in result["data"]["error"]
    entry = next(item for item in view["profiles"] if item["profile"] == mr.PROFILE_MAIN)
    assert entry["connectivity"] == "error"
    assert "401" in entry["last_error"]


def test_unknown_profile_is_rejected_as_bad_request():
    """未知档位名必须是 400（`BadRequestException`），不能 500 或静默当成默认档位。"""
    from app.core.exceptions import BadRequestException

    async def main():
        errors = []
        for make_call in (
            lambda: admin_api.reset_llm_models(
                req=ModelRolesResetRequest(profile="bogus"),
                payload=ADMIN_PAYLOAD,
            ),
            lambda: admin_api.test_llm_model_profile(
                req=ModelRolesTestRequest(profile="bogus"),
                payload=ADMIN_PAYLOAD,
            ),
        ):
            try:
                await make_call()
                errors.append(None)
            except BadRequestException as exc:  # noqa: PERF203 - 断言异常类型与文案
                errors.append(exc)
        return errors

    errors = asyncio.run(main())
    assert all(err is not None for err in errors), "未知档位必须被拒绝"
    assert all("bogus" in str(err) for err in errors)


def test_write_requires_only_superadmin_jwt():
    """写操作只需超管 JWT：不再要求 ``X-Admin-Token``（2026-09 二次密码验证已移除）。

    这是"管理员登录一次就能改配置/测连接"的契约；端点签名里已经没有
    ``x_admin_token`` 这个参数，带了也不会更严格、不带也不会拒绝。
    """
    import inspect

    assert "x_admin_token" not in inspect.signature(admin_api.update_llm_models).parameters

    async def main():
        written = await admin_api.update_llm_models(
            req=ModelRolesUpdateRequest(roles={mr.ROLE_TITLE: mr.PROFILE_MAIN}),
            payload=ADMIN_PAYLOAD,
        )
        view = (await admin_api.get_llm_models_view(payload=ADMIN_PAYLOAD))["data"]
        return written, view

    written, view = asyncio.run(main())
    assert written["code"] == 0, written
    assert view["roles"][mr.ROLE_TITLE] == mr.PROFILE_MAIN


def test_verify_password_endpoint_is_kept_for_legacy_clients_but_deprecated():
    """``POST /admin/verify-password`` 保留（旧客户端不 404）但明确标注已废弃。"""
    from app.models.knowledge import AdminPasswordVerifyRequest
    from app.platform.security.security import hash_password

    class _FakeSession:
        def __init__(self, user) -> None:
            self._user = user

        async def get(self, entity, pk):
            return self._user

    user = type("FakeUser", (), {})()
    user.id = "58b3f64f-0d22-4ef8-a79f-69c19e32b9b8"
    user.username = "admin"
    user.password_hash = hash_password("q12345678")

    result = asyncio.run(
        admin_api.verify_admin_password(
            req=AdminPasswordVerifyRequest(admin_password="q12345678"),
            db=_FakeSession(user),
            payload={**ADMIN_PAYLOAD, "sub": "58b3f64f-0d22-4ef8-a79f-69c19e32b9b8"},
        )
    )
    assert result["data"]["deprecated"] is True
    assert result["data"]["verified_token"], "仍然签发 token，旧客户端不会因此报错"
