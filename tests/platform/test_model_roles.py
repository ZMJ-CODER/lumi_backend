"""模型档位 / 职责角色回归（方案 Phase 1–2 的落点）。

锁死四件事：

1. **档位解析**：``main`` 走旧全局配置；``cheap`` 落到已有的低成本模型；
   ``vision`` 固定视觉模型；档位缺配置时回退 ``main``（"只配了 main 也能跑"）。
2. **职责映射**：低风险中间职责默认 ``cheap``（标题/摘要/意图/查询改写/记忆），
   最终回答与写代码保持 ``main``，执行/审查用 ``reasoning``；
   ``LLM_ROLE_*`` 与 Redis 动态配置都能覆盖，且**不需要重启**。
3. **密钥不外发**：``public_dict`` / Job 快照 / 管理视图都不含 api_key。
4. **冻结计划**：``ModelPlan`` 记录角色→档位→模型，任务期间不随配置变化。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.platform.model import model_roles as mr
from app.core.config import settings


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
def _clear_cache():
    mr.invalidate_role_cache()
    yield
    mr.invalidate_role_cache()


def _install_redis(monkeypatch) -> _FakeRedis:
    import app.core.redis as redis_module

    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    return fake


# ── 1. 档位解析 ──────────────────────────────────────────


def test_main_profile_follows_legacy_env():
    resolved = asyncio.run(mr.resolve_role(mr.ROLE_DIRECT_ANSWER))
    assert resolved.profile == mr.PROFILE_MAIN
    assert resolved.model  # 来自旧全局配置（DEEPSEEK_MODEL/QWEN_MODEL）
    assert resolved.supports_tools is True, "最终回答档位必须保留工具能力"


def test_cheap_profile_uses_existing_low_cost_model():
    resolved = asyncio.run(mr.resolve_role(mr.ROLE_TITLE))
    assert resolved.profile == mr.PROFILE_CHEAP
    # 没有配 LLM_CHEAP_* 时，落到项目已有的低成本模型（DS_FLASH_MODEL / QWEN_TURBO_MODEL）
    expected = {
        str(getattr(settings, "DS_FLASH_MODEL", "") or ""),
        str(getattr(settings, "QWEN_TURBO_MODEL", "") or ""),
        settings.QWEN_MODEL,
    } - {""}
    assert resolved.model in expected, resolved.model
    assert resolved.supports_tools is False, "低成本档位默认声明不支持工具调用"


def test_vision_role_always_uses_vision_profile():
    resolved = asyncio.run(mr.resolve_role("vision"))
    assert resolved.profile == mr.PROFILE_VISION
    assert resolved.model == settings.VL_MODEL
    assert resolved.supports_vision is True


def test_missing_profile_config_falls_back_to_main(monkeypatch):
    """cheap 档位没有任何可用模型时必须回退 main，而不是发空模型请求。"""
    monkeypatch.setattr(settings, "DS_FLASH_MODEL", "", raising=False)
    monkeypatch.setattr(settings, "QWEN_TURBO_MODEL", "", raising=False)
    monkeypatch.setattr(settings, "QWEN_MODEL", "", raising=False)
    resolved = asyncio.run(mr.resolve_role(mr.ROLE_TITLE))
    assert resolved.model, "回退后必须仍有可用模型"
    assert resolved.profile in {mr.PROFILE_CHEAP, mr.PROFILE_MAIN}


def test_profile_capabilities_are_declared():
    caps = mr.profile_capabilities(mr.PROFILE_CHEAP)
    for key in ("supports_tools", "supports_json", "supports_vision", "supports_reasoning", "timeout", "max_tokens", "max_context"):
        assert key in caps, key
    assert caps["supports_json"] is True


# ── 2. 职责映射（含动态覆盖，不重启生效）──────────────────


def test_default_role_mapping_matches_plan():
    expectations = {
        mr.ROLE_TITLE: mr.PROFILE_CHEAP,
        mr.ROLE_SUMMARY: mr.PROFILE_CHEAP,
        mr.ROLE_INTENT_ASSESSOR: mr.PROFILE_CHEAP,
        mr.ROLE_QUERY_REWRITER: mr.PROFILE_CHEAP,
        mr.ROLE_MEMORY_EXTRACT: mr.PROFILE_CHEAP,
        mr.ROLE_PLANNER_SIMPLE: mr.PROFILE_CHEAP,
        mr.ROLE_PLANNER_COMPLEX: mr.PROFILE_MAIN,
        mr.ROLE_TOOL_READ: mr.PROFILE_CHEAP,
        mr.ROLE_TOOL_WRITE: mr.PROFILE_MAIN,
        mr.ROLE_TOOL_EXECUTE: mr.PROFILE_REASONING,
        mr.ROLE_DIRECT_ANSWER: mr.PROFILE_MAIN,
        mr.ROLE_FINAL_SUMMARY: mr.PROFILE_MAIN,
        mr.ROLE_CODE_WRITER: mr.PROFILE_MAIN,
        mr.ROLE_CODE_REVIEWER: mr.PROFILE_REASONING,
    }
    for role, profile in expectations.items():
        assert mr.role_profile(role) == profile, role


def test_env_can_pin_every_role_to_main(monkeypatch):
    """Phase 1 的"先都不改行为"：把角色都指回 main 即可。"""
    for role in mr.ALL_ROLES:
        monkeypatch.setattr(settings, f"LLM_ROLE_{role.upper()}", "main", raising=False)
    for role in (mr.ROLE_TITLE, mr.ROLE_TOOL_READ, mr.ROLE_PLANNER_SIMPLE):
        assert mr.role_profile(role) == mr.PROFILE_MAIN


def test_admin_can_switch_a_record_without_restart(monkeypatch):
    _install_redis(monkeypatch)
    monkeypatch.setattr(settings, "LLM_ROLE_TITLE", "", raising=False)

    async def main():
        before = await mr.resolve_role(mr.ROLE_TITLE)
        assert before.profile == mr.PROFILE_CHEAP
        # 管理员把 title 指回 main（写 Redis，立即生效，无需重启）
        await mr.set_role_profile(mr.ROLE_TITLE, mr.PROFILE_MAIN)
        monkeypatch.setattr(settings, "LLM_ROLE_TITLE", "main", raising=False)
        after = await mr.resolve_role(mr.ROLE_TITLE)
        return before, after

    before, after = asyncio.run(main())
    assert after.profile == mr.PROFILE_MAIN


def test_admin_profile_override_wins_and_reset_restores_env(monkeypatch):
    _install_redis(monkeypatch)

    async def main():
        base = await mr.resolve_role(mr.ROLE_TITLE)
        await mr.set_profile_config(mr.PROFILE_CHEAP, {
            "provider": "deepseek", "base_url": "https://api.deepseek.com",
            "api_key": "sk-admin", "model": "cheap-admin-model",
        })
        overridden = await mr.resolve_role(mr.ROLE_TITLE)
        assert overridden.model == "cheap-admin-model"
        assert overridden.source == "admin"
        await mr.set_profile_config(mr.PROFILE_CHEAP, None)
        return base, await mr.resolve_role(mr.ROLE_TITLE)

    base, restored = asyncio.run(main())
    assert restored.model == base.model, "reset 后必须回到 .env/默认档位"


# ── 3. 密钥不外发 ────────────────────────────────────────


def test_public_views_never_expose_api_key(monkeypatch):
    _install_redis(monkeypatch)

    async def main():
        resolved = await mr.resolve_role(mr.ROLE_DIRECT_ANSWER)
        view = await mr.role_config_view()
        return resolved, view

    resolved, view = asyncio.run(main())
    # "没有名为 api_key 的字段"（api_key_masked / api_key_last4 是脱敏展示，不是密钥本体）
    assert '"api_key"' not in json.dumps(resolved.public_dict())
    assert '"api_key"' not in json.dumps(view["roles"])
    assert '"api_key"' not in json.dumps(view["profiles"])
    main_entry = next(item for item in view["profiles"] if item["profile"] == mr.PROFILE_MAIN)
    assert "has_api_key" in main_entry and "api_key_masked" in main_entry
    assert "api_key_last4" in main_entry
    secret = str(resolved.api_key or "")
    if len(secret) > 4:
        assert secret not in json.dumps(view, ensure_ascii=False), "档位视图不得回显密钥原文"
        assert main_entry["api_key_last4"] == secret[-4:], "只允许给末 4 位"


# ── 4. 冻结计划 ──────────────────────────────────────────


def test_model_plan_freezes_roles_and_hides_secrets(monkeypatch):
    _install_redis(monkeypatch)

    from app.platform.model import model_plan as mp

    async def main():
        plan = await mp.build_model_plan(scene="office", user_id=None, plan_id="plan-1")
        # 任务中途"管理员改了配置"：已建计划不受影响
        await mr.set_profile_config(mr.PROFILE_MAIN, {
            "provider": "qwen", "base_url": "http://127.0.0.1:11434/v1",
            "api_key": "sk-later", "model": "changed-after-freeze",
        })
        return plan, await mp.load_model_plan("plan-1")

    plan, reloaded = asyncio.run(main())
    assert plan.roles[mr.ROLE_DIRECT_ANSWER]["model"] == reloaded.roles[mr.ROLE_DIRECT_ANSWER]["model"]
    assert reloaded.roles[mr.ROLE_TITLE]["profile"] == mr.PROFILE_CHEAP
    blob = json.dumps(plan.public_dict(), ensure_ascii=False)
    assert "api_key" not in blob
    assert plan.public_dict()["roles"][mr.ROLE_TITLE]["model"]


def test_frozen_plan_supplies_llm_config_without_leaking_into_snapshot(monkeypatch):
    _install_redis(monkeypatch)

    from app.platform.model import model_plan as mp

    async def main():
        plan = await mp.build_model_plan(scene="office", plan_id="plan-2")
        cfg = await mp.model_plan_llm_config(plan, mr.ROLE_TITLE)
        return plan, cfg

    plan, cfg = asyncio.run(main())
    assert cfg["model"] == plan.roles[mr.ROLE_TITLE]["model"]
    assert cfg["source"].startswith("model_plan:")
    # 计划公开视图（落 Job 快照的那份）不含密钥
    assert "api_key" not in json.dumps(plan.public_dict())


def test_role_label_is_user_facing_without_internals():
    from app.platform.model.model_plan import role_label

    assert role_label(mr.ROLE_TITLE) == "快速模型"
    assert role_label(mr.ROLE_DIRECT_ANSWER) == "标准模型"
    assert role_label(mr.ROLE_TOOL_EXECUTE) == "深度模型"
    assert role_label(mr.ROLE_VISION) == "视觉模型"


# ── 5. LLMClient 真的按角色路由（含遥测）───────────────────


def test_llm_client_routes_by_role_and_records_role(monkeypatch):
    """``LLMClient.chat(role=...)``：用角色的模型发请求，并把角色写进用量。"""
    from app.platform.model.llm import LLMClient

    captured: dict = {}

    class _Reply:
        content = "ok"
        usage_metadata = {"input_tokens": 3, "output_tokens": 2}

    class _Model:
        async def ainvoke(self, _messages):
            return _Reply()

    async def fake_get_chat_model(**kwargs):
        captured.update(kwargs)
        return _Model()

    recorded: dict = {}

    async def fake_record_usage(user_id, category, model, prompt_tokens, completion_tokens, **kwargs):
        recorded.update(
            {"model": model, "category": category, "prompt": prompt_tokens, "completion": completion_tokens, **kwargs}
        )

    monkeypatch.setattr("app.platform.model.llm.get_chat_model", fake_get_chat_model)
    monkeypatch.setattr("app.platform.model.llm.record_usage", fake_record_usage)

    text = asyncio.run(
        LLMClient().chat(
            [{"role": "user", "content": "hi"}],
            role=mr.ROLE_TITLE,
            usage_category="title",
        )
    )
    assert text == "ok"
    expected = asyncio.run(mr.resolve_role(mr.ROLE_TITLE))
    assert captured["model"] == expected.model, "必须用 title 角色的档位模型"
    assert captured["base_url"].rstrip("/") == expected.base_url.rstrip("/")
    assert recorded["model_role"] == mr.ROLE_TITLE
    assert recorded["model_profile"] == mr.PROFILE_CHEAP
    assert recorded["config_source"], "配置来源要落遥测"


def test_llm_client_role_failure_falls_back_to_main(monkeypatch):
    """intent_assessor（cheap）失败 → 按回退策略换 main 重试一次。"""
    from app.platform.model.llm import LLMClient

    seen_models: list[str] = []

    class _Reply:
        content = "ok"
        usage_metadata = {"input_tokens": 1, "output_tokens": 1}

    class _Model:
        def __init__(self, model: str) -> None:
            self._model = model

        async def ainvoke(self, _messages):
            seen_models.append(self._model)
            if len(seen_models) == 1:
                raise TimeoutError("cheap model timeout")
            return _Reply()

    async def fake_get_chat_model(**kwargs):
        return _Model(str(kwargs.get("model") or ""))

    async def fake_record_usage(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.platform.model.llm.get_chat_model", fake_get_chat_model)
    monkeypatch.setattr("app.platform.model.llm.record_usage", fake_record_usage)

    text = asyncio.run(
        LLMClient().chat(
            [{"role": "user", "content": "hi"}],
            role=mr.ROLE_INTENT_ASSESSOR,
            usage_category="plan",
        )
    )
    assert text == "ok"
    cheap = asyncio.run(mr.resolve_role(mr.ROLE_INTENT_ASSESSOR))
    main = asyncio.run(mr.resolve_role(mr.ROLE_DIRECT_ANSWER))
    assert seen_models[0] == cheap.model
    assert seen_models[1] == main.model, "结构化/评估类角色失败必须换 main 重试"


def test_llm_client_without_role_keeps_legacy_behaviour(monkeypatch):
    """不传 role 时完全沿用既有行为（回归护栏）。"""
    from app.platform.model.llm import LLMClient

    captured: dict = {}

    class _Reply:
        content = "legacy-ok"
        usage_metadata = {}

    class _Model:
        async def ainvoke(self, _messages):
            return _Reply()

    async def fake_get_chat_model(**kwargs):
        captured.update(kwargs)
        return _Model()

    async def fake_get_llm_config(scene, provider, user_id=None):
        return {
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-legacy",
            "model": "legacy-model",
            "timeout": 99,
        }

    async def fake_record_usage(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.platform.model.llm.get_chat_model", fake_get_chat_model)
    monkeypatch.setattr("app.platform.model.llm.get_llm_config", fake_get_llm_config)
    monkeypatch.setattr("app.platform.model.llm.record_usage", fake_record_usage)

    text = asyncio.run(LLMClient().chat([{"role": "user", "content": "hi"}], scene="office"))
    assert text == "legacy-ok"
    assert captured["model"] == "legacy-model"
    assert captured["api_key"] == "sk-legacy"

    # 契约表覆盖：所有默认角色都有档位，且档位/能力声明齐全
    for role in mr.ALL_ROLES:
        assert mr.role_profile(role) in mr.CHAT_PROFILES
        assert mr.role_fallback_policy(role) in {"none", "main", "original"}
    for profile in mr.CHAT_PROFILES:
        caps = mr.profile_capabilities(profile)
        assert isinstance(caps.get("max_context"), int) and caps["max_context"] > 0
