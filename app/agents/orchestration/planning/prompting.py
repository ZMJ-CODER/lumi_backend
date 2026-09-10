"""规划模型提示词及运行时能力摘要。"""

from __future__ import annotations


_FALLBACK_AGENT_LINES = (
    "- decision_node：受控阶段决策（request_domain/clarify/continue/finish），params 用 {\"decision\":\"continue\"}\n"
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
    return names or ("retrieval", "atomic_step")


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
    """Build the business-neutral office planning contract.

    The planner deliberately has no Tool or Skill names in its vocabulary.  It
    describes *what capability is needed*, while the dispatcher binds that
    capability to a user-visible Skill afterwards.  Keeping this boundary here
    prevents every new business workflow from becoming another planner branch.
    """
    from app.core.agent_security import UNTRUSTED_CONTENT_RULES

    return (
        "你是办公任务的抽象规划器，只输出可校验的 JSON。你不认识业务类别，"
        "也不知道任何 Skill、Tool、Agent、工作流或内部实现名称；绝不猜测它们。\n"
        "你的职责是把用户目标表达为通用能力画像和逻辑步骤，之后由系统的能力调度器匹配实现。\n"
        "任务画像字段：goal 只能为 GENERATE/RETRIEVE/ANALYZE/EXECUTE/INTERACT；"
        "required_sources 只能由 USER_INPUT/ATTACHED_FILE/LOCAL_KNOWLEDGE/PUBLIC_WEB/"
        "EXTERNAL_API/SYSTEM_STATE 组成；complexity 为 ATOMIC/SEQUENTIAL/DYNAMIC；"
        "safety_level 为 READ_ONLY/SAFE_WRITE/RISKY_WRITE/CRITICAL；confidence 为 0 到 1。\n"
        "信息边界：实时、近期、官方公开事实需要 PUBLIC_WEB 或 EXTERNAL_API；"
        "已上传附件需要 ATTACHED_FILE；内部知识或业务记录需要 LOCAL_KNOWLEDGE；"
        "仅根据用户给出的背景即可完成的写作、解释和分析使用 USER_INPUT。"
        "不要因为用户没说“搜索”就把实时或可验证事实当成通用常识。\n"
        "abstract_tasks 中每项只描述一个逻辑步骤：id、name、instruction、profile、depends_on、is_critical。"
        "不得出现业务名、Skill 名、Tool 名、Agent 名、domain、stage、params 或具体调用方式。"
        "读后分析、获取后汇总、或同一对象的读写必须拆成步骤；无依赖的步骤可以并行；"
        "单批最多 5 项。步骤的 depends_on 只能引用先前步骤 id。\n"
        "可逆文本交付在资料不完整时，仍规划 GENERATE + USER_INPUT 的初稿步骤；"
        "只有写入、发送、审批、提交、删除或其他不可逆/高风险操作缺少明确对象或授权时，"
        "才使用 clarification，且此时 abstract_tasks 必须为空。不要编造对象 ID。\n"
        "示例：‘比较两项最新公开规范并给建议’应先规划 RETRIEVE + PUBLIC_WEB，再规划 "
        "ANALYZE + USER_INPUT，后者依赖前者；‘根据这段背景写延期说明’是一个 "
        "GENERATE + USER_INPUT 步骤。\n"
        "严格输出 JSON（不要代码围栏、不要解释）：\n"
        "{\"plan\":\"简短执行计划\",\"task_profile\":{\"goal\":\"RETRIEVE\",\"required_sources\":[\"PUBLIC_WEB\"],\"complexity\":\"SEQUENTIAL\",\"safety_level\":\"READ_ONLY\",\"confidence\":0.9,\"entities\":{}},\"abstract_tasks\":[{\"id\":\"n1\",\"name\":\"获取公开事实\",\"instruction\":\"围绕用户问题获取可验证的公开事实\",\"profile\":{\"goal\":\"RETRIEVE\",\"required_sources\":[\"PUBLIC_WEB\"],\"complexity\":\"ATOMIC\",\"safety_level\":\"READ_ONLY\",\"confidence\":0.9,\"entities\":{}},\"depends_on\":[],\"is_critical\":true}],\"clarification\":\"\"}\n\n"
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
    """Provide only runtime source boundaries to the abstract planner.

    Concrete Tool/Skill lists belong to the dispatcher, not the model that
    creates abstract nodes.  This function intentionally retains its public
    name for callers and tests from the prior contract.
    """
    try:
        # 文档工具只有在本次 Job 已经注入了经授权的附件时才是可执行能力。
        # 不能仅因用户说了“文档”就把编辑工具交给规划模型，否则模型容易
        # 为普通文本分析臆造 doc_id，之后才由编译器拒绝，白白创建失败任务。
        # 此处是规划候选的硬边界；编译器仍保留自己的 doc_id 校验作为纵深防御。
        authorized_documents = any(
            isinstance(item, dict) and str(item.get("doc_id") or "").strip()
            for item in (office_docs or [])
        )
        scope_note = (
            "本次没有已授权附件。不得把 ATTACHED_FILE 作为可执行来源，也不得编造文件标识。"
            if not authorized_documents else
            "本次存在已授权附件。需要附件内容时将 required_sources 标为 ATTACHED_FILE；"
            "不输出任何文件标识或实现名称。"
        )
        return "\n运行时来源边界：" + scope_note
    except Exception:  # noqa: BLE001
        return ""
