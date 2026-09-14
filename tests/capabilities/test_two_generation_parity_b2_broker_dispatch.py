"""两代能力对照 · 缺口 3 **批次 2**：Broker 选择 / Provider 收窄 / 派发的跨代守卫。

批次划分见 `docs/CAPABILITY_TWO_GENERATIONS.md` §3.5。批次 1 管"发现得到吗"，
本批管"选得对不对、派得出去吗"——它是四批里**风险最高**的一批，因为选错 Provider
的代价不是"看不到工具"，而是"把这一步交给不该执行的那一侧"。因此这里的断言
以**跨代差分**为主：同一条租约列表、同一个绑定、同一组收窄条件下，
两条路径（旧派发适配层 vs Broker）必须做出同一个选择。

五条性质与批次 1 同名，但落在派发面上：

1. **新旧一致**：能力 → 规范入口、MCP 名 → 能力，在 `TOOL_REGISTRY_DERIVED` 开关
   两种状态下逐条相同（开关只能改"谁提供答案"，不能改答案）；
2. **只增不减**：收窄集合、候选清单只可能缩小候选，绝不新增候选（选出的一定在收窄集合里，
   除非收窄后为空——那是性质 4 的回落）；
3. **不认识就不猜**：形状可疑的工具名与能力名在派发面上一律没有结论；
4. **收窄失败不倒过来卡死**：收窄后没有候选 → 按收窄前候选继续（两条路径都要如此）；
5. **新增工具不改变既有派发**：插件新装一个工具后，既有能力 → 规范入口的答案必须不变
   （这是"插件装完老工具路由错了"那类事故的守门人）。
"""

from __future__ import annotations

import pytest
from lumi_contracts.plugins import (
    Deployment,
    ProviderHealth,
    ProviderLease,
    SessionBinding,
    capability_ok,
)

from app.agents.capabilities.broker.broker import CapabilityBroker
from app.agents.capabilities.broker.dispatch import (
    CAPABILITY_TOOL_MAP,
    DispatchOutcome,
    capability_for_mcp_tool,
    mcp_tool_for_capability,
    select_lease as dispatch_select_lease,
)
from app.agents.capabilities.catalog.legacy import CAPABILITY_CODE_EXECUTE, capability_catalog
from app.agents.capabilities.catalog.resource import binding_for_tool
from app.agents.capabilities.contracts.context import AgentExecutionContext
from app.agents.capabilities.policy.routing import (
    MODE_ACTIVE,
    MODE_OFF,
    MODE_READ_ONLY,
    MODE_SHADOW,
    READ_ONLY_CAPABILITIES,
    maybe_route_capability,
    normalize_mode,
    should_route,
)
from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP, capability_for_tool

_JUNK_NAMES = (
    "totally_unknown_tool",
    "workspace_write_extra",
    "mcp__lumi_pc__totally_unknown_tool",
    "",
)

#: 这些是**能力名**：作为"工具名"查必须没有结论，但作为能力名（能力 → 工具方向）
#: 必须解析得到规范入口——两个方向不能混为一谈。
_CAPABILITY_NAMES = ("resource.write", "workspace.read", "resource.read", "not.a.capability")


# ── 夹具：真实租约对象 + 只提供快照的轻量租约服务 ─────────────


def _lease(provider_id: str, capability: str, heartbeat: float, **overrides) -> ProviderLease:
    from app.services.capability_lease_redis import lease_id_for

    payload = {
        "provider_id": provider_id,
        "capability": capability,
        "contract_version": 1,
        "lease_id": lease_id_for(provider_id, capability),
        "user_id": "u1",
        "device_id": "device-1",
        "workspace_id": "ws-1",
        "conversation_id": "c1",
        "deployment": Deployment.CLIENT,
        "plugin_id": provider_id,
        "provider_version": "1.0.0",
        "health_status": ProviderHealth.HEALTHY.value,
        "expires_at": 9_999_999_999.0,
        "last_heartbeat_at": heartbeat,
    }
    payload.update(overrides)
    return ProviderLease(**payload)


class _LeaseService:
    """适配层与 Broker 都只需要 ``snapshot()``；``sync_registry`` 是 Broker 的前置调用。"""

    def __init__(self, leases: list[ProviderLease]) -> None:
        self._leases = leases

    def snapshot(self, *, purge: bool = True) -> list[ProviderLease]:
        return list(self._leases)

    def sync_registry(self) -> None:  # pragma: no cover - 无副作用
        return None

    async def refresh_from_redis(self) -> list[ProviderLease]:
        return list(self._leases)


