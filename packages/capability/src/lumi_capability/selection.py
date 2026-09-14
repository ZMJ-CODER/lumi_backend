"""候选租约选择：在多个 Provider 里挑一个（**纯决策**）。

从 ``app/agents/capabilities/broker/dispatch.py`` 抽出（结构重构 P3 第一批）。
顺序是方案定死的，改动等于改派发行为：

1. **能力匹配**（能力名按 ``@N`` 基名比）+
2. **绑定匹配**（``lease.matches(binding)``：用户/会话/工作区/设备）→
3. **资源类型收窄**（统一资源能力层给的 ``provider_ids``）→
4. **未过期**（过期**不**回退：客户端契约里过期就是"必须重新注册"）→
5. **健康**（隔离中的 Provider 不选；全都不健康时明确失败，而不是硬选）→
6. **最近心跳**。

两个刻意的设计（原实现如此）：

* **收窄只在不会卡死时生效**：收窄后一个候选都不剩时保留收窄前候选——
  "插件没声明资源类型"不该表现成"工具不可用"；发生回退时通过 ``warn`` 回调交给
  调用方打点（本包不依赖日志库，所以不自己写日志）；
* **绑定不匹配直接失败**，不尝试"宽泛匹配"：跨用户/跨会话的租约永远不该被选中。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from lumi_contracts.plugins import ProviderHealth

#: 可用健康态：``UNKNOWN`` 视为可用（刚注册、还没体检 ≠ 隔离）。
_USABLE_HEALTH: frozenset[ProviderHealth] = frozenset({ProviderHealth.HEALTHY, ProviderHealth.UNKNOWN})

_MISSING_BINDING = "没有与该会话绑定匹配的租约"
_EXPIRED = "租约已过期（需要客户端重新注册能力）"
_UNHEALTHY = "Provider 健康检查未通过（隔离中）"
_OK = "ok"


def _base(capability: str) -> str:
    return str(capability or "").split("@", 1)[0]


def select_lease(
    leases: Sequence[Any],
    *,
    capability: str,
    binding: Any,
    require_healthy: bool = True,
    provider_ids: Iterable[str] | None = None,
    warn: Callable[[str], None] | None = None,
) -> tuple[Any | None, str]:
    """在候选租约里选一个（返回 ``(租约, 未命中原因)``；命中时原因是 ``"ok"``）。

    租约对象需要提供 ``capability`` / ``provider_id`` / ``matches(binding)`` /
    ``is_expired()`` / ``health_status`` / ``last_heartbeat_at``——这是契约层的
    ``ProviderLease`` 形状，本包不重新定义它。
    """
    base = _base(capability)
    bound = [
        lease
        for lease in leases
        if lease.capability == base and lease.matches(binding)
    ]
    wanted = frozenset(provider_ids) if provider_ids else frozenset()
    if wanted and bound:
        narrowed = [lease for lease in bound if str(lease.provider_id) in wanted]
        if narrowed:
            bound = narrowed
        elif warn is not None:
            warn(
                "[capability] 资源类型收窄后没有候选，按收窄前候选继续 "
                f"capability={base} providers={sorted(wanted)[:4]}"
            )
    if not bound:
        return None, _MISSING_BINDING
    alive = [lease for lease in bound if not lease.is_expired()]
    if not alive:
        # 过期租约**不**回退：客户端契约里过期就是"必须重新注册"。
        return None, _EXPIRED
    if require_healthy:
        healthy = [lease for lease in alive if _is_healthy(lease)]
        if healthy:
            alive = healthy
        else:
            return None, _UNHEALTHY
    head = max(alive, key=lambda lease: lease.last_heartbeat_at)
    return head, _OK


def _is_healthy(lease: Any) -> bool:
    """健康判定：``HEALTHY`` 与 ``UNKNOWN`` 都算可用（未知 ≠ 隔离）。"""
    return getattr(lease, "health_status", None) in _USABLE_HEALTH


__all__ = ["select_lease"]
