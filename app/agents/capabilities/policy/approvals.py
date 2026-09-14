"""阶段 3 后端：把既有审批服务的结论接到能力调用（令牌 → 门禁上下文）。

现状与缺口：``policy_guard.validate_approval`` 已经会校验"能力指纹 + 过期时间"，但
**没有地方把审批结论存下来、也没有地方把它喂给 Broker**——于是策略门禁永远看到
"未审批"。本模块补这条缝，并严格复用既有审批语义：

* 审批的**事实来源**仍是既有 ``Job.routing["confirmed_tool_calls"]`` 与
  ``node.metadata["approval_fingerprint"]``（`ApprovalService.resolve` 写入），
  这里**不新建第二套审批**；
* 能力调用需要的是**能力指纹**（能力名 + 参数 + scope），因此这里额外记录
  ``capability_approvals``：{能力限定名 → {fingerprint, expires_at, approved_at}}，
  由审批通过时写入；
* 读侧给出 :func:`capability_approval_context`，直接可作为
  ``broker.invoke(..., approval_context=...)`` 的参数。

**过期与指纹不匹配都在读侧再次校验**（写侧只记录）：审批服务可能过期、参数可能在
批准后变化，读侧的 :func:`validate_approval` 是最后一道闸。
"""

from __future__ import annotations

import time
from typing import Any

#: ``routing`` 里保存能力审批的位置（只放指纹与时间，不放参数）。
ROUTING_CAPABILITY_APPROVALS = "capability_approvals"

#: 一条能力审批的默认有效期（与 ``policy_guard`` 的令牌 TTL 一致）。
DEFAULT_CAPABILITY_APPROVAL_TTL_SECONDS = 900.0


def _routing(job: Any) -> dict[str, Any]:
    routing = getattr(job, "routing", None)
    return routing if isinstance(routing, dict) else {}


def record_capability_approval(
    job: Any,
    *,
    capability: str,
    fingerprint: str,
    expires_at: float = 0.0,
    now: float | None = None,
    approved_by: str = "",
) -> dict[str, Any]:
    """记录一次**已获批准**的能力调用（原地写 ``job.routing``，返回该条记录）。

    只写指纹/时间/批准人——不写参数（参数可能含敏感值，且指纹已足够校验）。
    """
    stamp = time.time() if now is None else float(now)
    expires = float(expires_at) if expires_at else stamp + DEFAULT_CAPABILITY_APPROVAL_TTL_SECONDS
    routing = dict(_routing(job))
    approvals = dict(routing.get(ROUTING_CAPABILITY_APPROVALS) or {})
    record = {
        "capability": str(capability or ""),
        "fingerprint": str(fingerprint or ""),
        "approved_at": stamp,
        "expires_at": expires,
        "approved_by": str(approved_by or ""),
    }
    approvals[str(capability or "")] = record
    routing[ROUTING_CAPABILITY_APPROVALS] = approvals
    job.routing = routing
    return record


def binding_for_tool_call(
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    upstream_sha256: str = "",
    capability: str = "",
    approved_tool_calls: Any = None,
) -> dict[str, Any] | None:
    """把**工具级审批**映射成能力审批上下文（精确指纹，不接受工具名白名单）。

    两道校验缺一不可：

    1. 本次调用的**工具指纹**必须出现在已批准集合里（``approved_tool_calls``）——
       只认与本次参数完全一致的指纹，``confirmed_tools`` 那种"整工具预授权"不算，
       否则 Broker 的按次指纹校验形同虚设；
    2. 返回的 ``fingerprint`` 是 **能力指纹**（``capability_fingerprint``），因为
       ``validate_approval`` 会独立重算能力指纹做比对——两处必须同函数同输入，
       否则会出现"批准了却一直说未审批"的死循环。
    """
    from app.agents.capabilities.policy.policy_guard import capability_fingerprint
    from app.agents.skills.executor import tool_call_fingerprint

    if not capability:
        return None
    tool_fingerprint = tool_call_fingerprint(
        str(tool_name or ""), dict(args or {}), str(upstream_sha256 or "")
    )
    approved = {str(item) for item in (approved_tool_calls or ()) if str(item or "").strip()}
    if tool_fingerprint not in approved:
        return None
    return {
        "capability": str(capability),
        "fingerprint": capability_fingerprint(str(capability), dict(args or {})),
        "approved_at": time.time(),
        "approved_by": "tool_call",
    }


def capability_approval_context(
    job: Any,
    capability: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """取该能力的审批上下文（**已过期返回 None**，让门禁按"未审批"处理）。

    返回的 dict 可直接传给 ``validate_approval(approval_context=...)`` / ``broker.invoke``。
    """
    record = (dict(_routing(job).get(ROUTING_CAPABILITY_APPROVALS) or {})).get(str(capability))
    if not isinstance(record, dict):
        return None
    stamp = time.time() if now is None else float(now)
    expires_at = float(record.get("expires_at") or 0.0)
    if expires_at and stamp >= expires_at:
        # 过期条目保留在快照里（审计"曾经批准过"），但不再作为授权依据。
        return None
    return {
        "capability": str(record.get("capability") or capability),
        "fingerprint": str(record.get("fingerprint") or ""),
        "expires_at": expires_at,
        "approved_at": float(record.get("approved_at") or 0.0),
        "approved_by": str(record.get("approved_by") or ""),
    }


def approval_snapshot(job: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    """审批快照（进 run_view 展示"哪些能力在本任务内已批准/已过期"）。"""
    stamp = time.time() if now is None else float(now)
    rows: list[dict[str, Any]] = []
    for capability, record in sorted(
        (dict(_routing(job).get(ROUTING_CAPABILITY_APPROVALS) or {})).items()
    ):
        if not isinstance(record, dict):
            continue
        expires_at = float(record.get("expires_at") or 0.0)
        rows.append(
            {
                "capability": capability,
                "expired": bool(expires_at and stamp >= expires_at),
                "expires_at": expires_at,
                "approved_at": float(record.get("approved_at") or 0.0),
            }
        )
    return rows


def carry_approval_to_step(
    job: Any,
    node: Any,
    *,
    capability: str,
    now: float | None = None,
) -> dict[str, Any] | None:
    """在**执行这一步之前**把审批结论搬到节点元数据（供 worker 发起能力调用时读取）。

    与既有 ``node.metadata["confirmed_tool_calls"]`` 并存：那一份是**工具级**审批
    （既有链路在用，不动）；这一份是**能力级**审批（Broker 门禁在用）。
    """
    context = capability_approval_context(job, capability, now=now)
    if context is None:
        return None
    metadata = dict(getattr(node, "metadata", None) or {})
    carried = dict(metadata.get("capability_approvals") or {})
    carried[str(capability)] = context
    metadata["capability_approvals"] = carried
    node.metadata = metadata
    return context


def node_approval_context(node: Any, capability: str) -> dict[str, Any] | None:
    """worker 侧读回审批上下文（``None`` = 未审批/已过期/未搬运）。"""
    carried = dict((getattr(node, "metadata", None) or {}).get("capability_approvals") or {})
    context = carried.get(str(capability))
    return dict(context) if isinstance(context, dict) else None


__all__ = [
    "DEFAULT_CAPABILITY_APPROVAL_TTL_SECONDS",
    "ROUTING_CAPABILITY_APPROVALS",
    "approval_snapshot",
    "capability_approval_context",
    "carry_approval_to_step",
    "node_approval_context",
    "record_capability_approval",
]