def _context() -> AgentExecutionContext:
    return AgentExecutionContext.from_metadata(
        user_id="u1", conversation_id="c1", workspace_id="ws-1", device_id="device-1"
    )


def _binding() -> SessionBinding:
    return SessionBinding(user_id="u1", conversation_id="c1", workspace_id="ws-1", device_id="device-1")


#: 两条路径要一起看的租约集：同能力多 Provider（工作区 vs git）+ 代码 Provider。
_LEASES = [
    _lease("lumi.local.workspace", "workspace.read", 100.0),
    _lease("lumi.local.git", "workspace.read", 200.0),
    _lease("lumi.local.workspace", "workspace.write", 150.0),
    _lease("lumi.local.code", "code.execute", 50.0),
]


@pytest.fixture()
def lease_service() -> _LeaseService:
    return _LeaseService(_LEASES)


# ── 性质 1：开关只改"谁提供答案"，不改答案 ────────────────────


@pytest.mark.parametrize("derived", [False, True])
def test_capability_to_mcp_target_is_identical_in_both_truth_sources(monkeypatch, derived):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", derived)
    for capability, expected in CAPABILITY_TOOL_MAP.items():
        assert mcp_tool_for_capability(capability) == expected, (capability, derived)
    # 未声明的能力：两边都不猜（只回落到调用方给的兜底名）
    assert mcp_tool_for_capability("not.a.capability", fallback="x") == "x"


@pytest.mark.parametrize("derived", [False, True])
def test_mcp_name_forms_resolve_to_the_same_capability(monkeypatch, derived):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", derived)
    for tool, capability in TOOL_CAPABILITY_MAP.items():
        forms = [tool, f"mcp__lumi_pc__{tool}"]
        if "." not in tool:
            # ``lumi.<tool>`` 这种命名空间写法按**末段**匹配，只对不含点的工具名成立
            # （``code.edit`` 这类自带点的名字用 ``mcp__…__`` 形式——那是模型看到的形状）。
            forms.append(f"lumi.{tool}")
        for form in forms:
            assert capability_for_mcp_tool(form) == capability, (form, derived)


# ── 性质 3：不认识就不猜（派发面）────────────────────────────


@pytest.mark.parametrize("name", _JUNK_NAMES)
def test_junk_names_are_never_dispatched(name):
    """真正的垃圾名：工具方向与能力方向都必须没有结论。"""
    assert capability_for_mcp_tool(name) is None, name
    assert capability_for_tool(name) in (None, "")
    assert binding_for_tool(name).known is False
    assert mcp_tool_for_capability(name, fallback="") == ""


@pytest.mark.parametrize("name", _CAPABILITY_NAMES)
def test_capability_names_are_not_tool_names(name):
    """能力名作为**工具名**查必须没有结论（折末段会撞上同名原子工具，那是"按名字猜"），
    但作为能力名该解析的仍要解析——两个方向分开断言，避免"一刀切"把能力入口关掉。"""
    assert capability_for_mcp_tool(name) is None, name
    assert capability_for_tool(name) is None or capability_for_tool(name) == ""
    assert binding_for_tool(name).known is False, name
    expected = CAPABILITY_TOOL_MAP.get(name)
    assert mcp_tool_for_capability(name, fallback="?") == (expected or "?")


# ── 性质 1 + 4：两条路径必须做出同一个选择 ────────────────────


@pytest.mark.parametrize(
    ("capability", "provider_ids"),
    [
        ("workspace.read", None),
        ("workspace.read", frozenset({"lumi.local.workspace"})),
        ("workspace.read", frozenset({"lumi.local.git"})),
        # 收窄集合里一个候选都没有 → 两条路径都必须按收窄前候选继续
        ("workspace.read", frozenset({"lumi.local.not-installed"})),
        # 该能力只有一条租约，且被收窄排除 → 同样回落
        ("code.execute", frozenset({"lumi.local.workspace"})),
        ("workspace.write", frozenset({"lumi.local.git"})),
        ("code.execute", None),
    ],
)
def test_broker_and_dispatch_agree_on_the_selected_provider(lease_service, capability, provider_ids):
    """跨代差分：同输入 ⇒ 同 Provider（这是"两代永不天然等价"的正面验收）。"""
    context = _context()
    broker = CapabilityBroker(leases=lease_service)

    lease, _reason = dispatch_select_lease(
        lease_service.snapshot(),
        capability=capability,
        context=context,
        provider_ids=provider_ids,
    )
    selection = broker.select(
        capability,
        binding=_binding(),
        provider_ids=provider_ids,
    )

    assert selection.provider_id == (lease.provider_id if lease else ""), (
        capability,
        provider_ids,
        selection.provider_id,
        getattr(lease, "provider_id", None),
    )


