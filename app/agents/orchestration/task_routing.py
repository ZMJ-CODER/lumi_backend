"""Four-channel routing for office work units.

The scheduler deliberately classifies *execution requirements*, not document
genres.  A task list is first compiled into JSON-safe atomic work units and
then each unit independently selects the cheapest sufficient channel:

``direct_llm`` -> ``deterministic_script`` -> ``rag`` -> ``agent``.

This module is intentionally usable without an LLM.  A planner may enrich the
same schema later, but all routing decisions are validated here before a DAG
sees them.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.agents.orchestration.routing_patterns import (
    EXTERNAL_OPERATION as _EXTERNAL_OPERATION,
    FACTUAL_DOCUMENT_QUESTION as _FACTUAL_DOCUMENT_QUESTION,
    agent_operation_matches as _AGENT_OPERATION_MATCHES,
    file_operation_matches as _FILE_OPERATION_MATCHES,
    MULTI_OPERATION as _MULTI_OPERATION,
    rag_operation_matches as _RAG_OPERATION_MATCHES,
    STATEFUL_REASONING as _STATEFUL_REASONING,
)


class RouteChannel(str, Enum):
    DIRECT_LLM = "direct_llm"
    DETERMINISTIC_SCRIPT = "deterministic_script"
    RAG = "rag"
    AGENT = "agent"


class AtomicWorkItem(BaseModel):
    """Persisted, user-auditable unit of work; indices are 1-based at input."""

    id: str
    instruction: str
    description: str = ""
    estimated_type: RouteChannel = RouteChannel.AGENT
    route_reason: str = ""
    dependencies: list[int] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)
    # A decomposer may emit this only for a real mixed request.  It is flattened
    # before execution; no executor is allowed to treat it as a single tool.
    subtasks: list[dict[str, Any]] = Field(default_factory=list)


class RouteDecision(BaseModel):
    channel: RouteChannel
    reason: str
    estimated_tokens: int = Field(ge=0)


def estimate_tokens(instruction: str, channel: RouteChannel) -> int:
    """Conservative pre-execution budget used as a guardrail, never billing."""
    chars = max(1, len((instruction or "").strip()))
    base = {
        RouteChannel.DIRECT_LLM: 800,
        RouteChannel.DETERMINISTIC_SCRIPT: 1_600,
        RouteChannel.RAG: 1_200,
        RouteChannel.AGENT: 3_500,
    }[channel]
    return min(20_000, base + chars // 2)


def route_atomic_instruction(
    instruction: str,
    *,
    has_authorized_documents: bool = False,
    office_document_count: int = 0,
) -> RouteDecision:
    """Return the legacy result while policy is in shadow mode.

    The declarative engine is evaluated only after the existing routing result
    is known.  This makes migration observable without planning or executing a
    second task.
    """
    legacy = _route_atomic_instruction_legacy(
        instruction,
        has_authorized_documents=has_authorized_documents,
        office_document_count=office_document_count,
    )
    policy = _load_routing_policy()
    if policy is None:
        return legacy
    try:
        from app.agents.orchestration.policy.features import build_routing_features

        candidate = policy.evaluate(
            build_routing_features(
                instruction,
                has_authorized_documents=has_authorized_documents,
                office_document_count=office_document_count,
            )
        )
    except Exception as exc:  # noqa: BLE001
        # A policy error cannot make a user request unrouteable. The load-time
        # linter catches configuration defects; this protects future hooks.
        _record_policy_error(exc)
        return legacy
    if candidate is None:
        return legacy
    policy_decision = RouteDecision(
        channel=candidate.channel,
        reason=_POLICY_REASON_TEXT.get(candidate.reason_code, candidate.reason_code),
        estimated_tokens=estimate_tokens(instruction, candidate.channel),
    )
    mode = _routing_policy_mode()
    if mode == "enforce":
        return policy_decision
    if mode == "shadow":
        _record_policy_shadow_result(legacy, policy_decision, candidate)
    return legacy


_POLICY_REASON_TEXT = {
    "explicit_file_conversion": "明确的文件转换或批处理",
    "multi_document_targeting": "多文档事实问题需要先定位授权附件",
    "explicit_rag_lookup": "需要从已授权资料检索事实",
    "authorized_document_lookup": "需要从已授权资料检索事实",
    "agent_coordination": "需要多步协调或外部状态操作",
    "direct_response": "无需外部状态的直接内容生成",
}


def _load_routing_policy():
    from app.agents.orchestration.policy.runtime import load_routing_policy

    return load_routing_policy()


def _routing_policy_mode() -> str:
    from app.agents.orchestration.policy.runtime import routing_policy_mode

    return routing_policy_mode()


def _record_policy_shadow_result(
    legacy: RouteDecision,
    candidate: RouteDecision,
    policy_candidate: Any,
) -> None:
    from app.monitoring.context import MonitorContext
    from app.monitoring.logger import monitor_logger

    divergent = candidate.channel != legacy.channel
    monitor_logger.warning(
        "路由策略影子结果与旧路由不同" if divergent else "路由策略影子规则命中",
        event_type="policy_shadow_divergence" if divergent else "policy_shadow_match",
        category="routing",
        code="ROUTING_POLICY_SHADOW_DIVERGENCE" if divergent else "ROUTING_POLICY_SHADOW_MATCH",
        context=MonitorContext(component="task_routing"),
        metadata={
            "rule_id": policy_candidate.rule_id,
            "policy_sha256": policy_candidate.policy_sha256,
            "legacy_channel": legacy.channel.value,
            "candidate_channel": candidate.channel.value,
            "requirements": list(policy_candidate.requirements),
            "risk_level": policy_candidate.risk_level,
            "require_clarification": policy_candidate.require_clarification,
            "audit_metadata": dict(policy_candidate.audit_metadata),
        },
    )


def _record_policy_error(exc: Exception) -> None:
    from app.monitoring.context import MonitorContext
    from app.monitoring.logger import monitor_logger

    monitor_logger.error(
        "路由策略求值失败，继续使用旧路由",
        event_type="policy_evaluation_failure",
        category="routing",
        code="ROUTING_POLICY_EVALUATION_FAILED",
        context=MonitorContext(component="task_routing"),
        metadata={"error": str(exc)[:300]},
        exc_info=exc,
    )


def _route_atomic_instruction_legacy(
    instruction: str,
    *,
    has_authorized_documents: bool = False,
    office_document_count: int = 0,
) -> RouteDecision:
    """Pick one channel for a *single* atomic request.

    The ordering is intentional.  A request that needs several capabilities is
    not a route target; it belongs to the agent channel until decomposition
    expands it into a local subgraph.
    """
    text = (instruction or "").strip()
    if _MULTI_OPERATION.search(text) or _EXTERNAL_OPERATION.search(text) or _STATEFUL_REASONING.search(text) or _AGENT_OPERATION_MATCHES(text):
        channel = RouteChannel.AGENT
        return RouteDecision(channel=channel, reason="需要多步协调或外部状态操作", estimated_tokens=estimate_tokens(text, channel))
    # A document-set question cannot safely be answered by a document-agnostic
    # RAG node: it must first inspect the submitted set and select a document.
    if office_document_count >= 2 and _FACTUAL_DOCUMENT_QUESTION.search(text):
        channel = RouteChannel.RAG
        return RouteDecision(channel=channel, reason="多文档事实问题需要先定位授权附件", estimated_tokens=estimate_tokens(text, channel))
    if _FILE_OPERATION_MATCHES(text):
        channel = RouteChannel.DETERMINISTIC_SCRIPT
        return RouteDecision(channel=channel, reason="明确的文件转换或批处理", estimated_tokens=estimate_tokens(text, channel))
    if _RAG_OPERATION_MATCHES(text) or (
        # Keep the legacy fallback aligned with the governed feature contract:
        # “根据上传的合同说明违约条款” is a read-only, document-grounded
        # question even though it does not contain an explicit "查询" verb.
        has_authorized_documents and re.search(r"(?iu)(?:查|找|问答|总结|提取|分析|检索|回答|说明)", text)
    ):
        channel = RouteChannel.RAG
        return RouteDecision(channel=channel, reason="需要从已授权资料检索事实", estimated_tokens=estimate_tokens(text, channel))
    channel = RouteChannel.DIRECT_LLM
    return RouteDecision(channel=channel, reason="无需外部状态的直接内容生成", estimated_tokens=estimate_tokens(text, channel))


def normalize_atomic_items(
    raw_items: list[str] | list[dict[str, Any]],
    *,
    has_authorized_documents: bool = False,
    office_document_count: int = 0,
) -> list[AtomicWorkItem]:
    """Validate/route externally extracted work items and flatten subgraphs."""
    normalized: list[AtomicWorkItem] = []
    for position, raw in enumerate(raw_items, start=1):
        data = dict(raw) if isinstance(raw, dict) else {"instruction": str(raw)}
        instruction = re.sub(r"\s+", " ", str(data.get("instruction") or "")).strip()
        if not instruction:
            continue
        decision = route_atomic_instruction(
            instruction,
            has_authorized_documents=has_authorized_documents,
            office_document_count=office_document_count,
        )
        raw_deps = data.get("dependencies") or []
        dependencies = [
            int(value) for value in raw_deps
            if isinstance(value, int) and 0 < value < position
        ]
        item = AtomicWorkItem(
            id=f"item-{position}",
            instruction=instruction[:2000],
            description=str(data.get("description") or instruction)[:500],
            estimated_type=decision.channel,
            route_reason=decision.reason,
            dependencies=dependencies,
            estimated_tokens=decision.estimated_tokens,
            subtasks=list(data.get("subtasks") or [])[:12],
        )
        normalized.append(item)
    return normalized


def manifest_estimated_tokens(items: list[AtomicWorkItem]) -> int:
    return sum(item.estimated_tokens for item in items)
