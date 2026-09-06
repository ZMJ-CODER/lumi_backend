"""任务树提交前的纯规范化逻辑。"""

from __future__ import annotations

import re
from collections.abc import MutableSequence, MutableMapping
from typing import Any, Protocol


class PlanNode(Protocol):
    id: str
    name: str
    agent: str
    params: MutableMapping[str, Any]
    depends_on: MutableSequence[str]
    metadata: MutableMapping[str, Any]


_OUTPUT_LENGTH = re.compile(r"(?:不少于|至少|约|大约|控制在|写|生成)?\s*(\d{3,6})\s*(?:字|字符)")


def apply_generation_runtime_hints(nodes: MutableSequence[PlanNode], request: str) -> None:
    """为文本生成节点补齐可审计的输出预算和动态超时提示。

    Worker 的默认 ``max_tokens`` 不能只存在于函数体里，否则 JobSpec 冻结时
    看不到真实生成规模，会把长文节点按普通 IO 的 60 秒策略执行。这里只从
    用户明确的篇幅要求和节点自身指令推导执行事实，不做业务意图路由。
    """
    request_text = str(request or "")
    for node in nodes:
        if node.agent != "direct_llm":
            continue
        params = node.params or {}
        instruction = str(params.get("instruction") or node.name or "")
        combined = f"{request_text}\n{instruction}"
        lengths = [int(value) for value in _OUTPUT_LENGTH.findall(combined)]
        requested_chars = max(lengths, default=0)
        explicitly_long = requested_chars >= 1500 or any(
            marker in combined
            for marker in ("长文", "长篇", "完整报告", "详细报告", "深度报告", "详细分析")
        )
        if not explicitly_long:
            continue
        # 中文长文本按约 1.2 token/字预留并加结构化输出余量；Worker 当前
        # 上限为 4000，因此规格也必须使用同一个上限，避免声明和执行分裂。
        estimated = min(4000, max(2000, int(requested_chars * 1.2) + 256))
        params.setdefault("max_tokens", estimated)
        params.setdefault("estimated_output_tokens", estimated)
        params.setdefault("timeout_hint", "long_generation")
        node.params = params


def prefer_atomic_steps(nodes: MutableSequence[PlanNode], request: str) -> None:
    """将历史办公角色收束为声明能力的原子工具节点。"""
    tool_map = {"retrieval": "query_knowledge", "office_todo": "todo_manager", "office_calendar": "calendar_manager"}
    text_tools = {"email": "compose_email", "doc": "compose_official_doc", "rewrite": "rewrite_text", "summary": "summarize_text", "minutes": "meeting_minutes", "extract": "extract_info", "invoice": "invoice_parse", "compliance": "compliance_check"}
    research_tools = {"competitor": "competitor_analysis", "document_qa": "document_qa", "customer_service": "customer_service", "daily_report": "daily_report"}
    doc_tools = {"read": "office_doc_read", "edit": "office_doc_edit", "analyze": "office_doc_analyze"}
    system_tools = {"open_app": "OpenApp", "open_file": "OpenFile", "open_url": "OpenUrl", "send_email": "send_email", "ps": "ProcessList", "kill": "ProcessSignal", "env": "SystemInfo", "datetime": "DateTime", "curl": "curl"}
    for node in nodes:
        preferred = tool_map.get(node.agent)
        if node.agent == "office_text":
            preferred = text_tools.get(str(node.params.get("task") or ""))
        elif node.agent == "office_research":
            preferred = research_tools.get(str(node.params.get("mode") or ""))
        elif node.agent == "office_doc":
            preferred = doc_tools.get(str(node.params.get("mode") or "read"))
        elif node.agent == "office_system":
            preferred = system_tools.get(str(node.params.get("task") or "open_app"))
        if not preferred:
            continue
        old_agent, original = node.agent, dict(node.params or {})
        instruction = str(original.get("instruction") or original.get("query") or original.get("content") or node.name or request)
        node.agent = "atomic_step"
        node.params = {"instruction": instruction, "preferred_tool": preferred, "fallback_tools": ["office_doc_read"] if preferred == "office_doc_analyze" else [], "inputs": original}
        node.metadata = {**(node.metadata or {}), "legacy_agent": old_agent}


def enforce_react_complexity_policy(
    nodes: MutableSequence[PlanNode],
    complexity_level: str | None,
) -> None:
    """只允许 M3 节点进入 ReAct，其余复杂度降为单步执行。"""
    level = str(complexity_level or "").lower()
    if level not in {"m0", "m1", "m2"}:
        return
    for node in nodes:
        if node.agent != "react_step":
            continue
        metadata = {**(node.metadata or {}), "react_blocked_by_complexity": level}
        preferred = str((node.params or {}).get("preferred_tool") or "").strip()
        if preferred:
            node.agent = "atomic_step"
            node.params = {
                "instruction": str((node.params or {}).get("instruction") or node.name),
                "preferred_tool": preferred,
                "inputs": dict((node.params or {}).get("inputs") or {}),
            }
        else:
            node.agent = "direct_llm"
            node.params = {
                "instruction": str((node.params or {}).get("instruction") or node.name),
            }
        node.metadata = metadata


def adapt_unavailable_manifest_workers(nodes: MutableSequence[PlanNode], workers: dict[str, Any]) -> None:
    """仅在裁剪部署中将不可用角色收敛到受限 React Worker。"""
    if "react_step" not in workers:
        return
    for node in nodes:
        if node.agent not in workers and node.agent != "collect_results":
            node.metadata = {**(node.metadata or {}), "route_worker_fallback": node.agent}
            node.agent = "react_step"
            node.params.setdefault("max_rounds", 2)
        if node.agent == "collect_results" and node.agent not in workers:
            node.metadata = {**(node.metadata or {}), "manifest_collect_skipped": True}
            node.agent = "react_step"
            node.params = {"instruction": "汇集并简要列出本批清单的已完成、失败和取消结果。", "max_rounds": 1}


def serialize_steps(nodes: MutableSequence[PlanNode]) -> None:
    """将有效 DAG 折叠为拓扑有序的单链，供串行提交策略使用。"""
    if len(nodes) < 2:
        return
    by_id = {node.id: node for node in nodes}
    indegree = {node.id: 0 for node in nodes}
    children = {node.id: [] for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            if dependency in by_id:
                indegree[node.id] += 1
                children[dependency].append(node.id)
    ready = [node.id for node in nodes if indegree[node.id] == 0]
    ordered: list[PlanNode] = []
    while ready:
        node_id = ready.pop(0)
        ordered.append(by_id[node_id])
        for child_id in children[node_id]:
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)
    if len(ordered) != len(nodes):
        return
    for index, node in enumerate(ordered):
        node.depends_on = [] if index == 0 else [ordered[index - 1].id]
    nodes[:] = ordered
