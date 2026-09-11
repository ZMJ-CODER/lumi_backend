"""``Job.routing`` 路由快照的**唯一**投影器。

为什么要收敛：``task_profile`` / ``policy_version`` / ``execution_policy`` 以前由多个
模块各自写入（旧 v2 执行策略、Router v2、office 快速路径），最后写入者覆盖前面的
决策，于是同一个快照里出现两种语义并存：``policy_version`` 一会儿 ``v2`` 一会儿
``router_v2``，``task_profile`` 一会儿是旧 goal/source 画像、一会儿是严格 M0-M3 画像。

现在的固定规则：

1. **``router_v2`` 是唯一权威版本**；只有 Router v2 能提供严格 M0-M3 画像与
   路由模式。旧值 ``v2`` 只作为兼容信息出现在 ``routing.compat``。
2. **唯一权威 task_profile 是 Router v2 的严格画像**（M0-M3 词表）。旧
   goal/source 画像只能进 ``compat.legacy_task_profile``，不得占用顶层名字。
3. 顶层 ``policy_version`` / ``task_profile`` / ``route_mode`` /
   ``route_reason_code`` / ``safety_action`` 只是**迁移期只读镜像**，全部与
   ``route_decision`` 同源，由本模块一次性生成。
4. 两个开关互不覆盖：``TASK_ROUTER_V2_ENABLED`` 独立产出新决策；
   ``EXECUTION_POLICY_V2_ENABLED`` 只提供兼容字段；两者都关时策略字段全清。
5. 优先级固定：Router v2 > 旧执行策略兼容投影 > 旧路由逻辑，不允许"后写覆盖"。

调用约定（任何模块都不得再直接写上述字段）::

    snapshot = build_route_snapshot(...)
    apply_route_snapshot(job_routing, snapshot)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ROUTE_DECISION_SCHEMA_NAME = "lumi.route_decision"
ROUTE_DECISION_SCHEMA_VERSION = 1
ROUTER_V2_POLICY_VERSION = "router_v2"
LEGACY_EXECUTION_POLICY_VERSION = "v2"
COMPAT_KEY = "compat"
ROUTE_DECISION_KEY = "route_decision"

# 严格画像字段（Router v2 权威形状；前端与验收日志按此读取）。
STRICT_PROFILE_FIELDS: tuple[str, ...] = (
    "complexity",
    "confidence",
    "intent_type",
    "side_effects",
    "info_sources",
    "output_target",
    "execution_target",
    "required_capabilities",
    "path_determinism",
    "estimated_steps",
    "risk_level",
    "data_sensitivity",
    "context_size_estimate",
)

# 由本投影器独占的 routing 字段：写入前先清理，避免残留另一套语义。
MANAGED_KEYS: tuple[str, ...] = (
    ROUTE_DECISION_KEY,
    "policy_version",
    "task_profile",
    "route_mode",
    "route_reason_code",
    "safety_action",
    "execution_policy",
    "complexity",
    COMPAT_KEY,
)

# 严格画像 → 旧画像的派生表（旧画像只进 compat）。
# 严格画像的 info_sources 可能来自契约词表（PUBLIC_WEB）或评估词表
# （EXTERNAL_WEB），两种都要认，否则会被静默降级成 USER_INPUT。
_SOURCE_BY_INFO_SOURCE: dict[str, str] = {
    "USER_PROVIDED": "USER_INPUT",
    "CONVERSATION_MEMORY": "USER_INPUT",
    "INTERNAL_KNOWLEDGE": "LOCAL_KNOWLEDGE",
    "WORKSPACE": "WORKSPACE_READ",
    "ATTACHED_FILE": "ATTACHED_FILE",
    "PUBLIC_WEB": "PUBLIC_WEB",
    "EXTERNAL_WEB": "PUBLIC_WEB",
    "PRIVATE_SERVICE": "EXTERNAL_API",
    "SYSTEM_STATE": "SYSTEM_STATE",
}
_COMPLEXITY_BY_STRICT: dict[str, str] = {
    "M0": "ATOMIC",
    "M1": "ATOMIC",
    "M2": "SEQUENTIAL",
    "M3": "DYNAMIC",
}
_SAFETY_BY_RISK: dict[str, str] = {
    "READ_ONLY": "READ_ONLY",
    "REVERSIBLE": "SAFE_WRITE",
    "REQUIRES_APPROVAL": "RISKY_WRITE",
    "HIGH_RISK": "CRITICAL",
}


def legacy_task_profile_from_strict(strict: Mapping[str, Any] | None) -> dict[str, Any]:
    """严格 M0-M3 画像 → 旧 goal/source 画像（**只允许放进 compat**）。

    派生规则（不引入新的独立判断）：

    * ``needs_runtime_decision`` = ``path_determinism == "UNKNOWN"`` 或
      ``complexity == "M3"``；
    * ``goal`` 由 ``intent_type`` 与 ``side_effects`` 推导；
    * ``required_sources`` 对应 ``info_sources``；
    * ``has_side_effect`` 对应 ``bool(side_effects)``；
    * ``safety_level`` 由 ``risk_level`` 推导。
    """
    profile = dict(strict or {})
    complexity = str(profile.get("complexity") or "M1").upper()
    intent_type = str(profile.get("intent_type") or "GENERATE_ONLY")
    side_effects = [str(item) for item in (profile.get("side_effects") or [])]
    has_side_effect = bool(side_effects) or intent_type == "EXECUTE_ACTION"
    sources: list[str] = []
    for item in profile.get("info_sources") or []:
        mapped = _SOURCE_BY_INFO_SOURCE.get(str(item), "USER_INPUT")
        if mapped not in sources:
            sources.append(mapped)
    risk = str(profile.get("risk_level") or ("RISKY_WRITE" if has_side_effect else "READ_ONLY")).upper()
    path_determinism = str(profile.get("path_determinism") or "KNOWN").upper()
    return {
        "goal": "EXECUTE" if has_side_effect else "GENERATE",
        "required_sources": sources or ["USER_INPUT"],
        "complexity": _COMPLEXITY_BY_STRICT.get(complexity, "ATOMIC"),
        "safety_level": _SAFETY_BY_RISK.get(risk, "RISKY_WRITE" if has_side_effect else "READ_ONLY"),
        "has_side_effect": has_side_effect,
        "needs_runtime_decision": path_determinism == "UNKNOWN" or complexity == "M3",
        "confidence": float(profile.get("confidence") or 0.0),
    }


def build_route_snapshot(
    *,
    router_v2_enabled: bool,
    execution_policy_v2_enabled: bool,
    router_meta: Mapping[str, Any] | None = None,
    policy_meta: Mapping[str, Any] | None = None,
    existing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把各模块的决策对象合成**一份** routing 快照（唯一生成点）。

    :param router_meta: Router v2 决策（``RoutedTask.meta()``）；仅当 Router v2 开启。
    :param policy_meta: 旧执行策略决策（``policy_meta_from_signals(...)``）；仅当旧开关开启。
    :param existing: 当前 routing（用于 ``fallback_action`` 这类"已有值优先"的兼容字段）。
    """
    snapshot: dict[str, Any] = {}
    compat: dict[str, Any] = {}
    strict: dict[str, Any] = {}
    router_meta = dict(router_meta or {})
    policy_meta = dict(policy_meta or {})
    existing = dict(existing or {})

    if router_v2_enabled and router_meta:
        strict = dict(router_meta.get("task_profile") or {})
        route_mode = str(router_meta.get("route_mode") or "")
        policy_version = str(router_meta.get("policy_version") or ROUTER_V2_POLICY_VERSION)
        decision = {
            "schema_name": ROUTE_DECISION_SCHEMA_NAME,
            "schema_version": ROUTE_DECISION_SCHEMA_VERSION,
            "policy_version": policy_version,
            "task_profile": strict,
            "route_mode": route_mode,
            "route_reason_code": str(router_meta.get("route_reason_code") or ""),
            "safety_action": str(router_meta.get("safety_action") or ""),
            "assessor_source": str(router_meta.get("assessor_source") or ""),
        }
        snapshot[ROUTE_DECISION_KEY] = decision
        # 迁移期只读镜像：与 route_decision 同源，不允许各自写入。
        snapshot["policy_version"] = policy_version
        snapshot["task_profile"] = strict
        snapshot["route_mode"] = route_mode
        snapshot["route_reason_code"] = decision["route_reason_code"]
        snapshot["safety_action"] = decision["safety_action"]
        # 旧执行器/遥测读的 execution_policy 与权威 route_mode 同源；被拦截
        # （没有路由模式）时不写空串，避免"空策略名"被当成有效策略。
        if route_mode:
            snapshot["execution_policy"] = route_mode
        if strict.get("complexity"):
            snapshot["complexity"] = str(strict["complexity"])

    if execution_policy_v2_enabled and policy_meta:
        compat["execution_policy_version"] = LEGACY_EXECUTION_POLICY_VERSION
        compat["execution_policy_v2"] = {
            "policy_version": str(policy_meta.get("policy_version") or LEGACY_EXECUTION_POLICY_VERSION),
            "execution_policy": policy_meta.get("execution_policy"),
            "complexity": policy_meta.get("complexity"),
        }
        if snapshot.get(ROUTE_DECISION_KEY):
            # Router v2 权威：旧画像由严格画像派生，只进 compat。
            compat["legacy_task_profile"] = legacy_task_profile_from_strict(strict)
        else:
            # 兼容模式：只有旧画像可用，但仍然不占用顶层 task_profile。
            compat["legacy_task_profile"] = dict(policy_meta.get("task_profile") or {})
            if policy_meta.get("execution_policy"):
                snapshot["execution_policy"] = policy_meta.get("execution_policy")
            if policy_meta.get("complexity"):
                snapshot["complexity"] = str(policy_meta["complexity"])
        # 兼容字段：office 快速路径可能已经写了 fallback_action，已有值优先。
        if "fallback_action" in policy_meta and existing.get("fallback_action") is None:
            snapshot["fallback_action"] = policy_meta.get("fallback_action")

    if compat:
        snapshot[COMPAT_KEY] = compat
    return snapshot