def test_narrowing_never_invents_a_candidate(lease_service):
    """性质 2：收窄只可能缩小候选——选中的 Provider 必须在收窄集合里（除非回落）。"""
    context = _context()
    wanted = frozenset({"lumi.local.git"})
    lease, _reason = dispatch_select_lease(
        lease_service.snapshot(), capability="workspace.read", context=context, provider_ids=wanted
    )
    assert lease is not None and lease.provider_id in wanted

    broker = CapabilityBroker(leases=lease_service)
    selection = broker.select("workspace.read", binding=_binding(), provider_ids=wanted)
    assert selection.provider_id in wanted


def test_empty_narrowing_keeps_the_capability_usable(lease_service):
    """性质 4：收窄集合为空（"只有声明"的资源）⇒ 完全不收窄，而不是"能力不可用"。"""
    from app.agents.capabilities.broker.resource_dispatch import provider_ids_for

    assert provider_ids_for("resource.write", "memory") == frozenset()
    broker = CapabilityBroker(leases=lease_service)
    selection = broker.select("workspace.read", binding=_binding(), resource_type="memory")
    assert selection.provider_id != ""
    assert selection.reason == "ok"


# ── 性质 4（在线路径）：适配层的收窄失败也要照常派发 ───────────


class _RecordingCaller:
    def __init__(self, payload: dict | None = None) -> None:
        self.calls: list[dict] = []
        self.payload = payload if payload is not None else {"status": "ok", "content": "done"}

    async def __call__(self, name, tool_name, args=None, **kwargs):
        self.calls.append({"name": name, "tool": tool_name, "args": dict(args or {})})
        return self.payload


@pytest.mark.asyncio
async def test_dispatch_survives_a_narrowing_that_excludes_every_lease(lease_service):
    from app.agents.capabilities.broker.dispatch import CapabilityDispatchAdapter

    caller = _RecordingCaller()
    adapter = CapabilityDispatchAdapter(lease_service=lease_service, call_tool=caller)
    context = _context()

    outcome = await adapter.dispatch(
        capability="workspace.read",
        args={"action": "read", "path": "a.py"},
        context=context,
        provider_ids=frozenset({"lumi.local.not-installed"}),
    )

    assert outcome.handled is True, outcome.reason
    assert outcome.provider_id == "lumi.local.git", "回落之后仍然按心跳选出真 Provider"
    assert outcome.result is not None and outcome.result.ok is True
    assert caller.calls, "收窄失败不能让调用被吞掉"


# ── 性质：off / shadow 绝不改变执行路径 ───────────────────────


class _AdapterStub:
    """替身适配层：记录调用并返回预设结论。"""

    def __init__(self, outcome: DispatchOutcome | None = None) -> None:
        self.calls: list[dict] = []
        self._outcome = outcome

    async def dispatch(self, **kwargs) -> DispatchOutcome:
        self.calls.append(kwargs)
        return self._outcome or DispatchOutcome(
            handled=True,
            capability=str(kwargs.get("capability") or ""),
            provider_id="lumi.local.workspace",
            reason="ok",
            result=capability_ok({"status": "ok"}, capability="workspace.read@1"),
        )


