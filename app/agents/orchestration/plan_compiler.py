"""Deterministic compilation of an LLM-produced execution plan.

The planner may propose a graph, but it is not an execution authority.  This
module is the boundary where a plan is checked against the workers and the
currently authorized Skill/MCP namespace before it reaches the DAG runner.
Compilation is deliberately conservative: optional fallbacks may be removed
when unavailable, but required work is never silently deleted or merged.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.task_routing import RouteChannel, estimate_tokens


class CompileDecision(str, Enum):
    ACCEPTED = "accepted"
    NORMALIZED = "normalized"
    REPLAN_REQUIRED = "replan_required"
    CLARIFICATION_REQUIRED = "clarification_required"


class PlanViolation(BaseModel):
    code: str
    message: str
    node_id: str = ""
    severity: str = "error"


class PlanCost(BaseModel):
    estimated_tokens: int = Field(default=0, ge=0)
    critical_path_ms: int = Field(default=0, ge=0)
    peak_gpu_mb: int = Field(default=0, ge=0)
    node_count: int = Field(default=0, ge=0)


class CapabilitySnapshot(BaseModel):
    """The authorized runtime namespace used to compile one plan."""

    scene: str
    user_role: str
    workers: list[str] = Field(default_factory=list)
    tools: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fingerprint: str = ""


class CompiledPlan(BaseModel):
    decision: CompileDecision
    nodes: list[TaskNode] = Field(default_factory=list)
    violations: list[PlanViolation] = Field(default_factory=list)
    warnings: list[PlanViolation] = Field(default_factory=list)
    cost: PlanCost = Field(default_factory=PlanCost)
    capabilities: CapabilitySnapshot

    @property
    def ok(self) -> bool:
        return self.decision in {CompileDecision.ACCEPTED, CompileDecision.NORMALIZED}


_NODE_REQUIRED: dict[str, tuple[str, ...]] = {
    "direct_llm": ("instruction",),
    "collect_results": ("items",),
    "atomic_step": ("instruction", "preferred_tool"),
    "react_step": ("instruction",),
    "office_doc": ("doc_id", "instruction", "mode"),
    "office_text": ("instruction",),
    "office_research": ("instruction", "mode"),
    "office_todo": ("action",),
    "retrieval": ("query",),
    "web_research": ("instruction",),
    "code": ("project_id", "instruction"),
    "code_reader": ("project_id", "instruction"),
    "code_writer": ("project_id", "instruction"),
}

_NODE_COST = {
    "direct_llm": (1800, 8_000),
    "atomic_step": (2_500, 30_000),
    "react_step": (12_000, 120_000),
    "office_script": (4_000, 60_000),
    "office_document": (5_000, 60_000),
    "retrieval": (1_200, 5_000),
    "web_research": (3_500, 20_000),
    "office_doc": (2_500, 20_000),
    "office_text": (2_500, 20_000),
    "office_research": (4_000, 30_000),
    "office_todo": (2_000, 15_000),
    "code": (8_000, 90_000),
    "code_reader": (3_000, 20_000),
    "code_writer": (8_000, 90_000),
    "code_tester": (3_000, 60_000),
    "code_reviewer": (4_000, 30_000),
    "collect_results": (1_000, 5_000),
}


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def _validate_explicit_inputs(
    node: TaskNode,
    schema: dict[str, Any],
    violations: list[PlanViolation],
) -> None:
    """Validate fields the planner explicitly supplied.

    Required tool arguments may still be extracted from the natural-language
    instruction by AtomicStepAgent, so this intentionally does not require all
    schema fields at compile time.  It does reject malformed explicit values.
    """
    inputs = (node.params or {}).get("inputs")
    if inputs is None:
        return
    if not isinstance(inputs, dict):
        violations.append(PlanViolation(
            code="PARAMS_TYPE", message="inputs 必须是对象", node_id=node.id,
        ))
        return
    properties = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(properties, dict):
        return
    for name, value in inputs.items():
        definition = properties.get(name)
        if not isinstance(definition, dict):
            continue
        expected = definition.get("type")
        if isinstance(expected, str) and not _json_type_matches(value, expected):
            violations.append(PlanViolation(
                code="PARAM_TYPE",
                message=f"参数 {name} 类型不符合工具 schema（需要 {expected}）",
                node_id=node.id,
            ))


def _validate_plan_step_contract(
    node: TaskNode,
    node_ids: set[str],
    violations: list[PlanViolation],
) -> None:
    """Validate the optional typed route DSL attached by deterministic planners."""
    raw = (node.metadata or {}).get("plan_step")
    if raw is None:
        return
    if not isinstance(raw, dict):
        violations.append(PlanViolation(
            code="PLAN_STEP_SCHEMA", message="plan_step 必须是对象", node_id=node.id,
        ))
        return
    try:
        from app.agents.orchestration.route_plan import PlanStep

        step = PlanStep.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        violations.append(PlanViolation(
            code="PLAN_STEP_SCHEMA", message=f"plan_step schema 无效: {exc}", node_id=node.id,
        ))
        return
    if step.id != node.id:
        violations.append(PlanViolation(
            code="PLAN_STEP_ID", message="plan_step.id 必须与节点 id 一致", node_id=node.id,
        ))
    dependency_ids = set(node.depends_on)
    for ref in step.input_contract:
        if ref.source_step not in node_ids:
            violations.append(PlanViolation(
                code="PLAN_STEP_REF", message=f"输入引用 {ref.source_step} 不存在", node_id=node.id,
            ))
        elif ref.source_step not in dependency_ids:
            violations.append(PlanViolation(
                code="PLAN_STEP_DEPENDENCY", message=f"输入引用 {ref.source_step} 未声明为 depends_on", node_id=node.id,
            ))


def _critical_path(nodes: list[TaskNode]) -> int:
    durations: dict[str, int] = {}
    by_id = {node.id: node for node in nodes}
    for node in nodes:
        own = _NODE_COST.get(node.agent, (3_000, 30_000))[1]
        durations[node.id] = own + max((durations.get(dep, 0) for dep in node.depends_on), default=0)
    # Invalid dependencies are reported separately; this value remains useful
    # for logging even when a plan is rejected.
    return max(durations.values(), default=0)


async def build_capability_snapshot(
    *,
    scene: str,
    user_role: str,
    user_id: str,
    workers: dict[str, Any],
) -> CapabilitySnapshot:
    """Build a per-job capability snapshot after scene/role/runtime filters."""
    tools: dict[str, dict[str, Any]] = {}
    try:
        from app.agents.skills.executor import get_capabilities_for_scene

        capabilities = await get_capabilities_for_scene(scene, user_role, user_id)
        for capability in capabilities:
            tools[capability.name] = {
                "parameters": capability.parameters if isinstance(capability.parameters, dict) else {},
                "permission": capability.permission,
                "write_op": bool(capability.write_op),
                "requires_confirmation": bool(capability.requires_confirmation),
                "idempotent": bool(capability.idempotent),
                "status": capability.status,
                "description": capability.description,
            }
    except Exception:
        # Local workers can still be compiled if an optional MCP discovery
        # backend is unavailable.  The executor performs the same final check.
        tools = {}
    encoded = json.dumps(
        {"scene": scene, "role": user_role, "workers": sorted(workers), "tools": tools},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return CapabilitySnapshot(
        scene=scene,
        user_role=user_role,
        workers=sorted(workers),
        tools=tools,
        fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16],
    )


async def compile_plan(
    nodes: list[TaskNode],
    *,
    scene: str,
    user_role: str,
    user_id: str,
    workers: dict[str, Any],
    max_nodes: int | None = None,
) -> CompiledPlan:
    """Validate and cost a proposed plan without executing any side effect."""
    from app.agents.orchestration.dag import DagValidationError, validate_dag
    from app.core.config import settings

    max_nodes = max_nodes or int(getattr(settings, "AGENT_PLAN_MAX_NODES", 8))

    snapshot = await build_capability_snapshot(
        scene=scene, user_role=user_role, user_id=user_id, workers=workers,
    )
    compiled_nodes = [node.model_copy(deep=True) for node in nodes]
    violations: list[PlanViolation] = []
    warnings: list[PlanViolation] = []

    if not compiled_nodes:
        violations.append(PlanViolation(code="EMPTY_PLAN", message="规划没有生成可执行节点"))
    if len(compiled_nodes) > max_nodes:
        violations.append(PlanViolation(
            code="NODE_LIMIT", message=f"计划包含 {len(compiled_nodes)} 个节点，超过当前窗口上限 {max_nodes}；应改为逻辑计划或任务清单",
        ))
    try:
        validate_dag(compiled_nodes)
    except DagValidationError as exc:
        violations.append(PlanViolation(code="DAG_INVALID", message=str(exc)))

    node_ids = {node.id for node in compiled_nodes}
    for node in compiled_nodes:
        _validate_plan_step_contract(node, node_ids, violations)
        required = _NODE_REQUIRED.get(node.agent, ())
        params = node.params or {}
        for field in required:
            if field not in params or params[field] in (None, "", []):
                violations.append(PlanViolation(
                    code="PARAM_REQUIRED", message=f"缺少必需参数 {field}", node_id=node.id,
                ))
        if node.agent not in workers:
            violations.append(PlanViolation(
                code="WORKER_UNAVAILABLE", message=f"执行节点 {node.agent} 当前未注册", node_id=node.id,
            ))
        if node.agent == "atomic_step":
            params["instruction"] = str(params.get("instruction") or "").strip()
            params["preferred_tool"] = str(params.get("preferred_tool") or "").strip()
            fallback = params.get("fallback_tools") or []
            if not isinstance(fallback, list) or any(not isinstance(item, str) for item in fallback):
                violations.append(PlanViolation(
                    code="FALLBACK_SCHEMA", message="fallback_tools 必须是字符串数组", node_id=node.id,
                ))
                fallback = []
            params["fallback_tools"] = list(dict.fromkeys(item.strip() for item in fallback if item.strip()))[:2]
            if params.get("preferred_tool") in params["fallback_tools"]:
                violations.append(PlanViolation(
                    code="DUPLICATE_TOOL", message="preferred_tool 不能同时出现在 fallback_tools", node_id=node.id,
                ))
            candidates = [params.get("preferred_tool"), *params["fallback_tools"]]
            for index, tool_name in enumerate(candidates):
                if not tool_name:
                    continue
                tool = snapshot.tools.get(tool_name)
                if tool is None:
                    violations.append(PlanViolation(
                        code="TOOL_UNAVAILABLE" if index == 0 else "FALLBACK_UNAVAILABLE",
                        message=f"规划工具 {tool_name} 当前不可用或未授权",
                        node_id=node.id,
                        severity="error" if index == 0 else "warning",
                    ))
                    if index > 0:
                        params["fallback_tools"].remove(tool_name)
                    continue
                _validate_explicit_inputs(node, tool.get("parameters") or {}, violations)
            if params.get("preferred_tool"):
                node.metadata = {
                    **(node.metadata or {}),
                    "compiled_tool": params["preferred_tool"],
                    "compiled_tool_write": bool((snapshot.tools.get(params["preferred_tool"]) or {}).get("write_op")),
                }
        if node.agent == "react_step":
            rounds = params.get("max_rounds", 6)
            if not isinstance(rounds, int) or not 1 <= rounds <= 6:
                violations.append(PlanViolation(
                    code="REACT_ROUNDS", message="react_step.max_rounds 必须在 1 到 6 之间", node_id=node.id,
                ))

    # Optional fallback failures are warnings after normalization, not a reason
    # to reject an otherwise executable plan.
    warnings.extend(item for item in violations if item.severity == "warning")
    violations = [item for item in violations if item.severity != "warning"]
    total_tokens = sum(
        estimate_tokens(
            str((node.params or {}).get("instruction") or (node.params or {}).get("task") or node.name),
            RouteChannel.AGENT if node.agent not in {"retrieval", "direct_llm"} else (
                RouteChannel.RAG if node.agent == "retrieval" else RouteChannel.DIRECT_LLM
            ),
        )
        for node in compiled_nodes
    )
    cost = PlanCost(
        estimated_tokens=total_tokens,
        critical_path_ms=_critical_path(compiled_nodes),
        peak_gpu_mb=max(
            (int((snapshot.tools.get(str((node.params or {}).get("preferred_tool"))) or {}).get("gpu_mb") or 0)
             for node in compiled_nodes),
            default=0,
        ),
        node_count=len(compiled_nodes),
    )
    token_budget = int(getattr(settings, "AGENT_LOGICAL_PLAN_TOKEN_BUDGET", 80_000))
    if cost.estimated_tokens > 0 and cost.estimated_tokens > token_budget:
        violations.append(PlanViolation(
            code="TOKEN_BUDGET", message=f"计划预估 token {cost.estimated_tokens} 超过 {token_budget}",
        ))
    decision = CompileDecision.ACCEPTED
    if violations:
        decision = CompileDecision.REPLAN_REQUIRED
    elif warnings:
        decision = CompileDecision.NORMALIZED
    return CompiledPlan(
        decision=decision, nodes=compiled_nodes, violations=violations,
        warnings=warnings, cost=cost, capabilities=snapshot,
    )