def apply_route_snapshot(routing: dict[str, Any], snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """把快照写进 ``routing``（**唯一写入点**），并清掉不属于当前快照的策略字段。

    两个开关都关时 ``snapshot`` 为空 → 策略字段被清空，快照保持干净。
    """
    for key in MANAGED_KEYS:
        routing.pop(key, None)
    if snapshot:
        routing.update(dict(snapshot))
    return routing


def public_policy_fields(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """快照 → SSE / 验收日志的公开字段（Router v2 权威，旧信息只在 compat）。"""
    if not snapshot:
        return {}
    out: dict[str, Any] = {}
    decision = snapshot.get(ROUTE_DECISION_KEY)
    if isinstance(decision, Mapping):
        profile = dict(decision.get("task_profile") or {})
        out[ROUTE_DECISION_KEY] = dict(decision)
        # 迁移期：SSE 仍带一份权威画像镜像，前端可继续按旧键读取。
        out["task_profile"] = {key: profile.get(key) for key in STRICT_PROFILE_FIELDS if key in profile}
        out["policy_version"] = decision.get("policy_version")
        out["route_mode"] = decision.get("route_mode")
        out["execution_policy"] = snapshot.get("execution_policy")
    elif snapshot.get("execution_policy"):
        out["execution_policy"] = snapshot.get("execution_policy")
        out["complexity"] = snapshot.get("complexity")
    if snapshot.get(COMPAT_KEY):
        out[COMPAT_KEY] = snapshot[COMPAT_KEY]
    return out


__all__ = [
    "COMPAT_KEY",
    "LEGACY_EXECUTION_POLICY_VERSION",
    "MANAGED_KEYS",
    "ROUTE_DECISION_KEY",
    "ROUTE_DECISION_SCHEMA_NAME",
    "ROUTE_DECISION_SCHEMA_VERSION",
    "ROUTER_V2_POLICY_VERSION",
    "STRICT_PROFILE_FIELDS",
    "apply_route_snapshot",
    "build_route_snapshot",
    "legacy_task_profile_from_strict",
    "public_policy_fields",
]