class _ExplodingAdapter:
    async def dispatch(self, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError("off 模式不得触碰适配层")


def test_should_route_matrix_is_closed():
    """模式 × 能力：off/shadow 永不真派发；read_only 只放只读能力；active 全放。"""
    capabilities = [item.name for item in capability_catalog.all()]
    assert capabilities, "能力目录不能为空（否则这条断言是空转）"
    for capability in capabilities:
        assert should_route(MODE_OFF, capability) is False, capability
        assert should_route(MODE_SHADOW, capability) is False, capability
        assert should_route(MODE_ACTIVE, capability) is True, capability
        assert should_route(MODE_READ_ONLY, capability) is (
            capability in READ_ONLY_CAPABILITIES
        ), capability
    # 历史别名必须收敛到同一套模式（配置里写哪个都能用）
    for alias in ("", "none", "disabled"):
        assert normalize_mode(alias) == MODE_OFF, alias
    for alias in ("readonly", "workspace_read_only"):
        assert normalize_mode(alias) == MODE_READ_ONLY, alias
    for alias in ("write", "code", "full"):
        assert normalize_mode(alias) == MODE_ACTIVE, alias
    assert normalize_mode("nonsense") == MODE_OFF


@pytest.mark.asyncio
async def test_off_mode_never_touches_the_adapter():
    decision = await maybe_route_capability(
        tool_name="workspace_navigator",
        args={"action": "read"},
        context=_context(),
        lease_service=_LeaseService(_LEASES),
        adapter=_ExplodingAdapter(),
        mode=MODE_OFF,
    )
    assert decision.handled is False
    assert decision.shadow is False


@pytest.mark.asyncio
async def test_shadow_mode_observes_but_never_handles():
    """shadow 的契约：查询、打点、**不接管**（handled=False 且没有结果）。"""
    adapter = _AdapterStub()
    decision = await maybe_route_capability(
        tool_name="workspace_navigator",
        args={"action": "read"},
        context=_context(),
        lease_service=_LeaseService(_LEASES),
        adapter=adapter,
        mode=MODE_SHADOW,
    )
    assert adapter.calls, "shadow 要真的查一遍才叫打点"
    assert decision.handled is False
    assert decision.result is None
    assert decision.shadow is True
    assert decision.provider_id == "lumi.local.workspace"
    assert decision.observation, "打点信息要带上（过程日志/审计据此回放）"


@pytest.mark.asyncio
async def test_unknown_tool_is_not_routed_even_in_active_mode():
    adapter = _AdapterStub()
    decision = await maybe_route_capability(
        tool_name="totally_unknown_tool",
        args={},
        context=_context(),
        lease_service=_LeaseService(_LEASES),
        adapter=adapter,
        mode=MODE_ACTIVE,
    )
    assert decision.handled is False
    assert adapter.calls == [], "不认识的工具连派发尝试都不该有"


# ── 性质 5：新增工具不改变既有派发 ───────────────────────────


def test_adding_a_tool_does_not_change_existing_dispatch_targets(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "TOOL_REGISTRY_DERIVED", True)
    before_targets = {cap: mcp_tool_for_capability(cap) for cap in CAPABILITY_TOOL_MAP}
    probe = "demo_dispatch_probe"
    before_capability = capability_for_mcp_tool("mcp__lumi_pc__workspace_write")

    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    class _Probe(Tool):
        description = "测试用：新增工具不能改变既有派发"
        category = "devtools"
        environment = "client"
        capability = CAPABILITY_CODE_EXECUTE
        resource_type = "workspace"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, params, context=None):  # noqa: ANN001
            return ToolOutput(success=True, output="ok", data=dict(params))

    _Probe.name = probe
    ToolRegistry.register(_Probe(), source="test")
    try:
        after_targets = {cap: mcp_tool_for_capability(cap) for cap in CAPABILITY_TOOL_MAP}
        assert after_targets == before_targets
        assert capability_for_mcp_tool("mcp__lumi_pc__workspace_write") == before_capability
    finally:
        ToolRegistry.unregister(probe)
        from app.agents.capabilities.catalog.tool_registry import invalidate_cache

        invalidate_cache()


def test_declared_capability_of_a_plugin_tool_is_still_gated_by_the_static_table():
    """性质 2 的反面：工具自述**不能**改写静态表里已有的归属（否则写工具能被说成只读）。

    直接问解析内核（不注册任何东西、不动全局注册表）：同一个名字，一个说自己是
    ``workspace.write``（静态表），一个自称 ``resource.read``（工具自述）——结论必须是
    静态表那个，且 ``declared`` 标志为假（表示"不是靠声明得来的"）。
    """
    from types import SimpleNamespace

    from app.agents.capabilities.catalog.tool_registry import _resolve_capability

    self_reported = SimpleNamespace(
        name="workspace_write", capability="resource.read", resource_type="memory"
    )
    capability, declared = _resolve_capability("workspace_write", self_reported)
    assert capability == "workspace.write"
    assert declared is False
    # 静态表不认识的工具，声明才生效（插件新工具走这条），并标记为"来自声明"。
    plugin_reported = SimpleNamespace(
        name="demo_plugin_writer", capability="resource.write", resource_type="memory"
    )
    capability, declared = _resolve_capability("demo_plugin_writer", plugin_reported)
    assert capability == "resource.write"
    assert declared is True
