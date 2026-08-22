"""Compile deterministic route intents into safe, typed DAG steps.

This module deliberately covers only action chains whose node types are known
before execution. Requests whose next *action type* depends on an intermediate
result remain on the dynamic ``react_step`` path.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.routing_intent import RouteIntent


class InputRef(BaseModel):
    """A typed reference to a previous step's sanitized result."""

    source_step: str
    field: str = "content"


class OutputContract(BaseModel):
    artifact_type: str
    fields: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    """Planner DSL consumed by the static DAG compiler and audit layer."""

    id: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    input_contract: list[InputRef] = Field(default_factory=list)
    output_contract: OutputContract
    risk_level: Literal["read_only", "write", "external_send", "system_command"] = "read_only"
    depends_on: list[str] = Field(default_factory=list)


def _id(prefix: str) -> str:
    return f"{prefix}{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _doc_payload(office_docs: list[dict] | None) -> list[dict[str, str]]:
    return [
        {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
        for item in (office_docs or [])
        if item.get("doc_id")
    ]


def _step_metadata(
    *,
    step_id: str,
    action: str,
    risk_level: str,
    output_type: str,
    input_refs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    step = PlanStep(
        id=step_id,
        action=action,
        input_contract=[InputRef(**ref) for ref in (input_refs or [])],
        output_contract=OutputContract(artifact_type=output_type),
        risk_level=risk_level if risk_level in {"read_only", "write", "external_send", "system_command"} else "read_only",
    )
    return {"plan_step": step.model_dump(mode="json")}


def compile_static_route(
    request: str,
    intent: RouteIntent,
    office_docs: list[dict] | None = None,
) -> list[TaskNode] | None:
    """Return a static DAG, or ``None`` when dynamic planning is required.

    The compiler is intentionally conservative. External side effects and
    history/feedback branches are not silently converted into a text-only DAG.
    """
    actions = tuple(step.action for step in intent.action_steps) or intent.actions
    if not actions:
        return None
    if intent.requires_network or intent.requires_side_effect or "lookup_history" in actions or "task_result" in intent.objects:
        return None

    docs = _doc_payload(office_docs)
    has_source = bool(docs) or intent.requires_retrieval
    has_read = any(action in actions for action in ("read", "query", "analyze"))
    has_reasoning = any(action in actions for action in ("analyze", "create", "converse"))
    has_transform = "transform" in actions

    # A deterministic file transformation is already handled by the dedicated
    # conversion path. Keep this branch for colloquial transforms such as
    # compression where the office script worker owns the file contract.
    if docs and has_transform and not has_reasoning:
        transform_id = _id("s")
        return [
            TaskNode(
                id=transform_id,
                name="按要求转换文件",
                agent="office_script",
                params={
                    "task": request,
                    "doc_ids": [item["doc_id"] for item in docs],
                    "office_docs": docs,
                },
                metadata=_step_metadata(
                    step_id=transform_id,
                    action="transform", risk_level=intent.risk_level, output_type="file",
                ),
            )
        ]

    # Retrieval/read followed by fixed analysis or generation is a static DAG:
    # the result changes, but the next action type is known up front.
    if has_source and has_read and has_reasoning:
        read_id = _id("r")
        output_id = _id("g")
        read_node = TaskNode(
            id=read_id,
            name="读取相关资料",
            agent="retrieval",
            params={
                "query": request,
                "top_k": 5,
                "doc_ids": [item["doc_id"] for item in docs],
                "office_docs": docs,
            },
            metadata=_step_metadata(
                step_id=read_id,
                action="read", risk_level="read_only", output_type="retrieval_context",
            ),
        )
        output_node = TaskNode(
            id=output_id,
            name="分析并生成结果",
            agent="direct_llm",
            params={
                "instruction": (
                    "基于前序步骤提供的资料完成用户请求。"
                    "如果资料不足，明确说明缺口，不要编造。\n用户请求：" + request
                ),
            },
            depends_on=[read_id],
            metadata=_step_metadata(
                step_id=output_id,
                action="analyze" if "analyze" in actions else "create",
                risk_level="read_only",
                output_type="text",
                input_refs=[{"source_step": read_id, "field": "content"}],
            ),
        )
        return [read_node, output_node]

    return None
