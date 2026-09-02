"""Temporal 静态 DAG 运行时的声明式准入策略。

该策略刻意比通用 DAG 编译器更严格。节点即使可在进程内运行时有效，仍要等其
暂停、恢复及副作用语义均已在 Activity 中验证后，才有资格进入 Temporal。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StaticTemporalDecision:
    eligible: bool
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReadOnlyAgentProfile:
    """Allowed parameter variants for a fully materialized read-only node."""

    modes: frozenset[str] | None = None
    allow_approved_effect: bool = False


# This is an allow-list, rather than a list of denied agents. A new plugin is
# Legacy-only by default and must be reviewed before it can enter Temporal.
READ_ONLY_AGENT_PROFILES: dict[str, ReadOnlyAgentProfile] = {
    "direct_llm": ReadOnlyAgentProfile(),
    "retrieval": ReadOnlyAgentProfile(),
    "document_targeting": ReadOnlyAgentProfile(),
    "web_research": ReadOnlyAgentProfile(),
    "office_text": ReadOnlyAgentProfile(),
    "office_research": ReadOnlyAgentProfile(),
    "office_doc": ReadOnlyAgentProfile(
        modes=frozenset({"read", "analyze", "edit"}), allow_approved_effect=True
    ),
    "office_todo": ReadOnlyAgentProfile(allow_approved_effect=True),
    "office_calendar": ReadOnlyAgentProfile(allow_approved_effect=True),
}

# 滚动逻辑计划不复用静态 DAG 的“已审批写操作”例外。该运行时会跨多个
# Activity/Workflow Run 推进，因此在服务级验收前只接受无审批、无副作用的
# 纯读节点。新增插件默认继续走 Legacy。
LOGICAL_READ_AGENT_PROFILES: dict[str, ReadOnlyAgentProfile] = {
    "direct_llm": ReadOnlyAgentProfile(),
    "retrieval": ReadOnlyAgentProfile(),
    "document_targeting": ReadOnlyAgentProfile(),
    "web_research": ReadOnlyAgentProfile(),
    "office_text": ReadOnlyAgentProfile(),
    "office_research": ReadOnlyAgentProfile(),
    "office_doc": ReadOnlyAgentProfile(modes=frozenset({"read", "analyze"})),
}

# 已审批逻辑计划使用独立运行时：它保留滚动前沿的 History 控制，但不允许
# 自动 LLM 重规划。只有已经审查过 effect-journal / 资源锁语义的 agent 可
# 进入，且写操作必须在计划阶段明确声明审批。
LOGICAL_EFFECT_AGENT_PROFILES: dict[str, ReadOnlyAgentProfile] = {
    **LOGICAL_READ_AGENT_PROFILES,
    "office_doc": ReadOnlyAgentProfile(
        modes=frozenset({"read", "analyze", "edit"}), allow_approved_effect=True
    ),
    "office_todo": ReadOnlyAgentProfile(allow_approved_effect=True),
    "office_calendar": ReadOnlyAgentProfile(allow_approved_effect=True),
}


def evaluate_static_temporal(
    job: Any,
    *,
    max_nodes: int,
    long_dag_enabled: bool = False,
    long_dag_max_nodes: int | None = None,
) -> StaticTemporalDecision:
    """Explain whether a frozen, no-side-effect DAG may enter Temporal."""
    routing = getattr(job, "routing", {}) or {}
    if routing.get("manifest") or routing.get("logical_plan"):
        return StaticTemporalDecision(False, "rolling_plan", "任务包含滚动清单或逻辑计划")
    return evaluate_static_temporal_nodes(
        list(getattr(job, "nodes", None) or []),
        max_nodes=max_nodes,
        long_dag_enabled=long_dag_enabled,
        long_dag_max_nodes=long_dag_max_nodes,
    )


def evaluate_static_temporal_nodes(
    nodes: list[Any],
    *,
    max_nodes: int,
    long_dag_enabled: bool = False,
    long_dag_max_nodes: int | None = None,
) -> StaticTemporalDecision:
    """Node-only preflight used before a Job id exists during compilation."""
    if not nodes:
        return StaticTemporalDecision(False, "empty_dag", "任务没有可执行节点")
    regular_limit = max(1, int(max_nodes))
    is_long_dag = len(nodes) > regular_limit
    long_limit = max(regular_limit, int(long_dag_max_nodes or regular_limit))
    if is_long_dag and not long_dag_enabled:
        return StaticTemporalDecision(False, "node_limit", f"节点数超过 Temporal 静态上限 {max_nodes}")
    if len(nodes) > (long_limit if is_long_dag else regular_limit):
        return StaticTemporalDecision(False, "node_limit", f"节点数超过 Temporal 静态上限 {long_limit}")
    for node in nodes:
        profile = READ_ONLY_AGENT_PROFILES.get(str(getattr(node, "agent", "")))
        if profile is None:
            return StaticTemporalDecision(False, "agent_not_allowlisted", f"节点 {node.id} 的 agent 未通过 Temporal 审核")
        approval = bool(getattr(node, "approval", False))
        metadata = getattr(node, "metadata", {}) or {}
        if metadata.get("awaiting_approval") or metadata.get("escalation"):
            return StaticTemporalDecision(False, "dynamic_control", f"节点 {node.id} 已进入动态控制流程")
        effectful = bool(getattr(node, "idempotency_key", None))
        claims = list(getattr(node, "resource_claims", None) or [])
        effectful = effectful or any(
            str(getattr(claim, "mode", "read")).lower() == "write" for claim in claims
        )
        params = getattr(node, "params", {}) or {}
        mode = str(params.get("mode") or "read").lower()
        agent_name = str(getattr(node, "agent", ""))
        if agent_name == "office_doc" and mode == "edit":
            effectful = True
        if agent_name in {"office_todo", "office_calendar"}:
            action = str(params.get("action") or "list").lower()
            effectful = effectful or action not in {"list", "read", "export"}
        # A declared approval gate is meaningful only for a concrete,
        # idempotent side effect.  Keep approval-only/read nodes on Legacy so
        # Temporal cannot silently reinterpret a future control flow.
        if approval and not effectful:
            return StaticTemporalDecision(
                False, "approval_without_effect", f"节点 {node.id} 声明审批但没有可绑定的副作用",
            )
        # 长 DAG 会跨多代 Workflow，并使用结果引用而非把完整节点正文留在
        # History 中。只让没有审批和任何副作用的节点进入该路径，避免把
        # 人工确认或 effect-journal 恢复语义拆到多个 Workflow Run。
        if is_long_dag and (approval or effectful):
            return StaticTemporalDecision(
                False,
                "long_dag_not_pure_read",
                f"长 Temporal DAG 仅允许纯读节点，节点 {node.id} 包含审批或副作用",
            )
        if agent_name == "office_doc" and mode == "edit" and not approval:
            return StaticTemporalDecision(
                False, "mode_not_allowlisted", f"节点 {node.id} 的 edit 模式需要审批",
            )
        if effectful and (not approval or not profile.allow_approved_effect):
            return StaticTemporalDecision(
                False, "effectful", f"节点 {node.id} 的副作用未通过 Temporal 审批策略"
            )
        if profile.modes is not None:
            if mode not in profile.modes:
                return StaticTemporalDecision(False, "mode_not_allowlisted", f"节点 {node.id} 的 mode={mode} 不允许")
    detail = "静态、完全物化、无副作用 DAG"
    if is_long_dag:
        detail = "静态、完全物化、纯读长 DAG"
    return StaticTemporalDecision(True, "eligible", detail)


def evaluate_logical_read_temporal(job: Any, plan: dict[str, Any] | None) -> StaticTemporalDecision:
    """校验 Redis 中完整逻辑计划是否可由 Temporal 仅推进纯读前沿。

    ``Job.nodes`` 只是当前前沿，不能据此放行；必须检查完整计划中的每个
    节点。逻辑计划重规划会改写未完成尾部，因此已存在重规划历史也拒绝进入
    此运行时，避免 Workflow 与 Planner 同时拥有计划定义权。
    """
    routing = getattr(job, "routing", {}) or {}
    pointer = routing.get("logical_plan")
    if not isinstance(pointer, dict) or not pointer.get("plan_id"):
        return StaticTemporalDecision(False, "not_logical_plan", "任务不是滚动逻辑计划")
    if not isinstance(plan, dict) or str(plan.get("plan_id") or "") != str(pointer.get("plan_id") or ""):
        return StaticTemporalDecision(False, "logical_plan_unavailable", "完整逻辑计划不可用或不匹配")
    expected = str(plan.get("execution_fingerprint") or "")
    if not expected:
        return StaticTemporalDecision(False, "logical_plan_unsealed", "逻辑计划缺少不可变执行指纹")
    try:
        from app.agents.orchestration.logical_plan import logical_plan_execution_fingerprint

        if logical_plan_execution_fingerprint(plan) != expected:
            return StaticTemporalDecision(False, "logical_plan_fingerprint", "逻辑计划执行定义校验失败")
    except Exception:
        return StaticTemporalDecision(False, "logical_plan_fingerprint", "逻辑计划执行定义无法校验")
    history = list(plan.get("history") or [])
    if history and any(
        not isinstance(item, dict)
        or (
            item.get("runtime") != "temporal_logical_read"
            and item.get("event") != "plan_patch_applied"
        )
        for item in history
    ):
        return StaticTemporalDecision(False, "logical_plan_replanned", "逻辑计划包含未审核的动态重规划历史")

    records = plan.get("nodes") or {}
    order = list(plan.get("order") or [])
    if not order:
        return StaticTemporalDecision(False, "empty_dag", "逻辑计划没有可执行节点")
    for node_id in order:
        record = records.get(node_id)
        if not isinstance(record, dict):
            return StaticTemporalDecision(False, "logical_plan_invalid", f"逻辑节点 {node_id} 缺少记录")
        raw_node = record.get("node") or {}
        agent_name = str(raw_node.get("agent") or "")
        profile = LOGICAL_READ_AGENT_PROFILES.get(agent_name)
        if profile is None:
            return StaticTemporalDecision(False, "agent_not_allowlisted", f"节点 {node_id} 的 agent 未通过 Temporal 纯读审核")
        metadata = raw_node.get("metadata") or {}
        if bool(raw_node.get("approval")) or metadata.get("awaiting_approval") or metadata.get("escalation"):
            return StaticTemporalDecision(False, "dynamic_control", f"节点 {node_id} 包含审批或动态控制")
        if raw_node.get("idempotency_key") or str(record.get("effect_status") or ""):
            return StaticTemporalDecision(False, "effectful", f"节点 {node_id} 包含副作用安全状态")
        claims = raw_node.get("resource_claims") or []
        if any(str((claim or {}).get("mode") or "read").lower() == "write" for claim in claims if isinstance(claim, dict)):
            return StaticTemporalDecision(False, "effectful", f"节点 {node_id} 声明写资源")
        params = raw_node.get("params") or {}
        mode = str(params.get("mode") or "read").lower()
        if profile.modes is not None and mode not in profile.modes:
            return StaticTemporalDecision(False, "mode_not_allowlisted", f"节点 {node_id} 的 mode={mode} 不允许")
        if agent_name in {"office_todo", "office_calendar", "react_step"}:
            return StaticTemporalDecision(False, "agent_not_allowlisted", f"节点 {node_id} 不属于纯读逻辑计划范围")
    return StaticTemporalDecision(True, "eligible", "完整逻辑计划已冻结，全部节点为纯读")


def evaluate_logical_effect_temporal(job: Any, plan: dict[str, Any] | None) -> StaticTemporalDecision:
    """校验带预声明审批的滚动逻辑计划能否进入 Temporal 写路径。

    与纯读路径不同，该运行时允许已审核的 effect-journal 节点，但禁止
    ReAct 和自动重规划。完整计划必须先校验，不能凭当前只读前沿放行。
    """
    routing = getattr(job, "routing", {}) or {}
    pointer = routing.get("logical_plan")
    if not isinstance(pointer, dict) or not pointer.get("plan_id"):
        return StaticTemporalDecision(False, "not_logical_plan", "任务不是滚动逻辑计划")
    if not isinstance(plan, dict) or str(plan.get("plan_id") or "") != str(pointer.get("plan_id") or ""):
        return StaticTemporalDecision(False, "logical_plan_unavailable", "完整逻辑计划不可用或不匹配")
    try:
        from app.agents.orchestration.logical_plan import logical_plan_execution_fingerprint

        if str(plan.get("execution_fingerprint") or "") != logical_plan_execution_fingerprint(plan):
            return StaticTemporalDecision(False, "logical_plan_fingerprint", "逻辑计划执行定义校验失败")
    except Exception:
        return StaticTemporalDecision(False, "logical_plan_fingerprint", "逻辑计划执行定义无法校验")
    history = list(plan.get("history") or [])
    if history and any(
        not isinstance(item, dict)
        or (
            item.get("runtime") != "temporal_logical_effects"
            and item.get("event") != "plan_patch_applied"
        )
        for item in history
    ):
        return StaticTemporalDecision(False, "logical_plan_replanned", "逻辑计划包含未审核的计划替换历史")
    records = plan.get("nodes") or {}
    order = list(plan.get("order") or [])
    if not order:
        return StaticTemporalDecision(False, "empty_dag", "逻辑计划没有可执行节点")
    declared_effect_count = 0
    for node_id in order:
        record = records.get(node_id)
        if not isinstance(record, dict):
            return StaticTemporalDecision(False, "logical_plan_invalid", f"逻辑节点 {node_id} 缺少记录")
        raw_node = record.get("node") or {}
        agent_name = str(raw_node.get("agent") or "")
        profile = LOGICAL_EFFECT_AGENT_PROFILES.get(agent_name)
        if profile is None:
            return StaticTemporalDecision(False, "agent_not_allowlisted", f"节点 {node_id} 的 agent 未通过 Temporal 审核")
        metadata = raw_node.get("metadata") or {}
        if metadata.get("escalation"):
            return StaticTemporalDecision(False, "dynamic_control", f"节点 {node_id} 已包含动态升级状态")
        claims = raw_node.get("resource_claims") or []
        effectful = bool(raw_node.get("idempotency_key")) or any(
            str((claim or {}).get("mode") or "read").lower() == "write"
            for claim in claims
            if isinstance(claim, dict)
        )
        params = raw_node.get("params") or {}
        mode = str(params.get("mode") or "read").lower()
        if agent_name == "office_doc" and mode == "edit":
            effectful = True
        if agent_name in {"office_todo", "office_calendar"}:
            action = str(params.get("action") or "list").lower()
            effectful = effectful or action not in {"list", "read", "export"}
        if profile.modes is not None and mode not in profile.modes:
            return StaticTemporalDecision(False, "mode_not_allowlisted", f"节点 {node_id} 的 mode={mode} 不允许")
        if effectful:
            declared_effect_count += 1
            if not bool(raw_node.get("approval")):
                return StaticTemporalDecision(False, "effect_without_approval", f"节点 {node_id} 的副作用未声明审批")
            if not raw_node.get("idempotency_key"):
                return StaticTemporalDecision(False, "effect_without_idempotency", f"节点 {node_id} 缺少幂等键")
            if not profile.allow_approved_effect:
                return StaticTemporalDecision(False, "effectful", f"节点 {node_id} 的副作用未通过 Temporal 审核")
        elif bool(raw_node.get("approval")):
            return StaticTemporalDecision(False, "approval_without_effect", f"节点 {node_id} 审批未绑定副作用")
    # This backend is deliberately not a more permissive alias of the
    # pure-read runtime.  Without an approved effect, use
    # ``temporal_logical_read`` so the two rollout switches remain independent.
    if declared_effect_count == 0:
        return StaticTemporalDecision(
            False,
            "no_declared_effect",
            "逻辑计划不包含已声明审批的副作用节点，应使用纯读 Temporal 路径",
        )
    return StaticTemporalDecision(True, "eligible", "完整逻辑计划已冻结，副作用均为预声明审批节点")
