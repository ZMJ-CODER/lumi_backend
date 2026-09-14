"""Model Capability Router 接线回归（``MODEL_CAPABILITY_ROUTER_V2``）。

锁死五件事：

1. 结构化结论的**精确键集**（前端 ``src/services/modelPlan.js::describeModelRouting``
   读的 ``reason_code`` / ``from_profile`` / ``to_profile`` / ``excluded[]`` /
   ``switch_allowed`` 必须真的存在），以及 ``excluded[]`` 条目形状；
2. **开关关闭**时逐字等价（计划公开视图、角色表、运行态载荷都不变）；
3. 能力规则：工具缺失 → 阻断且**不调用模型**；视觉缺失 → 换档或阻断（带 ``excluded[]``
   原因）；主档位不可用 → 降级；能力允许 → 优先低成本档位；
4. **BYOK 用户模型不参与默认切换**；
5. ModelPlan 冻结后任务期间不变（管理员改配置也不影响在跑的任务）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.platform.model import model_roles as mr
from app.core.config import settings
from app.platform.model.model_capability_router import (
    EXCLUDED_ENTRY_KEYS,
    MODEL_ROUTING_KEYS,
    MODEL_ROUTING_NOTICE_KEY,
    CapabilityRequirements,
    ModelCandidate,
    ModelCapabilityRouter,
    attach_model_routing,
    model_capability_router,
    model_routing_process_frame,
)
from app.platform.model.model_plan import ModelPlan

#: 前端契约层**必须**读到的字段（缺一个界面就永远空白）。
from _paths import REPO_ROOT
FRONTEND_REQUIRED_KEYS: tuple[str, ...] = (
    "reason_code",
    "from_profile",
    "to_profile",
    "excluded",
    "switch_allowed",
)

REPO_ROOT = REPO_ROOT


# ── 夹具 ─────────────────────────────────────────────────


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
def _clear_role_cache():
    mr.invalidate_role_cache()
    yield
    mr.invalidate_role_cache()


@pytest.fixture()
def router_off(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_CAPABILITY_ROUTER_V2", False, raising=False)
    return False


@pytest.fixture()
def router_on(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_CAPABILITY_ROUTER_V2", True, raising=False)
    return True


def _install_redis(monkeypatch) -> _FakeRedis:
    import app.core.redis as redis_module

    fake = _FakeRedis()
    monkeypatch.setattr(redis_module, "get_redis", lambda: fake)
    return fake


def _entry(
    profile: str,
    model: str,
    *,
    tools: bool = True,
    vision: bool = False,
    streaming: bool = True,
    max_context: int = 128_000,
    byok: bool = False,
    provider: str = "deepseek",
) -> dict[str, Any]:
    return {
        "role": "direct_answer",
        "profile": profile,
        "provider": provider,
        "model": model,
        "source": "env",
        "byok": byok,
        "timeout": 60.0,
        "capabilities": {
            "supports_tools": tools,
            "supports_json": True,
            "supports_vision": vision,
            "supports_streaming": streaming,
            "supports_reasoning": False,
            "max_context": max_context,
            "timeout": 60.0,
            "max_tokens": 4096,
        },
    }


def _plan(
    *,
    main: dict[str, Any] | None = None,
    cheap: dict[str, Any] | None = None,
    reasoning: dict[str, Any] | None = None,
    vision: dict[str, Any] | None = None,
    byok: bool = False,
    tools: bool = False,
) -> ModelPlan:
    """四档位候选的最小冻结计划（+ 角色级工具角色，供 refine_roles 断言）。"""
    roles = {
        "direct_answer": main or _entry("main", "main-model"),
        "final_summary": main or _entry("main", "main-model"),
        "planner_complex": main or _entry("main", "main-model"),
        "tool_write": main or _entry("main", "main-model"),
        "tool_read": cheap or _entry("cheap", "cheap-model", tools=False),
        "title": cheap or _entry("cheap", "cheap-model", tools=False),
        "summary": cheap or _entry("cheap", "cheap-model", tools=False),
        "tool_execute": reasoning or _entry("reasoning", "reason-model"),
        "code_reviewer": reasoning or _entry("reasoning", "reason-model"),
        "vision": vision or _entry("vision", "vision-model", tools=False, vision=True),
    }
    if tools:
        for name in ("direct_answer", "final_summary", "planner_complex", "tool_write"):
            roles[name] = {**roles[name], "capabilities": {**roles[name]["capabilities"], "supports_tools": True}}
    return ModelPlan(plan_id="plan-test", byok=byok, scene="office", roles=roles)


def _tools_missing_plan() -> ModelPlan:
    """没有任何档位支持工具的计划（工具缺失 → 硬阻断）。"""
    return _plan(
        main=_entry("main", "main-no-tools", tools=False),
        cheap=_entry("cheap", "cheap-no-tools", tools=False),
        reasoning=_entry("reasoning", "reason-no-tools", tools=False),
    )


def _router() -> ModelCapabilityRouter:
    """无注册表噪音的路由器（注册表候选单独测）。"""
    return ModelCapabilityRouter(catalog=lambda: [])


def _route(plan: ModelPlan, requirements: CapabilityRequirements, **kwargs):
    return asyncio.run(_router().route(plan=plan, requirements=requirements, **kwargs))


# ── 1. 前端冻结契约 ──────────────────────────────────────


def test_model_routing_key_set_is_exactly_pinned():
    conclusion = _route(_plan(), CapabilityRequirements())
    payload = conclusion.to_routing()
    assert set(payload) == set(MODEL_ROUTING_KEYS), "结论键集变化必须同步前端契约"
    for key in FRONTEND_REQUIRED_KEYS:
        assert key in payload, f"前端 describeModelRouting 读不到 {key}"
    assert isinstance(payload["excluded"], list)
    assert isinstance(payload["switch_allowed"], bool)
    # 每个 excluded 条目都解释"为什么被拒绝"
    switched = _route(_plan(), CapabilityRequirements(needs_vision=True))
    assert switched.excluded, "换档时必须有候选被排除的解释"
    for item in switched.excluded:
        assert set(item) == set(EXCLUDED_ENTRY_KEYS)
        assert item["model"] and item["reason_code"]
    assert json.dumps(payload, ensure_ascii=False)  # 必须可直接进 Job 快照


def test_routing_snapshot_and_process_notice_use_existing_channels():
    conclusion = _route(_plan(), CapabilityRequirements(needs_vision=True))
    routing = attach_model_routing({"llm": {"model": "main-model"}}, conclusion)
    payload = routing["model_routing"]
    assert payload["reason_code"] == "VISION_REQUIRED"
    assert payload["from_profile"] == "main" and payload["to_profile"] == "vision"
    assert payload["switch_allowed"] is True
    notice = routing[MODEL_ROUTING_NOTICE_KEY]
    assert set(notice) == {"kind", "title", "summary", "status", "detail"}
    assert notice["status"] == "completed" and notice["summary"]
    frame = model_routing_process_frame(routing)
    assert frame is not None and frame["type"] == "process"
    assert frame["entry_id"] == "process:model_routing"
    assert frame["summary"] == notice["summary"]
    # 没有变化时不写 notice（界面不该出现"其实没降级"的提示）
    quiet = attach_model_routing({}, _route(_plan(), CapabilityRequirements(needs_tools=True)))
    assert MODEL_ROUTING_NOTICE_KEY not in quiet
    assert quiet["model_routing"]["reason_code"] == ""
    assert quiet["model_routing"]["degraded"] is False


# ── 2. 开关关闭 = 逐字等价 ───────────────────────────────


def test_flag_off_keeps_plan_and_runtime_payload_identical(monkeypatch, router_off):
    import app.platform.model.model_plan as mp

    fake = _install_redis(monkeypatch)
    assert model_capability_router.enabled() is False

    base = asyncio.run(mp.build_model_plan(scene="office", plan_id="plan-off-1"))
    with_requirements = asyncio.run(
        mp.build_model_plan(
            scene="office",
            plan_id="plan-off-2",
            requirements=CapabilityRequirements(needs_tools=True, needs_vision=True),
        )
    )

    def _view(plan: ModelPlan) -> str:
        data = plan.public_dict()
        data.pop("created_at")
        data.pop("plan_id")
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    assert _view(base) == _view(with_requirements), "开关关闭时需求参数必须被忽略"
    assert base.roles == with_requirements.roles
    assert base.model_routing == {} and with_requirements.model_routing == {}
    # 关闭时连计划对象都不重建（identity 证明"零介入"）
    same = asyncio.run(
        mp._refine_with_capability_router(
            base,
            requirements=CapabilityRequirements(needs_tools=True),
            scene="office",
            user_id=None,
        )
    )
    assert same is base
    # 运行态载荷里不许出现路由键（开关关闭 = 旧载荷逐字不变）
    for plan_id in ("plan-off-1", "plan-off-2"):
        assert "model_routing" not in fake.store[f"llm_plan:{plan_id}"]


def test_flag_off_wiring_is_gated_in_submission_service(router_off):
    """源码守卫：需求计算/结论写入/阻断短路都必须在开关分支里。"""
    source = (
        REPO_ROOT / "app" / "agents" / "orchestration" / "submission" / "service.py"
    ).read_text(encoding="utf-8")
    assert "MODEL_CAPABILITY_ROUTER_V2" in source
    gate = source.index("if model_router_enabled:")
    assert gate < source.index('plan_kwargs["requirements"]')
    assert gate < source.index('model_routing.get("blocked")')
    assert gate < source.index("if model_routing is not None:")


# ── 3. 能力规则 ──────────────────────────────────────────


def test_tools_missing_blocks_and_never_calls_a_model():
    calls: list[str] = []

    async def _resolver(role: str, **_kwargs):
        calls.append(role)
        raise AssertionError("候选已注入，不应再解析档位（更不应调用模型）")

    router = ModelCapabilityRouter(resolver=_resolver, catalog=lambda: [])
    plan = _tools_missing_plan()
    conclusion = asyncio.run(
        router.route(
            plan=plan,
            requirements=CapabilityRequirements(needs_tools=True),
            resolver=_resolver,
        )
    )
    payload = conclusion.to_routing()
    assert payload["blocked"] is True
    assert payload["action"] == "BLOCK"
    assert payload["reason_code"] == "MODEL_LACKS_CAPABILITY"
    assert payload["decision_reason_code"] == "MODEL_TOOLS_UNSUPPORTED"
    assert payload["error_code"] == "CAPABILITY_UNAVAILABLE", "阻断必须用冻结错误码"
    assert payload["missing_capabilities"] == ["tools"]
    assert payload["switch_allowed"] is False
    assert {item["reason_code"] for item in payload["excluded"]} == {"LACKS_TOOLS"}
    notice = conclusion.process_notice_payload()
    assert notice is not None and notice["status"] == "failed" and "工具" in notice["summary"]
    assert calls == []
    # 阻断结论不改写任何角色（绝不"换个模型硬跑"）
    assert router.refine_roles(plan, conclusion)["direct_answer"]["model"] == "main-no-tools"


def test_blocked_job_never_dispatches_to_a_model():
    """端到端：阻断路径必须物化终态 Job，且不进入计划选择（= 不调用模型）。"""
    from app.agents.orchestration.execution.job_materialization_service import JobMaterializationService
    from app.agents.orchestration.submission.service import JobSubmissionService
    from app.agents.orchestration.models import JobStatus

    dispatched: list[str] = []

    class _Store:
        async def create_job(self, job):
            dispatched.append("create_job")

    class _Selection:
        async def select(self, **_kwargs):
            dispatched.append("planner")
            raise AssertionError("阻断路径不得进入计划选择")

    class _Backend:
        async def submit(self, *_args, **_kwargs):
            dispatched.append("dispatch")
            raise AssertionError("阻断路径不得派发执行")

    async def _never(*_args, **_kwargs):
        dispatched.append("never")
        return None

    service = JobSubmissionService(
        store=_Store(),
        context_service=object(),
        office_plan_selection=_Selection(),
        plan_compilation=object(),
        materialization=JobMaterializationService(workers={}),
        temporal_static_mode=False,
        temporal_logical_read_mode=False,
        temporal_logical_effects_mode=False,
        can_run_static_temporal=lambda _job: False,
        probe_temporal=_never,
        static_backend=_Backend(),
        logical_read_backend=_Backend(),
        logical_effects_backend=_Backend(),
        legacy_backend=_Backend(),
        start_heartbeat=lambda *_args: None,
        stop_heartbeat=_never,
        plan_with_context=_never,
        plan_contexts={},
        llm_configs={},
    )
    conclusion = _route(_tools_missing_plan(), CapabilityRequirements(needs_tools=True))
    job = asyncio.run(
        service._blocked_by_model_capability(
            user_id="u1",
            user_role="user",
            request="把报告写入工作区文件",
            scene="office",
            conversation_id=None,
            submission_key="k1",
            admission_token="t1",
            routing={"llm": {"model": "main-no-tools"}},
            model_routing=conclusion.to_routing(),
        )
    )
    assert job.status == JobStatus.COMPLETED
    assert job.result["type"] == "clarification"
    assert job.nodes == []
    assert job.routing["model_routing"]["blocked"] is True
    assert job.routing["fallback_action"] == "model_capability_blocked"
    assert "planner" not in dispatched and "dispatch" not in dispatched


def test_vision_missing_switches_with_excluded_reasons():
    conclusion = _route(_plan(), CapabilityRequirements(needs_vision=True))
    payload = conclusion.to_routing()
    assert payload["reason_code"] == "VISION_REQUIRED"
    assert (payload["from_profile"], payload["to_profile"]) == ("main", "vision")
    assert payload["to_model"] == "vision-model"
    assert payload["switch_allowed"] is True
    assert payload["missing_capabilities"] == []
    reasons = {item["profile"]: item["reason_code"] for item in payload["excluded"]}
    assert reasons["main"] == "LACKS_VISION" and reasons["cheap"] == "LACKS_VISION"
    assert "视觉" in payload["safe_message"]


def test_vision_missing_without_candidate_blocks():
    plan = _plan(vision=_entry("vision", "", tools=False, vision=True))
    conclusion = _route(plan, CapabilityRequirements(needs_vision=True))
    payload = conclusion.to_routing()
    assert payload["blocked"] is True
    assert payload["missing_capabilities"] == ["vision"]
    assert payload["error_code"] == "CAPABILITY_UNAVAILABLE"
    reasons = [item["reason_code"] for item in payload["excluded"]]
    assert "LACKS_VISION" in reasons, "有候选但不支持视觉，必须给出原因"
    assert "UNHEALTHY" in reasons, "没配出模型的视觉档位按不可用记录"


def test_main_unavailable_degrades_to_available_profile():
    plan = _plan(main=_entry("main", ""))

    conclusion = _route(plan, CapabilityRequirements())
    payload = conclusion.to_routing()
    assert payload["reason_code"] == "PRIMARY_UNAVAILABLE"
    assert (payload["from_profile"], payload["to_profile"]) == ("main", "cheap")
    assert payload["degraded"] is True
    reasons = {(item["profile"], item["model"]): item["reason_code"] for item in payload["excluded"]}
    assert reasons[("main", "")] == "UNHEALTHY", "不可用的档位必须给出原因"
    assert "不可用" in payload["safe_message"]


def test_low_cost_preference_when_capability_allows():
    plan = _plan()
    conclusion = _route(plan, CapabilityRequirements())
    payload = conclusion.to_routing()
    assert payload["reason_code"] == "COST_OPTIMIZED"
    assert (payload["from_profile"], payload["to_profile"]) == ("main", "cheap")
    assert payload["switch_allowed"] is True
    notice = conclusion.process_notice_payload()
    assert notice is not None and notice["status"] == "completed"
    # 切换真正落到计划：无工具/审批依赖的角色跟着走，工具类角色保持原档位
    roles = _router().refine_roles(plan, conclusion)
    for role in ("direct_answer", "final_summary", "title", "summary"):
        assert roles[role]["profile"] == "cheap" and roles[role]["model"] == "cheap-model"
    for role in ("tool_write", "tool_read", "planner_complex", "code_reviewer"):
        assert roles[role]["profile"] == plan.roles[role]["profile"], f"{role} 不得被降级"


def test_no_decision_keeps_excluded_empty():
    """没做决策（``reason_code=""`` / 档位未变）→ ``excluded[]`` 必须为空数组。

    前端会照着 ``excluded[]`` 渲染"被排除的候选模型(n)"；如果每个 office 任务都带着
    "cheap 缺工具/vision 缺工具"这类**没导致任何变化**的排除项，界面就会在没有降级时
    显示降级说明。键必须保留（前端读它），内容只在真决策时才有。
    """
    conclusion = _route(_plan(), CapabilityRequirements(needs_tools=True))
    payload = conclusion.to_routing()
    assert payload["reason_code"] == ""
    assert (payload["from_profile"], payload["to_profile"]) == ("main", "main")
    assert payload["degraded"] is False
    assert payload["switch_allowed"] is True
    assert payload["excluded"] == [], "无决策时不得带排除明细"
    assert list(payload["excluded"]) == [] and "excluded" in payload, "键必须保留"
    assert payload["details"]["eligible_count"] >= 1, "候选筛选仍然发生（只是不进前端）"
    assert conclusion.process_notice_payload() is None, "没降级就不该发过程提示"
    # 影子/排障模式：同样的无决策结论仍保留诊断明细（不改执行路径）
    diagnostic = _route(_plan(), CapabilityRequirements(needs_tools=True), shadow=True)
    reasons = {item["profile"]: item["reason_code"] for item in diagnostic.excluded}
    assert reasons["cheap"] == "LACKS_TOOLS", "影子模式必须能解释为什么没切过去"
    assert reasons["vision"] == "LACKS_TOOLS"
    assert diagnostic.to_routing()["switch_allowed"] is True
    assert diagnostic.degraded is False


def test_real_decision_keeps_excluded_reasons():
    """真决策（换档/阻断/BYOK 钉住）→ ``excluded[]`` 必须带原因码（词表不变）。"""
    switched = _route(_plan(), CapabilityRequirements(needs_vision=True)).to_routing()
    assert switched["degraded"] is True and switched["excluded"]
    assert {item["reason_code"] for item in switched["excluded"]} == {"LACKS_VISION"}

    blocked = _route(_tools_missing_plan(), CapabilityRequirements(needs_tools=True)).to_routing()
    assert blocked["blocked"] is True and blocked["switch_allowed"] is False
    assert {item["reason_code"] for item in blocked["excluded"]} == {"LACKS_TOOLS"}

    pinned = _route(
        _plan(main=_entry("main", "user-own-model", byok=True, provider="custom"), byok=True),
        CapabilityRequirements(),
    ).to_routing()
    assert pinned["switch_allowed"] is False and pinned["degraded"] is False
    assert pinned["excluded"], "BYOK 钉住是决策，必须解释用户模型不参与默认切换"
    # 影子模式不得改变结论本身（除 excluded[] 外逐字相同）
    quiet = _route(_plan(), CapabilityRequirements(needs_tools=True))
    diagnostic = _route(_plan(), CapabilityRequirements(needs_tools=True), shadow=True)
    assert {k: v for k, v in diagnostic.to_routing().items() if k != "excluded"} == {
        k: v for k, v in quiet.to_routing().items() if k != "excluded"
    }



def test_context_and_streaming_requirements_filter_candidates():
    small = _plan(
        main=_entry("main", "main-small", max_context=8_000),
        cheap=_entry("cheap", "cheap-small", tools=False, max_context=8_000),
        reasoning=_entry("reasoning", "reason-small", max_context=8_000),
        vision=_entry("vision", "vision-small", tools=False, vision=True, max_context=8_000),
    )
    blocked = _route(small, CapabilityRequirements(min_context_tokens=64_000))
    payload = blocked.to_routing()
    assert payload["blocked"] is True and payload["missing_capabilities"] == ["long_context"]
    assert {item["reason_code"] for item in payload["excluded"]} == {"CONTEXT_TOO_SMALL"}

    non_streaming = _plan(main=_entry("main", "main-slow", streaming=False))
    strict = _route(non_streaming, CapabilityRequirements(needs_streaming=True, strict_streaming=True))
    assert strict.to_routing()["to_profile"] == "cheap"
    assert any(item["reason_code"] == "LACKS_STREAMING" for item in strict.excluded)
    soft = _route(non_streaming, CapabilityRequirements(needs_streaming=True))
    assert soft.action == "SIMULATE_STREAM" and soft.to_profile == "main"
    assert "流式" in soft.process_notice


# ── 4. BYOK 不参与默认切换 ───────────────────────────────


def test_byok_models_never_participate_in_default_switching():
    byok_entry = _entry("main", "user-own-model", byok=True, provider="custom")
    plan = _plan(main=byok_entry, byok=True)
    conclusion = _route(plan, CapabilityRequirements(needs_vision=True))
    payload = conclusion.to_routing()
    assert payload["byok"] is True
    assert payload["reason_code"] == "BYOK_PINNED"
    assert payload["switch_allowed"] is False
    assert (payload["from_profile"], payload["to_profile"]) == ("main", "main")
    assert payload["to_model"] == "user-own-model"
    assert {"model": "user-own-model", "profile": "main", "reason_code": "BYOK"} in payload["excluded"]
    assert conclusion.process_notice_payload() is None, "BYOK 不是降级，不发过程提示"
    roles = _router().refine_roles(plan, conclusion)
    assert roles == {role: dict(entry) for role, entry in plan.roles.items()}


def test_user_model_candidate_is_excluded_even_without_plan_byok_flag():
    """即使用户模型"看起来可用"，系统默认切换也不选它（只进 excluded[] 解释）。"""
    byok_candidate = ModelCandidate(
        profile="main",
        model="user-own-model",
        provider="custom",
        byok=True,
        available=True,
    )
    normal = ModelCandidate(
        profile="cheap",
        model="cheap-model",
        available=True,
        price_rank=0,
        capabilities={"supports_tools": False, "max_context": 32_000},
    )
    conclusion = asyncio.run(
        _router().route(
            plan=ModelPlan(plan_id="p", roles={"direct_answer": _entry("main", "")}),
            requirements=CapabilityRequirements(),
            candidates=[byok_candidate, normal],
        )
    )
    payload = conclusion.to_routing()
    assert payload["to_model"] == "cheap-model", "默认切换不得落到用户自备模型"
    assert {"model": "user-own-model", "profile": "main", "reason_code": "BYOK"} in payload["excluded"]


# ── 5. 注册表候选 + 冻结一致性 ───────────────────────────


def test_registry_candidates_are_reported_and_enrich_capabilities():
    catalog = [
        {"id": "main-model", "provider": "deepseek", "context_window": 200_000, "multimodal": True},
        {"id": "unconfigured-model", "provider": "qwen", "context_window": 32_000, "multimodal": False},
    ]
    router = ModelCapabilityRouter(catalog=lambda: catalog)
    plan = _plan()
    candidates = asyncio.run(router.build_candidates(plan=plan))
    by_model = {item.model: item for item in candidates}
    assert "unconfigured-model" in by_model and by_model["unconfigured-model"].available is False
    # 注册表补充能力事实：多模态 → 视觉；上下文取较大值
    main = by_model["main-model"]
    assert main.capability_profile().accepts("image")
    assert main.capability_profile().max_context_tokens == 200_000

    payload = asyncio.run(
        router.route(plan=plan, requirements=CapabilityRequirements())
    ).to_routing()
    registry = [item for item in payload["excluded"] if item["model"] == "unconfigured-model"]
    assert registry and registry[0]["reason_code"] == "UNHEALTHY"


def test_frozen_plan_and_routing_do_not_change_after_config_edit(monkeypatch, router_on):
    import app.platform.model.model_plan as mp

    _install_redis(monkeypatch)
    assert model_capability_router.enabled() is True
    plan = asyncio.run(
        mp.build_model_plan(scene="office", plan_id="plan-freeze", requirements=CapabilityRequirements())
    )
    assert plan.model_routing["reason_code"] == "COST_OPTIMIZED"
    routed_model = plan.roles["direct_answer"]["model"]
    assert plan.roles["direct_answer"]["profile"] == "cheap"

    # 任务执行期间管理员改配置：已冻结的计划与路由结论都不许变
    monkeypatch.setattr(settings, "LLM_CHEAP_MODEL", "changed-after-freeze", raising=False)
    monkeypatch.setattr(settings, "LLM_MAIN_MODEL", "changed-main-after-freeze", raising=False)
    mr.invalidate_role_cache()
    reloaded = asyncio.run(mp.load_model_plan("plan-freeze"))
    assert reloaded is not None
    assert reloaded.roles["direct_answer"]["model"] == routed_model
    assert reloaded.model_routing == plan.model_routing
    cfg = asyncio.run(mp.model_plan_llm_config(plan, "direct_answer"))
    assert cfg["model"] == routed_model, "执行链必须用冻结计划里的模型"


def test_process_log_projection_reuses_the_same_entry_id():
    from app.contracts.process_log import process_log_from_job

    class _Job:
        job_id = "job-1"
        created_at = 1_700_000_000.0
        plan_text = ""
        nodes: list[Any] = []
        routing = attach_model_routing({}, _route(_plan(), CapabilityRequirements(needs_vision=True)))

    entries = process_log_from_job(_Job())
    routing_entries = [item for item in entries if item.entry_id == "process:model_routing"]
    assert len(routing_entries) == 1
    assert routing_entries[0].kind.value == "thinking"
    assert routing_entries[0].summary
    assert str(routing_entries[0].status) == "completed"


# ── 6. 候选来源与职责边界 ────────────────────────────────


def test_candidates_come_from_env_profiles_and_the_model_registry():
    """候选 = ``.env``/管理员档位解析 + 注册表条目；解析路径 = model_roles。"""
    calls: list[str] = []

    async def _resolver(role: str, **_kwargs):
        calls.append(role)
        profile = {
            "direct_answer": "main",
            "title": "cheap",
            "tool_execute": "reasoning",
            "vision": "vision",
        }[role]
        return mr.ResolvedModel(
            role=role,
            profile=profile,
            provider="deepseek",
            model=f"{profile}-model",
            base_url="https://api.example/v1",
            api_key="sk-test",
            timeout=60.0,
            reasoning_effort=None,
            source="env",
            capabilities={"supports_tools": profile in {"main", "reasoning"}, "max_context": 128_000},
        )

    router = ModelCapabilityRouter(
        resolver=_resolver,
        catalog=lambda: [{"id": "registry-only", "provider": "qwen", "context_window": 8_000}],
    )
    candidates = asyncio.run(router.build_candidates(scene="office", user_id="u1"))
    origins = {item.model: item.origin for item in candidates}
    assert origins["main-model"] == "profile" and origins["cheap-model"] == "profile"
    assert origins["registry-only"] == "registry"
    assert set(calls) == {"direct_answer", "title", "tool_execute", "vision"}, "走既有角色解析"
    conclusion = asyncio.run(router.route(requirements=CapabilityRequirements()))
    payload = conclusion.to_routing()
    assert payload["from_profile"] == "main" and payload["to_profile"] == "cheap"


def test_router_never_asks_the_capability_broker_to_pick_models():
    """职责边界：Broker 只回答"谁提供能力"，选模型只走 model_roles/model_plan。"""
    source = (REPO_ROOT / "app" / "platform" / "model" / "model_capability_router.py").read_text(encoding="utf-8")
    assert "capability_broker" not in source
    assert "capabilities.broker" not in source
    assert "from app.platform.model import model_roles" in source
    assert "from app.platform.model.model_plan import ModelPlan" in source
