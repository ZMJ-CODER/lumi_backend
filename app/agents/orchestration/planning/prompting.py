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
        "你是任务规划器。把用户请求拆解为任务计划。\n"
        "默认使用 atomic_step，把任务拆成可独立提交、可独立失败/重试的原子步骤。"
        "每个 atomic_step 只允许一个唯一目标、最多一次 Skill/MCP 调用；需要读后再写、搜索后再总结时必须拆成多个节点。"
        "所有步骤必须按用户叙述的顺序串行执行：除第一个步骤外，每一步都 depends_on 前一步，禁止并行。"
        "必须覆盖用户请求中的每一个动作；上传文档只提供上下文，不能让文档分析任务吞掉打开应用、写文件、发邮件等独立指令。"
        "atomic_step 可以调用当前 office 场景的本地 Skill、system Skill 和 MCP 工具，"
        "但规划时必须唯一指定 preferred_tool，执行器只会向模型暴露这一个工具。"
        "params 用 {\"instruction\":\"本步骤唯一目标\",\"preferred_tool\":\"首选工具名\",\"fallback_tools\":[\"不同原理的备用工具\"],\"inputs\":{}}。"
        "为可能失败的读取、解析、转换步骤规划不同原理且当前已允许的 fallback_tools；备用工具不得与首选工具重复。"
        "涉及同一文件、文档、日历或待办的步骤必须通过 depends_on 表达逻辑顺序；执行器会自动声明资源读写锁。\n"
        "可用执行 agent：\n" + agent_prompt_lines()
        + "\n代码任务建议按文件拆分节点：阅读/定位使用 code_reader，修改使用 code_writer，"
        "写入后用 code_tester 验证；修改已有文件必须明确 target_file，目标不明时先定位。\n"
        "办公任务：文件转换、清洗、合并拆分、导出或生成真实文件优先使用 office_script；"
        "读写文档用 office_doc 并携带 doc_id；文本产出使用 office_text；竞品分析、文档问答等使用 office_research；"
        "产出节点必须依赖读取节点。\n"
        "严格输出 JSON（不要代码块围栏、不要解释）：\n"
        "{\"plan\":\"给用户看的执行计划\",\"tasks\":[{\"id\":\"t1\",\"name\":\"任务名\",\"agent\":\"retrieval\",\"params\":{},\"depends_on\":[]}],\"clarification\":\"\"}\n"
        "意图不明确或缺少关键信息时，tasks 留空、clarification 填需要确认的问题。\n\n"
        + UNTRUSTED_CONTENT_RULES
    )


async def runtime_capability_note(request: str, user_id: str = "") -> str:
    """提供已鉴权的候选工具摘要，避免模型提出不可执行计划。"""
    try:
        from app.agents.skills.executor import select_capabilities_for_request

        capabilities = await select_capabilities_for_request(request, "office", user_id=user_id)
        entries: list[str] = []
        for capability in capabilities:
            schema = capability.parameters if isinstance(capability.parameters, dict) else {}
            required = schema.get("required") if isinstance(schema, dict) else []
            flags = (["required=" + ",".join(str(item) for item in required[:8])] if required else [])
            if capability.write_op:
                flags.append("write=true")
            if capability.requires_confirmation:
                flags.append("confirmation=true")
            entries.append(f"{capability.name}({'; '.join(flags) or 'read'}): {str(capability.description or '').replace(chr(10), ' ')[:120]}")
        return "\n当前请求可用的候选 Skill（已按权限、场景和运行时状态收窄；preferred_tool 必须从此列表选择）：\n- " + "\n- ".join(entries)
    except Exception:  # noqa: BLE001
        return ""
