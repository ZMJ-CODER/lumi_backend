"""候选租约选择的纯函数测试（顺序即契约，顺序变了就是行为变了）。"""

from __future__ import annotations

from dataclasses import dataclass

from lumi_contracts.plugins import ProviderHealth

from lumi_capability import selection as s


@dataclass
class _Lease:
    capability: str
    provider_id: str = "lumi.local.workspace"
    health_status: ProviderHealth = ProviderHealth.HEALTHY
    last_heartbeat_at: float = 0.0
    expired: bool = False
    binding_ok: bool = True
    binding: object = "same"

    def matches(self, binding: object) -> bool:
        return self.binding_ok and binding == self.binding

    def is_expired(self) -> bool:
        return self.expired


def test_empty_candidates_report_binding_reason():
    lease, reason = s.select_lease([], capability="workspace.write", binding="same")
    assert lease is None
    assert "绑定" in reason


def test_binding_mismatch_is_not_selected():
    """跨用户/跨会话的租约永远不该被选中——不做"宽泛匹配"兜底。"""
    other = _Lease(capability="workspace.write", binding="other")
    lease, reason = s.select_lease([other], capability="workspace.write", binding="same")
    assert lease is None and "绑定" in reason


def test_capability_must_match_exactly():
    wrong = _Lease(capability="workspace.read")
    lease, reason = s.select_lease([wrong], capability="workspace.write", binding="same")
    assert lease is None and "绑定" in reason


def test_capability_suffix_revision_is_ignored():
    """``capability@2`` 与 ``capability`` 是同一个能力的两个版本声明。"""
    lease_obj = _Lease(capability="workspace.write")
    lease, reason = s.select_lease([lease_obj], capability="workspace.write@3", binding="same")
    assert lease is lease_obj and reason == "ok"


def test_most_recent_heartbeat_wins():
    old = _Lease(capability="workspace.write", provider_id="a", last_heartbeat_at=1.0)
    new = _Lease(capability="workspace.write", provider_id="b", last_heartbeat_at=2.0)
    lease, _ = s.select_lease([old, new], capability="workspace.write", binding="same")
    assert lease is new


def test_expired_lease_does_not_fall_back():
    """过期**不**回退：客户端契约里过期就是"必须重新注册"。"""
    fresh = _Lease(capability="workspace.write", expired=False, last_heartbeat_at=1.0)
    stale = _Lease(capability="workspace.write", expired=True, last_heartbeat_at=9.0)
    lease, reason = s.select_lease([fresh, stale], capability="workspace.write", binding="same")
    assert lease is fresh
    only_stale = _Lease(capability="workspace.write", expired=True)
    lease, reason = s.select_lease([only_stale], capability="workspace.write", binding="same")
    assert lease is None and "过期" in reason


def test_unknown_health_is_usable_but_unhealthy_is_not():
    unknown = _Lease(capability="workspace.write", health_status=ProviderHealth.UNKNOWN)
    lease, reason = s.select_lease([unknown], capability="workspace.write", binding="same")
    assert lease is unknown and reason == "ok"

    bad = _Lease(capability="workspace.write", health_status=ProviderHealth.UNHEALTHY)
    lease, reason = s.select_lease([bad], capability="workspace.write", binding="same")
    assert lease is None and "健康" in reason


def test_healthy_and_unknown_are_equally_usable():
    """``UNKNOWN``（刚注册还没体检）与 ``HEALTHY`` 同等可用，因此由心跳决定胜负。

    这是**原实现的行为**，不是疏忽：把 ``UNKNOWN`` 当成不可用，会让"客户端刚注册
    还没上报健康"的工具在第一分钟里不可用。
    """
    healthy = _Lease(capability="workspace.write", provider_id="h", health_status=ProviderHealth.HEALTHY, last_heartbeat_at=1.0)
    unknown = _Lease(capability="workspace.write", provider_id="u", health_status=ProviderHealth.UNKNOWN, last_heartbeat_at=9.0)
    lease, _ = s.select_lease([healthy, unknown], capability="workspace.write", binding="same")
    assert lease is unknown


def test_unhealthy_lease_never_wins_even_if_freshest():
    bad = _Lease(capability="workspace.write", provider_id="bad", health_status=ProviderHealth.UNHEALTHY, last_heartbeat_at=99.0)
    ok = _Lease(capability="workspace.write", provider_id="ok", health_status=ProviderHealth.HEALTHY, last_heartbeat_at=1.0)
    lease, _ = s.select_lease([bad, ok], capability="workspace.write", binding="same")
    assert lease is ok


def test_require_healthy_false_accepts_unhealthy():
    bad = _Lease(capability="workspace.write", health_status=ProviderHealth.UNHEALTHY)
    lease, reason = s.select_lease(
        [bad], capability="workspace.write", binding="same", require_healthy=False
    )
    assert lease is bad and reason == "ok"


# ── 资源类型收窄 ────────────────────────────────────────────


def test_provider_ids_narrow_the_candidates():
    code = _Lease(capability="code.execute", provider_id="lumi.local.code", last_heartbeat_at=1.0)
    workspace = _Lease(capability="code.execute", provider_id="lumi.local.workspace", last_heartbeat_at=9.0)
    lease, _ = s.select_lease([code, workspace], capability="code.execute", binding="same")
    assert lease is workspace, "不收窄时选心跳最新的"
    lease, _ = s.select_lease(
        [code, workspace], capability="code.execute", binding="same",
        provider_ids=frozenset({"lumi.local.code"}),
    )
    assert lease is code, "收窄后必须选声明允许的 Provider"


def test_narrowing_falls_back_when_it_would_starve():
    """声明不完整（插件没声明资源类型）不该表现成"工具不可用"。"""
    only = _Lease(capability="code.execute", provider_id="plugin.custom")
    warnings: list[str] = []
    lease, reason = s.select_lease(
        [only], capability="code.execute", binding="same",
        provider_ids=frozenset({"lumi.local.code"}), warn=warnings.append,
    )
    assert lease is only and reason == "ok"
    assert warnings and "收窄后没有候选" in warnings[0]


def test_empty_provider_ids_means_no_narrowing():
    lease_obj = _Lease(capability="code.execute", provider_id="whatever")
    lease, _ = s.select_lease(
        [lease_obj], capability="code.execute", binding="same", provider_ids=frozenset()
    )
    assert lease is lease_obj


def test_narrowing_prefers_declared_even_if_older():
    """收窄发生在心跳比较**之前**：声明匹配优先于新旧。"""
    declared = _Lease(capability="code.execute", provider_id="lumi.local.code", last_heartbeat_at=1.0)
    other = _Lease(capability="code.execute", provider_id="lumi.local.other", last_heartbeat_at=9.0)
    lease, _ = s.select_lease(
        [declared, other], capability="code.execute", binding="same",
        provider_ids=frozenset({"lumi.local.code"}),
    )
    assert lease is declared
