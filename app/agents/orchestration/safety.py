"""DAG 节点的资源声明与副作用安全元数据。"""

from __future__ import annotations

import hashlib
import json

from app.agents.orchestration.models import ResourceClaim, TaskNode


_WRITE_HINTS = (
    "write", "edit", "delete", "rename", "move", "create", "install",
    "send", "apply", "commit", "calendar", "todo", "kill",
)


def _tool_is_write(tool: str) -> bool:
    if not tool:
        return False
    if tool.startswith("mcp__"):
        # MCP 暂无统一安全元数据，P0 先按可能写操作保守处理。
        return True
    try:
        from app.agents.skills.registry import SkillRegistry

        skill = SkillRegistry.get(tool)
        if skill is not None:
            return bool(skill.write_op or skill.requires_confirmation)
    except Exception:  # noqa: BLE001
        pass
    lower = tool.lower()
    return any(hint in lower for hint in _WRITE_HINTS)


def _normalize_claims(claims: list[ResourceClaim], user_id: str) -> list[ResourceClaim]:
    merged: dict[str, str] = {}
    for claim in claims:
        key = str(claim.key or "").strip()
        if not key:
            continue
        if not key.startswith("global:") and not key.startswith("user:"):
            key = f"user:{user_id}:{key}"
        mode = "write" if str(claim.mode).lower() == "write" else "read"
        if merged.get(key) == "write" or mode == "write":
            merged[key] = "write"
        else:
            merged[key] = "read"
    return [ResourceClaim(key=k, mode=merged[k]) for k in sorted(merged)]


def _render_resource_template(template: str, values: dict) -> str:
    rendered = str(template or "")
    for key, value in values.items():
        if isinstance(value, (str, int, float)):
            rendered = rendered.replace("{" + str(key) + "}", str(value))
    return "" if "{" in rendered or "}" in rendered else rendered


def prepare_node_safety(node: TaskNode, user_id: str, job_id: str) -> None:
    """补齐旧模板/旧 Planner 未声明的资源与幂等字段。"""
    params = node.params or {}
    inputs = params.get("inputs") if isinstance(params.get("inputs"), dict) else params
    inputs = inputs or {}
    tool = str(params.get("preferred_tool") or "")
    claims = list(node.resource_claims or [])
    if node.agent == "react_step":
        # 动态工具在运行时才确定，先用用户级写锁隔离整个循环。
        claims.append(ResourceClaim(key=f"react:user:{user_id}", mode="write"))
    if tool:
        try:
            from app.agents.skills.registry import SkillRegistry

            skill = SkillRegistry.get(tool)
            if skill is not None:
                for template in skill.resource_templates or []:
                    key = _render_resource_template(template, inputs)
                    if key:
                        claims.append(
                            ResourceClaim(key=key, mode="write" if _tool_is_write(tool) else "read")
                        )
        except Exception:  # noqa: BLE001
            pass

    doc_ids: list[str] = []
    if inputs.get("doc_id"):
        doc_ids.append(str(inputs["doc_id"]))
    doc_ids.extend(str(x) for x in (inputs.get("doc_ids") or []) if x)
    doc_write = (
        str(inputs.get("mode") or "").lower() in {"edit", "write", "commit"}
        or "edit" in tool.lower()
        or node.agent == "office_script"
    )
    for doc_id in doc_ids:
        claims.append(ResourceClaim(key=f"office-doc:{doc_id}", mode="write" if doc_write else "read"))

    project_id = str(inputs.get("project_id") or params.get("project_id") or "")
    path = str(
        inputs.get("target_file")
        or inputs.get("file_path")
        or inputs.get("path")
        or params.get("target_file")
        or ""
    ).replace("\\", "/")
    if project_id:
        key = f"project:{project_id}" + (f":file:{path}" if path else "")
        mode = "write" if node.agent in {"code", "code_writer"} or _tool_is_write(tool) else "read"
        claims.append(ResourceClaim(key=key, mode=mode))

    if tool in {"todo_manager"}:
        claims.append(ResourceClaim(key="todo", mode="write"))
    if tool in {"calendar_manager"}:
        claims.append(ResourceClaim(key="calendar", mode="write"))
    if tool.startswith("mcp__") and not claims:
        server = tool.split("__", 2)[1] if "__" in tool else "unknown"
        claims.append(ResourceClaim(key=f"mcp-server:{server}", mode="write"))

    is_write = any(c.mode == "write" for c in claims) or _tool_is_write(tool)
    if is_write and not claims:
        claims.append(ResourceClaim(key=f"tool:{tool or node.agent}", mode="write"))
    node.resource_claims = _normalize_claims(claims, user_id)

    if is_write and not node.idempotency_key:
        material = json.dumps(
            {"job": job_id, "node": node.id, "tool": tool or node.agent, "inputs": inputs},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        node.idempotency_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
        node.effect_status = node.effect_status or "pending"


def is_effectful(node: TaskNode) -> bool:
    return bool(node.idempotency_key or any(c.mode == "write" for c in node.resource_claims))
