"""规划模型提示词及运行时能力摘要。"""

from __future__ import annotations


_FALLBACK_AGENT_LINES = (
    "- retrieval：检索知识库/项目索引定位信息，params 用 {\"query\": \"检索词\", \"top_k\": 5}\n"
    "- code_reader：在本地代码项目里定位并读取相关文件，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"定位/分析指令\", \"target_file\": \"可选文件路径\"}\n"
    "- code_writer：生成或修改本地代码文件并写回，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"编码指令\", \"target_file\": \"可选文件路径\", \"original_content\": \"可选，来自 reader\"}\n"
    "- code_tester：按项目类型自动选择并运行合适的验证命令，params 用 {\"project_id\": \"项目ID\"}\n"
    "- code_reviewer：审查已有代码或改动，params 用 {\"project_id\": \"项目ID\", \"instruction\": \"审查要求\", \"target_file\": \"可选文件路径\"}\n"
    "- code：旧版单节点代码任务（定位→生成→写回），params 用 {\"project_id\": \"项目ID\", \"instruction\": \"指令\"}\n"
)


def known_agents() -> tuple[str, ...]:
    """从注册表读取允许进入计划的执行角色。"""
    try:
        from app.agents.core.registry import AgentRegistry

        names = tuple(AgentRegistry.names())
    except Exception:  # noqa: BLE001
        names = ()
    return names or ("retrieval", "web_research")


def agent_prompt_lines() -> str:
    """从注册表生成模型可见角色，不维护第二份角色清单。"""
    try:
        from app.agents.core.registry import AgentRegistry

        agents = AgentRegistry.list()
    except Exception:  # noqa: BLE001
        agents = []
    if not agents:
        return _FALLBACK_AGENT_LINES
    return "\n".join(
        f"- {agent.name}：{agent.description}{f'，{agent.params_help}' if agent.params_help else ''}"
        for agent in sorted(agents, key=lambda item: item.name)
    )


def build_planner_prompt() -> str:
    """构建结构化规划提示词；运行时工具候选由另一函数单独注入。"""
    from app.core.agent_security import UNTRUSTED_CONTENT_RULES

    return (
        "你是办公任务规划器，只输出可校验的 JSON 计划。\n"
        "把用户目标拆成最少的原子步骤；每个 atomic_step 只有一个目标和一次工具调用。"
        "读后写、搜索后总结、或同一资源的读写必须拆步；互不依赖的读取/检索/分析保持 depends_on=[]。"
        "只从下方候选能力中选择工具。atomic_step 的 params 必须是 "
        "{\"instruction\":\"唯一目标\",\"preferred_tool\":\"候选工具名\",\"fallback_tools\":[],\"inputs\":{}}。"
        "目标文档、项目、账户或候选工具要求的字段不明确时，返回 clarification，绝不编造 ID 或参数。"
        "若候选中明确列出 Workflow Skill，才可使用 workflow_skill，params 为 "
        "{\"skill_name\":\"候选工作流名\",\"inputs\":{}}。"
        "只有下一步必须使用上一步结果、共享资源有先后关系，或用户明确要求顺序时填写 depends_on。"
        "同一批无依赖节点最多生成 5 个；超过 5 个相似子任务时，拆成后续滚动批次，"
        "不要一次性扇出大量模型或外部请求。"
        "不得为了形式完整虚构步骤，也不得把多个独立操作吞进一个节点。\n"
        "可用执行 agent：\n" + agent_prompt_lines()
        + "\n严格输出 JSON（不要代码块围栏、不要解释）：\n"
        "{\"plan\":\"给用户看的执行计划\",\"tasks\":[{\"id\":\"t1\",\"name\":\"任务名\",\"agent\":\"retrieval\",\"params\":{},\"depends_on\":[]}],\"clarification\":\"\"}\n"
        "意图不明确或缺少关键信息时，tasks 留空、clarification 填需要确认的问题。\n\n"
        + UNTRUSTED_CONTENT_RULES
    )


_DOCUMENT_SCOPED_TOOL_NAMES = frozenset({
    "inspect_document_set",
    "read_document",
    "office_doc_read",
    "office_doc_analyze",
    "office_doc_edit",
})


async def runtime_capability_note(
    request: str,
    user_id: str = "",
    office_docs: list[dict] | None = None,
) -> str:
    """提供已鉴权的候选工具摘要，避免模型提出不可执行计划。"""
    try:
        from app.agents.skills.executor import select_capabilities_for_request

        capabilities = await select_capabilities_for_request(request, "office", user_id=user_id)
        # 文档工具只有在本次 Job 已经注入了经授权的附件时才是可执行能力。
        # 不能仅因用户说了“文档”就把编辑工具交给规划模型，否则模型容易
        # 为普通文本分析臆造 doc_id，之后才由编译器拒绝，白白创建失败任务。
        # 此处是规划候选的硬边界；编译器仍保留自己的 doc_id 校验作为纵深防御。
        authorized_documents = any(
            isinstance(item, dict) and str(item.get("doc_id") or "").strip()
            for item in (office_docs or [])
        )
        if not authorized_documents:
            capabilities = [
                capability
                for capability in capabilities
                if capability.name not in _DOCUMENT_SCOPED_TOOL_NAMES
                and "doc_id" not in set(getattr(capability, "plan_required_fields", None) or [])
            ]
        entries: list[str] = []
        for capability in capabilities:
            schema = capability.parameters if isinstance(capability.parameters, dict) else {}
            required = schema.get("required") if isinstance(schema, dict) else []
            flags = (["required=" + ",".join(str(item) for item in required[:8])] if required else [])
            plan_required = list(getattr(capability, "plan_required_fields", None) or [])
            if plan_required:
                flags.append("plan_required=" + ",".join(str(item) for item in plan_required[:8]))
            if capability.write_op:
                flags.append("write=true")
            if capability.requires_confirmation:
                flags.append("confirmation=true")
            entries.append(f"{capability.name}({'; '.join(flags) or 'read'}): {str(capability.description or '').replace(chr(10), ' ')[:120]}")
        from app.services.user_workflow_skills import get_visible_workflow_skills

        workflows = await get_visible_workflow_skills(user_id)
        workflow_entries = [
            f"{item.name}(workflow; source={item.source}; tools={','.join(item.allowed_tools) or 'none'}): "
            f"{str(item.description or '').replace(chr(10), ' ')[:120]}"
            for item in workflows
            if item.supports_scene("office")
        ]
        scope_note = (
            "本次没有已授权办公附件；严禁规划需要 doc_id 的读取、分析或编辑工具。"
            if not authorized_documents
            else "本次已注入授权办公附件；涉及文档工具时必须使用清单中的真实 doc_id。"
        )
        tool_note = (
            "\n" + scope_note
            + "\n当前请求可用的候选 Tool（已按权限、场景和运行时状态收窄；atomic_step.preferred_tool 必须从此列表选择）：\n- "
            + "\n- ".join(entries)
        )
        workflow_note = (
            "\n当前用户可见的 Workflow Skill（仅可作为 workflow_skill.skill_name，不可作为 Tool 调用）：\n- "
            + "\n- ".join(workflow_entries)
            if workflow_entries else ""
        )
        return tool_note + workflow_note
    except Exception:  # noqa: BLE001
        return ""
