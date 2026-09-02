"""静态路由意图到任务树节点的保守编译器。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from lumi_orch import InputRef, OutputContract, PlanStep

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.routing_intent import RouteIntent


def _id(prefix: str) -> str:
    return f"{prefix}{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _document_payload(office_docs: list[dict] | None) -> list[dict[str, str]]:
    return [
        {"doc_id": str(item.get("doc_id")), "filename": str(item.get("filename") or "")}
        for item in (office_docs or [])
        if item.get("doc_id")
    ]


def _metadata(
    *, step_id: str, action: str, risk_level: str, output_type: str, input_refs: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    step = PlanStep(
        id=step_id,
        action=action,
        input_contract=[InputRef(**item) for item in (input_refs or [])],
        output_contract=OutputContract(artifact_type=output_type),
        risk_level=risk_level if risk_level in {"read_only", "write", "external_send", "system_command"} else "read_only",
    )
    return {"plan_step": step.model_dump(mode="json")}


def compile_static_route(request: str, intent: RouteIntent, office_docs: list[dict] | None = None) -> list[TaskNode] | None:
    """将动作类型预先确定的请求编译为静态 DAG；其余请求返回 ``None``。"""
    actions = tuple(step.action for step in intent.action_steps) or intent.actions
    if not actions or intent.requires_network or intent.requires_side_effect or "lookup_history" in actions or "task_result" in intent.objects:
        return None
    documents = _document_payload(office_docs)
    has_source = bool(documents) or intent.requires_retrieval
    has_read = any(action in actions for action in ("read", "query", "analyze"))
    has_reasoning = any(action in actions for action in ("analyze", "create", "converse"))
    if documents and "transform" in actions and not has_reasoning:
        node_id = _id("s")
        return [TaskNode(
            id=node_id,
            name="按要求转换文件",
            agent="office_script",
            params={"task": request, "doc_ids": [item["doc_id"] for item in documents], "office_docs": documents},
            metadata=_metadata(step_id=node_id, action="transform", risk_level=intent.risk_level, output_type="file"),
        )]
    if has_source and has_read and has_reasoning:
        read_id, output_id = _id("r"), _id("g")
        return [
            TaskNode(
                id=read_id,
                name="读取相关资料",
                agent="retrieval",
                params={"query": request, "top_k": 5, "doc_ids": [item["doc_id"] for item in documents], "office_docs": documents},
                metadata=_metadata(step_id=read_id, action="read", risk_level="read_only", output_type="retrieval_context"),
            ),
            TaskNode(
                id=output_id,
                name="分析并生成结果",
                agent="direct_llm",
                params={"instruction": "基于前序步骤提供的资料完成用户请求。如果资料不足，明确说明缺口，不要编造。\n用户请求：" + request},
                depends_on=[read_id],
                metadata=_metadata(
                    step_id=output_id,
                    action="analyze" if "analyze" in actions else "create",
                    risk_level="read_only",
                    output_type="text",
                    input_refs=[{"source_step": read_id, "field": "content"}],
                ),
            ),
        ]
    return None
