"""Pure plan normalization helpers shared by submit and replan paths."""

from __future__ import annotations


def prefer_atomic_steps(nodes, request: str) -> None:
    """Map legacy office roles to explicit atomic capabilities in place."""
    tool_map = {
        "retrieval": "query_knowledge",
        "web_research": "web_search",
        "office_todo": "todo_manager",
        "office_calendar": "calendar_manager",
    }
    text_tools = {
        "email": "compose_email",
        "doc": "compose_official_doc",
        "rewrite": "rewrite_text",
        "summary": "summarize_text",
        "minutes": "meeting_minutes",
        "extract": "extract_info",
        "invoice": "invoice_parse",
        "compliance": "compliance_check",
    }
    research_tools = {
        "competitor": "competitor_analysis",
        "document_qa": "document_qa",
        "customer_service": "customer_service",
        "daily_report": "daily_report",
    }
    doc_tools = {
        "read": "office_doc_read",
        "edit": "office_doc_edit",
        "analyze": "office_doc_analyze",
    }
    system_tools = {
        "open_app": "open_app",
        "open_file": "open_file",
        "open_url": "open_url",
        "send_email": "send_email",
        "ps": "ps",
        "kill": "kill",
        "env": "env",
        "datetime": "get_datetime",
        "curl": "curl",
    }
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
        old_agent = node.agent
        original = dict(node.params or {})
        instruction = str(
            original.get("instruction")
            or original.get("query")
            or original.get("content")
            or node.name
            or request
        )
        node.agent = "atomic_step"
        node.params = {
            "instruction": instruction,
            "preferred_tool": preferred,
            "fallback_tools": ["office_doc_read"] if preferred == "office_doc_analyze" else [],
            "inputs": original,
        }
        node.metadata = {**(node.metadata or {}), "legacy_agent": old_agent}


def adapt_unavailable_manifest_workers(nodes, workers: dict) -> None:
    """Use the bounded React worker only for deliberately trimmed deployments."""
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
            node.params = {
                "instruction": "汇集并简要列出本批清单的已完成、失败和取消结果。",
                "max_rounds": 1,
            }


def serialize_steps(nodes) -> None:
    """Collapse a valid DAG into a topological single chain in place."""
    if len(nodes) < 2:
        return
    by_id = {node.id: node for node in nodes}
    indegree = {node.id: 0 for node in nodes}
    children = {node.id: [] for node in nodes}
    for node in nodes:
        for dep in node.depends_on:
            if dep in by_id:
                indegree[node.id] += 1
                children[dep].append(node.id)
    ready = [node.id for node in nodes if indegree[node.id] == 0]
    ordered = []
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
