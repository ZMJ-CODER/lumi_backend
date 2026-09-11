"""工作区工作流共用的桌面能力选择与工具 schema 投影。

两个工作流 Skill（``workspace_operation`` / ``workspace_code_change``）都面向同一份
桌面 MCP 能力集，并且都必须遵守同一条规则：**读取阶段只把聚合入口
``workspace_navigator`` 给模型**，内部原子读取名（workspace_list / workspace_read /
workspace_search / workspace_catalog / workspace_stat / workspace_content_extract）
保留在 MCP 注册表与后端内部依赖里，不进入 function calling schema。
"""

from __future__ import annotations

from typing import Any


def tool_definitions(capabilities) -> list[dict]:
    """把能力转成 function calling 定义，并隐藏服务端注入的敏感标识。"""
    definitions: list[dict] = []
    for capability in capabilities:
        definition = capability.to_tool_definition()
        function = definition.get("function") or {}
        schema = dict(function.get("parameters") or {})
        properties = dict(schema.get("properties") or {})
        # workspace_id 由 SkillContext/executor 注入，模型不得编造安全敏感标识。
        properties.pop("workspace_id", None)
        required = [item for item in (schema.get("required") or []) if item != "workspace_id"]
        schema["properties"] = properties
        schema["required"] = required
        function["parameters"] = schema
        definition["function"] = function
        definitions.append(definition)
    return definitions


async def select_desktop_workspace_capabilities(
    user_id: str,
    scene: str,
    user_role: str,
    workspace_id: str,
    allowed_tools,
) -> list[Any]:
    """返回本次工作流可用且已授权的桌面工作区能力。

    * 聚合入口 ``workspace_navigator`` 由后端合成（Electron 只发布内部原子工具），
      因此这里必须显式补上，否则读取阶段会“看不到任何工具”。
    * 其余能力仍然只从当前用户已连接的 Electron 工具清单里挑选。
    """
    wanted = {str(name or "") for name in (allowed_tools or ()) if str(name or "")}
    if not wanted:
        return []
    from app.agents.skills.executor import (
        get_desktop_mcp_capabilities,
        get_workspace_navigator_capability,
    )
    from app.services.workspace_context import WORKSPACE_NAVIGATOR, resolve_workspace_desktop

    selected: list[Any] = []
    route = resolve_workspace_desktop(user_id, str(workspace_id or "").strip()) if workspace_id else {}
    server_name = str((route or {}).get("server_name") or "")
    navigator_names = {
        f"mcp__{server_name}__{WORKSPACE_NAVIGATOR}" if server_name else "",
    }
    if wanted & navigator_names:
        selected.extend(
            await get_workspace_navigator_capability(user_id, scene, user_role, workspace_id)
        )
    for capability in await get_desktop_mcp_capabilities(user_id, scene, user_role):
        if capability.name in wanted and capability.name not in {item.name for item in selected}:
            selected.append(capability)
    return selected


__all__ = ["select_desktop_workspace_capabilities", "tool_definitions"]
