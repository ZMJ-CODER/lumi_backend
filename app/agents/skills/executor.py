"""技能执行器 —— LLM function calling 循环 + 参数校验 + 审计.

职责:
  - 按场景过滤可用的技能（category/permission 治理入口）
  - 把技能列表转成 function calling 工具定义交给 LLM
  - 解析并执行 LLM 发出的工具调用，结果回填对话继续循环
  - 高危技能（requires_confirmation）执行前拦截，等待用户确认
  - 每次调用写审计日志（control_logs）
"""

import hashlib
import json
import re
import time
import uuid

from loguru import logger

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.capability import ToolCapability, role_allows
from app.agents.skills.registry import SkillRegistry
from app.core.config import settings
from app.core.agent_security import redact_server_text, sanitize_server_result, wrap_untrusted_tool_output
from app.core.database import async_session_factory
from app.models.db_models import ControlLog
from app.services import client_tools
from app.services.usage import CATEGORY_CHAT, CATEGORY_SKILL


_WRITE_NAME_HINTS = (
    "write", "edit", "delete", "rename", "move", "create", "install",
    "send", "apply_patch", "kill", "rollback", "commit", "todo", "calendar",
)

# 普通聊天不是办公自动化入口。即使某个历史 Skill 的 ``scenes`` 元数据仍含
# ``chat``，也不能因此获得本机操作、写入、日程或文件管理等能力。文档检索、
# 可信的当前时间与联网查询属于“问答”范畴，保留在聊天白名单中。
_CHAT_SKILL_ALLOWLIST = {"web_search", "query_knowledge", "get_datetime"}

# M3 ReAct 是动态选择工具的路径，不能把项目开发、通用文件系统和通用 Shell
# 一并交给模型。普通办公只保留业务办公、桌面/进程控制、系统信息，以及明确
# 审核过的检索和隔离脚本能力。开发工具仍可由显式的代码 Worker / M2 计划使用。
_OFFICE_REACT_ALLOWED_CATEGORIES = {"office", "desktop", "process", "system", "mcp"}
_OFFICE_REACT_ALLOWED_SKILLS = {"python_exec", "create_office_document", "query_knowledge", "web_search"}
_OFFICE_REACT_DENIED_SKILLS = {"env"}


def tool_call_fingerprint(tool_name: str, args: dict) -> str:
    """Return a stable approval identity for one exact tool invocation."""
    payload = json.dumps(
        {"tool": str(tool_name or ""), "args": args if isinstance(args, dict) else {}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_tool_call_confirmed(
    tool_name: str,
    args: dict,
    confirmed_tool_calls: frozenset[str] | set[str] | None,
) -> bool:
    return tool_call_fingerprint(tool_name, args) in (confirmed_tool_calls or set())

# 历史插件不必一次性逐文件改元数据；这里是普通办公 ReAct 已审核能力的
# 集中语义目录。插件自身声明优先，目录只补齐空字段。后续新 Skill 应直接
# 在 Skill 类上声明 domain / intent_tags 等字段。
_OFFICE_REACT_ROUTING_METADATA: dict[str, dict] = {
    "office_doc_read": {"domain": "document", "intent_tags": ["阅读", "读取", "文档", "附件", "内容"], "preferred_over": ["document_qa"]},
    "office_doc_analyze": {"domain": "document", "intent_tags": ["分析", "解读", "文档", "表格", "附件"]},
    "office_doc_edit": {"domain": "document", "intent_tags": ["修改", "编辑", "文档", "批注", "修订"]},
    "create_office_document": {"domain": "document", "intent_tags": ["生成", "创建", "制作", "ppt", "pptx", "word", "docx", "excel", "xlsx", "演示文稿"]},
    "python_exec": {"domain": "data", "intent_tags": ["转换", "导出", "生成文件", "清洗", "合并", "拆分", "csv", "xlsx", "脚本"], "preferred_over": ["office_doc_read", "office_doc_analyze"]},
    "extract_info": {"domain": "document", "intent_tags": ["提取", "字段", "金额", "姓名", "信息"]},
    "invoice_parse": {"domain": "document", "intent_tags": ["发票", "报销", "税额", "金额"]},
    "document_qa": {"domain": "research", "intent_tags": ["文档问答", "资料", "回答", "引用"]},
    "query_knowledge": {"domain": "research", "intent_tags": ["知识库", "检索", "资料", "查询"]},
    "web_search": {"domain": "research", "intent_tags": ["联网", "搜索", "公开资料", "网页"]},
    "competitor_analysis": {"domain": "research", "intent_tags": ["竞品", "对比", "市场", "调研"]},
    "customer_service": {"domain": "research", "intent_tags": ["客服", "客诉", "回复"]},
    "daily_report": {"domain": "research", "intent_tags": ["早报", "晚报", "日报"]},
    "compose_email": {"domain": "writing", "intent_tags": ["邮件", "撰写", "草稿", "标题"]},
    "compose_official_doc": {"domain": "writing", "intent_tags": ["公文", "通知", "报告", "正式文书"]},
    "rewrite_text": {"domain": "writing", "intent_tags": ["改写", "润色", "语气"]},
    "summarize_text": {"domain": "writing", "intent_tags": ["摘要", "总结", "概括"]},
    "meeting_minutes": {"domain": "schedule", "intent_tags": ["会议纪要", "会议", "决议", "行动项"]},
    "calendar_manager": {"domain": "schedule", "intent_tags": ["日历", "日程", "会议", "预约"]},
    "todo_manager": {"domain": "schedule", "intent_tags": ["待办", "任务清单", "提醒"]},
    "send_email": {"domain": "communication", "intent_tags": ["发送邮件", "发邮件", "收件人"], "conflicts_with": ["compose_email"]},
    "open_app": {"domain": "desktop", "intent_tags": ["打开应用", "启动", "软件", "wps", "excel", "word"]},
    "open_file": {"domain": "desktop", "intent_tags": ["打开文件", "预览文件"]},
    "open_url": {"domain": "desktop", "intent_tags": ["打开网页", "网址", "链接"]},
    "ask_user": {"domain": "desktop", "intent_tags": ["询问", "确认", "选择"]},
    "ps": {"domain": "desktop", "intent_tags": ["进程", "运行中", "状态"]},
    "kill": {"domain": "desktop", "intent_tags": ["结束进程", "关闭进程"]},
    "speech_to_text": {"domain": "document", "intent_tags": ["语音", "转文字", "转写"]},
    "get_datetime": {"domain": "system", "intent_tags": ["日期", "时间", "几点"]},
    "task_memory": {"domain": "memory", "intent_tags": ["上次", "此前", "记忆"]},
    "compliance_check": {"domain": "writing", "intent_tags": ["合规", "敏感词", "审查"]},
}

_OFFICE_REACT_DOMAIN_MARKERS = {
    "document": ("文档", "文件", "附件", "表格", "csv", "xlsx", "pdf", "docx", "ppt", "pptx", "word", "演示文稿", "提取", "发票"),
    "data": ("转换", "转为", "导出", "生成文件", "清洗", "合并", "拆分", "脚本"),
    "research": ("查询", "检索", "搜索", "研究", "竞品", "资料", "知识库", "原因", "分析"),
    "writing": ("撰写", "改写", "润色", "摘要", "报告", "通知", "公文"),
    "schedule": ("日历", "日程", "待办", "会议", "提醒", "纪要"),
    "communication": ("发送", "发邮件", "收件人"),
    "desktop": ("打开", "启动", "应用", "软件", "网页", "网址", "进程"),
    "system": ("时间", "日期", "几点"),
}


def _skill_is_write(skill: Skill) -> bool:
    name = str(skill.name or "").lower()
    return bool(
        skill.write_op
        or skill.requires_confirmation
        or any(hint in name for hint in _WRITE_NAME_HINTS)
    )


def skill_runtime_unavailable(skill: Skill | None) -> tuple[str, str] | None:
    """返回当前部署下不可执行的 Skill 原因。

    规划阶段就隐藏不可用能力，执行阶段仍复核一次，避免模型生成一段脚本后
    才发现生产环境没有隔离沙箱。
    """
    if skill is not None and skill.name == "python_exec":
        try:
            from app.agents.sandbox.registry import get_sandbox

            sandbox = get_sandbox()
            if sandbox.name == "local" and not settings.AGENT_ALLOW_UNSAFE_LOCAL_SANDBOX:
                return (
                    "SANDBOX_REQUIRED",
                    "当前服务器未配置隔离脚本沙箱，不能安全执行 Python 脚本。",
                )
            available, reason = sandbox.is_available()
            if not available:
                return "SANDBOX_REQUIRED", reason or "脚本沙箱当前不可用。"
        except Exception:  # noqa: BLE001
            return "SANDBOX_REQUIRED", "脚本沙箱当前不可用。"
    return None


def get_skills_for_scene(scene: str, user_role: str = "user") -> list[Skill]:
    """按场景过滤技能（scenes 白名单；空 = 全场景）.

    渐进开放写工具：AGENT_TOOL_WRITE_ENABLED=False 时隐藏写操作技能（只读先行）。
    """
    allow_write = bool(settings.AGENT_TOOL_WRITE_ENABLED)
    return [
        s
        for s in SkillRegistry.list()
        if s.supports_scene(scene)
        and s.status != "disabled"
        and (scene != "chat" or s.name in _CHAT_SKILL_ALLOWLIST)
        and (allow_write or not _skill_is_write(s))
        and role_allows(s.permission, user_role)
    ]


def skills_to_tools(scene: str, user_role: str = "user") -> list[dict]:
    """场景内技能 → function calling 工具定义."""
    return [s.to_tool_definition() for s in get_skills_for_scene(scene, user_role)]


def _skill_capability(skill: Skill) -> ToolCapability:
    parameters = skill.parameters_schema if isinstance(skill.parameters_schema, dict) else {}
    resource_templates = (
        skill.resource_templates if isinstance(skill.resource_templates, list) else []
    )
    routing = _OFFICE_REACT_ROUTING_METADATA.get(skill.name, {})
    return ToolCapability(
        name=skill.name,
        version=skill.version,
        status=skill.status,
        schema_fingerprint=skill.schema_fingerprint,
        replacement_skill_id=skill.replacement_skill_id,
        description=skill.description,
        category=skill.category,
        domain=str(getattr(skill, "domain", "") or routing.get("domain") or skill.category),
        intent_tags=list(getattr(skill, "intent_tags", None) or routing.get("intent_tags") or []),
        conflicts_with=list(getattr(skill, "conflicts_with", None) or routing.get("conflicts_with") or []),
        preferred_over=list(getattr(skill, "preferred_over", None) or routing.get("preferred_over") or []),
        parameters=parameters,
        source="skill",
        permission=skill.permission,
        write_op=_skill_is_write(skill),
        requires_confirmation=bool(skill.requires_confirmation),
        confirmation_mode="client" if skill.environment == "client" else "server",
        idempotent=bool(skill.idempotent and not _skill_is_write(skill)),
        resource_templates=list(resource_templates),
        annotations={
            "cost_estimate": skill.cost_estimate,
            "success_rate": skill.success_rate,
        },
    )


async def get_capabilities_for_scene(
    scene: str,
    user_role: str = "user",
    user_id: str = "",
    include_nonstable: bool = False,
) -> list[ToolCapability]:
    """统一能力目录；在暴露给 Planner/Executor 前完成权限和写开关过滤。"""
    capabilities = [
        _skill_capability(s)
        for s in get_skills_for_scene(scene, user_role)
        if skill_runtime_unavailable(s) is None
        and (include_nonstable or s.status == "stable")
    ]
    # 桌面端能力不以 MCP 全局发现：后端无法从固定地址判断某个 Electron
    # 属于哪位用户。所有客户端 Skill 必须经 run_client_skill_request() 投递到
    # 当前 JWT 用户的专属队列，由其已登录桌面端领取。这样 user_id、角色和
    # 场景在服务端已完成授权，模型不能指定或切换其他人的客户端。
    if user_id:
        try:
            from app.services.mcp_bindings import get_bound_capabilities

            capabilities.extend(await get_bound_capabilities(user_id, scene, user_role))
        except Exception as exc:  # noqa: BLE001
            logger.debug("加载用户 MCP 工具绑定失败，继续使用本地 Skill: {}", exc)
    try:
        from app.services.skill_telemetry import apply_success_rate_hints

        await apply_success_rate_hints(capabilities, scene)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.agents.skills.routing import schedule_skill_semantic_index

        schedule_skill_semantic_index(capabilities)
    except Exception:  # noqa: BLE001
        pass
    return capabilities


def _routing_terms(text: str) -> set[str]:
    """Small lexical feature set for the zero-LLM tool namespace router."""
    value = (text or "").casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", value))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", value))
    terms.update(chinese[index : index + 2] for index in range(max(0, len(chinese) - 1)))
    return {term for term in terms if term}


def _preferred_domains(text: str) -> set[str]:
    lower = (text or "").casefold()
    return {
        domain
        for domain, markers in _OFFICE_REACT_DOMAIN_MARKERS.items()
        if any(marker in lower for marker in markers)
    }


async def select_capabilities_for_request(
    request: str,
    scene: str,
    user_role: str = "user",
    limit: int = 24,
    allowed_names: set[str] | None = None,
    allowed_categories: set[str] | None = None,
    denied_names: set[str] | None = None,
    user_id: str = "",
) -> list[ToolCapability]:
    """Narrow the planner's tool namespace without changing authorization.

    This is a cheap first stage only.  The selected capabilities have already
    passed scene, role, write-toggle and runtime-availability checks.
    """
    capabilities = await get_capabilities_for_scene(scene, user_role, user_id)
    if allowed_names is not None or allowed_categories is not None or denied_names:
        capabilities = [
            capability
            for capability in capabilities
            if (allowed_names is None or capability.name in allowed_names
                or (allowed_categories is not None and capability.category in allowed_categories))
            and capability.name not in (denied_names or set())
        ]
    if len(capabilities) <= max(1, limit):
        return capabilities
    request_terms = _routing_terms(request)
    lower = (request or "").casefold()
    preferred_domains = _preferred_domains(request)
    preferred: set[str] = {"query_knowledge", "web_search", "get_datetime"}
    file_markers = ("文档", "文件", "csv", "xlsx", "docx", "ppt", "pptx", "word", "excel", "pdf", "txt", "表格")
    transform_markers = (
        "转换", "转为", "转成", "导出", "生成文件", "保存为", "另存为", "批量",
        "清洗", "合并", "拆分", "格式化", "重命名", "创建", "制作",
    )
    coarse_file_script = (
        any(marker in lower for marker in file_markers)
        and any(marker in lower for marker in transform_markers)
    )
    groups = {
        ("文档", "文件", "csv", "xlsx", "docx", "ppt", "pptx", "word", "excel", "pdf", "txt", "表格", "转换", "导出", "创建", "制作"):
            {"office_doc_read", "office_doc_analyze", "office_doc_edit", "create_office_document", "python_exec", "extract_info"},
        ("邮件", "email", "发送"):
            {"compose_email", "send_email"},
        ("日历", "会议", "日程"):
            {"calendar_manager", "meeting_minutes"},
        ("待办", "todo", "任务清单"):
            {"todo_manager"},
        ("代码", "脚本", "python", "bug", "项目"):
            {"python_exec", "shell_exec", "read_file", "write_file"},
    }
    for markers, names in groups.items():
        if any(marker in lower for marker in markers):
            preferred.update(names)

    try:
        from app.agents.skills.routing import semantic_scores

        semantic = await semantic_scores(request, capabilities)
    except Exception:  # noqa: BLE001
        semantic = {}
    ranked = []
    for index, capability in enumerate(capabilities):
        searchable = " ".join([
            capability.name,
            capability.description,
            capability.domain,
            " ".join(capability.intent_tags),
        ])
        overlap = len(request_terms & _routing_terms(searchable))
        tag_overlap = len(request_terms & _routing_terms(" ".join(capability.intent_tags)))
        score = overlap * 10 + tag_overlap * 18 + (30 if capability.name in preferred else 0)
        score += semantic.get(capability.name, 0.0) * float(settings.SKILL_ROUTING_SEMANTIC_WEIGHT)
        metadata = capability.annotations if isinstance(capability.annotations, dict) else {}
        success_rate = metadata.get("success_rate")
        cost_estimate = metadata.get("cost_estimate")
        if isinstance(success_rate, (int, float)):
            score += max(0.0, min(1.0, float(success_rate))) * float(settings.SKILL_ROUTING_RELIABILITY_WEIGHT)
        if isinstance(cost_estimate, (int, float)):
            score -= max(0.0, float(cost_estimate)) * float(settings.SKILL_ROUTING_COST_WEIGHT)
        if capability.domain in preferred_domains:
            score += 42
        if coarse_file_script:
            # 转换/导出需要的是一次产生真实产物的粗粒度能力。读取、写入等细工具
            # 仍保留给文档问答和编辑，但不应在这种请求里压过脚本执行器。
            if capability.name in {"python_exec", "create_office_document"}:
                score += 100
            elif capability.name in {"read_file", "write_file", "office_doc_read", "office_doc_analyze"}:
                score -= 35
        ranked.append((score, -index, capability))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected = []
    selected_names: set[str] = set()
    for _, _, capability in ranked:
        if len(selected) >= max(1, limit):
            break
        # 显式冲突的工具不同时给模型。只有当上位工具不在候选中时才保留它。
        if any(name in selected_names for name in capability.conflicts_with):
            continue
        if any(capability.name in item.conflicts_with for item in selected):
            continue
        # preferred_over 用于“同一目标下的替代关系”：例如明确的文件转换
        # 选择 python_exec 后，不再额外暴露逐文档读取工具，避免模型退回口述。
        if any(capability.name in item.preferred_over for item in selected):
            continue
        selected.append(capability)
        selected_names.add(capability.name)
    # ``ranked`` 已以 score 和原始注册顺序作为稳定 tie-breaker 排序；保留该顺序
    # 才能让优先关系真实影响模型看到的工具排列。
    return selected


async def get_office_react_capabilities_for_request(
    request: str,
    user_role: str = "user",
    limit: int = 8,
    excluded_names: set[str] | None = None,
    user_id: str = "",
) -> list[ToolCapability]:
    """Return the ordinary-office tool namespace for the M3 ReAct runner.

    This is a capability boundary rather than a prompt hint: project/devtools,
    generic filesystem and unrestricted shell tools are absent before the model
    receives its function schemas.
    """
    capabilities = await select_capabilities_for_request(
        request,
        "office",
        user_role,
        limit,
        allowed_names=_OFFICE_REACT_ALLOWED_SKILLS,
        allowed_categories=_OFFICE_REACT_ALLOWED_CATEGORIES,
        denied_names=_OFFICE_REACT_DENIED_SKILLS,
        user_id=user_id,
    )
    excluded_names = excluded_names or set()
    return [item for item in capabilities if item.name not in excluded_names]


async def get_tools_for_scene(scene: str, user_role: str = "user", user_id: str = "") -> list[dict]:
    """统一工具目录：本地 Skill（含 system）+ 所有已连接的 MCP 工具."""
    return [c.to_tool_definition() for c in await get_capabilities_for_scene(scene, user_role, user_id)]


async def get_tool_capability(name: str, scene: str, user_role: str = "user", user_id: str = "") -> ToolCapability | None:
    for capability in await get_capabilities_for_scene(scene, user_role, user_id):
        if capability.name == name:
            return capability
    return None


def _parse_mcp_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    if len(parts) != 3 or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]


def _parse_arguments(raw) -> dict:
    """解析 LLM 传参（可能是 JSON 字符串或已解析对象）."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return {}


_EXPLICIT_DELETE_RE = re.compile(r"(?:删除|删掉|删去|移除|清理|扔进回收站)")


def is_explicit_user_delete_request(user_message: str, skill_name: str, args: dict) -> bool:
    """Return whether this *current user message* authorizes one file deletion.

    This deliberately does not inspect tool output, document text, memories or
    a planner-produced instruction.  Those are all untrusted for destructive
    actions.  Recursive directory deletes always require a local confirmation.
    """
    if skill_name != "delete_file" or bool(args.get("recursive")):
        return False
    message = str(user_message or "").strip()
    if not message or not _EXPLICIT_DELETE_RE.search(message):
        return False
    target = str(args.get("path") or "").strip().replace("\\", "/")
    filename = target.rsplit("/", 1)[-1].casefold()
    normalized = message.replace("\\", "/").casefold()
    # A named target is the strongest signal.  Pronouns are allowed only for a
    # single non-recursive file because the user intentionally delegated that
    # exact current-context action, not a directory cleanup.
    return bool(filename and filename in normalized) or any(
        marker in normalized for marker in ("这个文件", "该文件", "刚才的文件", "上述文件", "此文件")
    )


def _has_json_ref(value) -> bool:
    if isinstance(value, dict):
        return "$ref" in value or any(_has_json_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_json_ref(v) for v in value)
    return False


def _validate_mcp_arguments(schema: dict, args: dict) -> str | None:
    """校验不可信 MCP schema/参数，拒绝超大或带外部引用的调用。"""
    try:
        encoded = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "MCP 参数无法序列化"
    if len(encoded) > 64 * 1024:
        return "MCP 参数过大"
    if not isinstance(schema, dict) or _has_json_ref(schema):
        return "MCP 工具参数定义不安全或无效"
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator(schema).validate(args)
    except Exception as exc:  # ValidationError / SchemaError 都应拒绝
        return f"MCP 参数不符合工具定义: {str(exc)[:300]}"
    return None


async def execute_tool_call(
    tool_call: dict,
    user_id: str,
    scene: str = "chat",
    conversation_id: str = "",
    on_notify=None,
    user_role: str = "user",
    user_message: str = "",
    llm_api_key: str | None = None,
    llm_config: dict | None = None,
    confirmed_tools: frozenset[str] | set[str] | None = None,
    confirmed_tool_calls: frozenset[str] | set[str] | None = None,
    on_output=None,
) -> SkillResult:
    """执行一次技能调用：校验 → 高危拦截 → 执行 → 审计."""
    fn = tool_call.get("function") or {}
    name = str(fn.get("name") or "")
    args = _parse_arguments(fn.get("arguments"))
    # Reserved policy fields can never originate from a model tool call.
    args.pop("_lumi_execution_policy", None)
    capability = await get_tool_capability(name, scene, user_role, user_id)
    if capability is None:
        registered = SkillRegistry.get(name)
        unavailable = skill_runtime_unavailable(registered) if registered is not None else None
        if unavailable:
            code, error = unavailable
            return SkillResult(
                success=False,
                error=error,
                error_code=code,
                retryable=False,
                metadata={"skill": name, "runtime_available": False},
            )
        code = "FORBIDDEN" if registered is not None or _parse_mcp_name(name) else "SKILL_NOT_FOUND"
        return SkillResult(
            success=False,
            error=f"工具不存在、当前场景不可用或权限不足: {name}",
            error_code=code,
            retryable=False,
            metadata={"tool": name, "scene": scene, "role": user_role},
        )

    mcp_target = _parse_mcp_name(name)
    if mcp_target:
        from app.agents.mcp.manager import call_tool
        from app.services.mcp_bindings import (
            acquire_call_quota,
            register_active_binding_call,
            release_call_quota,
            unregister_active_binding_call,
        )

        server_name, tool_name = mcp_target
        validation_error = _validate_mcp_arguments(capability.parameters, args)
        if validation_error:
            return SkillResult(
                success=False,
                error=validation_error,
                error_code="INVALID_ARGS",
                retryable=False,
                metadata={"server": server_name, "tool": tool_name},
            )
        if (
            capability.requires_confirmation
            and capability.confirmation_mode != "client"
            and not is_tool_call_confirmed(name, args, confirmed_tool_calls)
        ):
            return SkillResult(
                success=False,
                error="该 MCP 操作需要用户确认",
                error_code="NEEDS_CONFIRMATION",
                retryable=False,
                metadata={
                    "server": server_name,
                    "tool": tool_name,
                    "approval_fingerprint": tool_call_fingerprint(name, args),
                },
            )
        binding_id = str((capability.annotations or {}).get("binding_id") or "")
        quota_acquired = False
        if binding_id:
            quota_acquired, quota_reason = await acquire_call_quota(
                binding_id, user_id,
                int((capability.annotations or {}).get("daily_call_limit") or 1),
                int((capability.annotations or {}).get("concurrency_limit") or 1),
            )
            if not quota_acquired:
                return SkillResult(
                    success=False,
                    error="外部 MCP 调用配额已用尽或配额服务暂不可用",
                    error_code=quota_reason or "MCP_QUOTA_EXCEEDED",
                    retryable=quota_reason in {"CONCURRENCY_LIMIT", "QUOTA_UNAVAILABLE"},
                    metadata={"server": server_name, "tool": tool_name, "binding_id": binding_id},
                )
        started_at = time.perf_counter()
        active_task_id = conversation_id or ""
        if quota_acquired:
            register_active_binding_call(binding_id, active_task_id)
        try:
            raw = await call_tool(
                server_name,
                tool_name,
                args,
                task_id=conversation_id or None,
                on_progress=on_notify,
            )
        finally:
            if quota_acquired:
                unregister_active_binding_call(binding_id, active_task_id)
                await release_call_quota(binding_id, user_id)
        if raw is None:
            return SkillResult(
                success=False,
                error=f"MCP 工具不可用: {server_name}/{tool_name}",
                error_code="MCP_UNAVAILABLE",
                retryable=True,
                metadata={"server": server_name, "tool": tool_name},
            )
        result = sanitize_server_result(SkillResult(
            success=bool(raw.get("success")) and not bool(raw.get("is_error")),
            output=str(raw.get("content") or ""),
            error=(str(raw.get("content") or "MCP 工具执行失败") if raw.get("is_error") else None),
            error_code=("MCP_EXEC_ERROR" if raw.get("is_error") else None),
            retryable=False,
            metadata={
                "server": server_name,
                "tool": tool_name,
                **(raw.get("metadata") or {}),
            },
        ))
        await _record_skill_telemetry(
            capability, scene, result, int((time.perf_counter() - started_at) * 1000)
        )
        await _record_skill_log(user_id, capability, args, result)
        return result

    skill = SkillRegistry.get(name)
    if not skill:
        return SkillResult(
            success=False,
            error=f"技能不存在: {name}",
            error_code="SKILL_NOT_FOUND",
            retryable=False,
            metadata={"skill": name},
        )
    unavailable = skill_runtime_unavailable(skill)
    if unavailable:
        code, error = unavailable
        return SkillResult(success=False, error=error, error_code=code, retryable=False)
    if not role_allows(skill.permission, user_role):
        return SkillResult(
            success=False,
            error=f"技能 {name} 需要 {skill.permission} 权限",
            error_code="FORBIDDEN",
            retryable=False,
            metadata={"skill": name, "required": skill.permission, "actual": user_role},
        )

    explicit_user_delete = is_explicit_user_delete_request(user_message, name, args)
    # 高危操作：server/sandbox 技能执行前必须确认；用户当前指令明确要求
    # 删除同一目标时由已有窄范围策略放行。client 技能仍由用户端确认。
    # client 技能由用户端弹窗确认（执行体内部处理），不在此拦截
    if (
        skill.requires_confirmation
        and skill.environment != "client"
        and not explicit_user_delete
        and not is_tool_call_confirmed(name, args, confirmed_tool_calls)
    ):
        result = SkillResult(
            success=False,
            error="该操作属于高危行为，需要用户确认后才能执行",
            error_code="NEEDS_CONFIRMATION",
            retryable=False,
            metadata={
                "skill": name,
                "params": args,
                "approval_fingerprint": tool_call_fingerprint(name, args),
            },
        )
        await _record_skill_log(user_id, skill, args, result)
        return result

    execution_policy = (
        {"explicit_user_delete": True}
        if explicit_user_delete
        else None
    )
    context = SkillContext(
        user_id=user_id,
        scene=scene,
        conversation_id=conversation_id,
        job_id=conversation_id,
        llm_api_key=llm_api_key,
        llm_config=llm_config,
        on_notify=on_notify,
        on_output=on_output,
        execution_policy=execution_policy,
    )
    # All registered Skills now pass through the MCP gateway.  The gateway
    # chooses Electron MCP for client capabilities and an in-process adapter
    # for backend/sandbox capabilities, preserving one timeout/result path.
    from app.agents.mcp.manager import call_skill

    started_at = time.perf_counter()
    raw = await call_skill(
        skill,
        args,
        context=context,
        task_id=conversation_id or None,
        on_progress=on_notify,
        execution_policy=execution_policy,
    )
    result = SkillResult(
        success=bool(raw.get("success")) and not bool(raw.get("is_error")),
        output=str(raw.get("content") or "") if not raw.get("is_error") else "",
        error=str(raw.get("content") or "技能执行失败") if raw.get("is_error") else None,
        error_code=raw.get("error_code") or ("EXEC_ERROR" if raw.get("is_error") else None),
        retryable=bool(raw.get("retryable", False)),
        metadata=raw.get("metadata") or {},
    )
    # server/sandbox results may contain stack traces, environment variables or
    # absolute paths; client paths are user-device data and remain untouched.
    if skill.environment in {"server", "sandbox"}:
        result = sanitize_server_result(result)
    await _record_skill_telemetry(
        capability, scene, result, int((time.perf_counter() - started_at) * 1000)
    )
    await _record_skill_log(user_id, skill, args, result)
    return result


async def _record_skill_telemetry(
    capability: ToolCapability,
    scene: str,
    result: SkillResult,
    duration_ms: int,
) -> None:
    try:
        from app.core.observability import inc_skill_call
        from app.services.skill_telemetry import record_skill_outcome

        inc_skill_call(capability.name, result.success)
        await record_skill_outcome(
            capability, scene,
            success=result.success,
            error_code=result.error_code,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001
        return


async def _record_skill_log(
    user_id: str,
    skill: Skill | ToolCapability,
    params: dict,
    result: SkillResult,
) -> None:
    """技能调用审计：control_logs 表（失败不阻塞主流程）."""
    try:
        uid = uuid.UUID(str(user_id)) if user_id else None
        if uid is None:
            return
        async with async_session_factory() as session:
            session.add(
                ControlLog(
                    user_id=uid,
                    action=f"skill:{skill.name}",
                    target=json.dumps(params, ensure_ascii=False)[:500],
                    success=result.success,
                    detail=json.dumps(
                        {
                            "error_code": result.error_code,
                            "error": result.error,
                            "output": result.output[:500],
                        },
                        ensure_ascii=False,
                    )[:2000],
                )
            )
            await session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("技能审计日志写入失败: {}", exc)


async def run_client_skill_request(
    user_id: str,
    skill_name: str,
    params: dict,
    requires_confirmation: bool = False,
    timeout: float | None = None,
    ttl: int | None = None,
) -> SkillResult:
    """客户端技能通用执行：创建待执行请求 → 用户端轮询执行 → 等待结果（超时取消）.

    供 client 环境技能（本地文件/项目操作）复用；key 不经过服务端。
    timeout：覆盖默认客户端工具等待超时（如依赖安装可能超过 120s）。
    """
    if not user_id:
        return SkillResult(
            success=False,
            error="该技能需要登录后使用",
            error_code="INVALID_ARGS",
            retryable=False,
        )
    req = await client_tools.create_client_tool_request(
        user_id, skill_name, params, requires_confirmation, ttl=ttl
    )
    if not req:
        return SkillResult(
            success=False,
            error="该技能需要登录后使用",
            error_code="INVALID_ARGS",
            retryable=False,
        )
    t0 = time.time()
    result = await client_tools.await_result(user_id, req["request_id"], timeout=timeout)
    logger.debug(
        "[ClientSkill] {} 往返 {:.0f}ms | success={}",
        skill_name,
        (time.time() - t0) * 1000,
        bool(result and result.get("success")),
    )
    if result is None:
        return SkillResult(
            success=False,
            error="等待用户响应超时，操作已取消",
            error_code="TIMEOUT",
            retryable=False,
        )
    if result.get("success"):
        return SkillResult(
            success=True,
            output=str(result.get("output") or ""),
            metadata=result.get("metadata") or {},
        )
    return SkillResult(
        success=False,
        error=str(result.get("error") or "客户端执行失败"),
        error_code=str((result.get("metadata") or {}).get("error_code") or "EXEC_ERROR"),
        retryable=False,
        metadata=result.get("metadata") or {},
    )


async def _run_skill_loop_legacy(
    llm,
    user_id: str,
    messages: list[dict],
    scene: str = "chat",
    conversation_id: str = "",
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_config: dict | None = None,
    on_text=None,
    on_progress=None,
    user_role: str = "user",
    user_message: str = "",
) -> tuple[str, list[dict], list[dict]]:
    """兼容工具循环（仅供非 LangChain mock 与图运行故障后的降级）.

    流程：LLM function calling 决定技能 → 执行 → 结果回填 → 再调 LLM，
    直到 LLM 不再请求技能（输出最终回复）或达到最大轮数。

    Args:
        llm: LLMClient 实例
        messages: 当前对话消息列表（最后一个为用户消息）
        llm_api_key: BYOK 用户本次请求临时携带的 API key（用完即弃，不落库）
        on_text: 可选回调，每轮 assistant 文本产出时调用（流式输出用）
        on_progress: 可选回调，工具执行过程 notify（如"正在启动软件…"）独立通道，
            用于前端"思维链/执行过程"展示，避免混入最终回复正文

    Returns:
        (final_text, records, citations)
        - final_text: 最终回复文本
        - records: 技能调用记录 [{skill, success, error_code}]
        - citations: 技能返回的引用列表（web_search / query_knowledge）
    """
    # 能力目录已在场景、角色与运行时可用性维度过滤；办公场景可见 office/system
    # Skill，普通聊天仅保留问答白名单。MCP 不在此环节作全局发现。
    tools = await get_tools_for_scene(scene, user_role, user_id)
    if not tools:
        return "", [], []
    max_rounds = settings.AGENT_SKILLS_MAX_ROUNDS
    records: list[dict] = []
    citations: list[dict] = []
    final_text = ""
    messages = list(messages)

    def emit_progress(item) -> None:
        if not on_progress:
            return
        value = item if isinstance(item, (str, dict)) else str(item)
        on_progress(value)

    for _ in range(max_rounds):
        content, tool_calls = await llm.chat_with_tools(
            messages,
            tools,
            scene=scene,
            base_url=llm_base_url,
            model=llm_model,
            usage_user_id=user_id,
            usage_category=CATEGORY_SKILL,
            api_key=llm_api_key,
            llm_config=llm_config,
        )
        if content:
            final_text = content
            if on_text:
                on_text(content)
        if not tool_calls:
            break

        messages.append(
            {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
        )
        for tc in tool_calls:
            skill_name = str(tc.get("function", {}).get("name") or "")
            if on_progress:
                emit_progress(
                    {
                        "type": "step",
                        "id": str(tc.get("id") or f"tool-{len(records) + 1}"),
                        "title": skill_name or "执行工具",
                        "status": "running",
                        "tool": skill_name,
                    }
                )
            result = await execute_tool_call(
                tc,
                user_id,
                scene,
                conversation_id,
                on_notify=emit_progress,
                user_role=user_role,
                user_message=user_message,
                llm_config=llm_config,
            )
            records.append(
                {
                    "skill": tc.get("function", {}).get("name"),
                    "success": result.success,
                    "error_code": result.error_code,
                    "error": result.error,
                }
            )
            if result.metadata.get("citations"):
                citations.extend(result.metadata["citations"])
            if on_progress:
                emit_progress(
                    {
                        "type": "step",
                        "id": str(tc.get("id") or f"tool-{len(records)}"),
                        "title": skill_name or "执行工具",
                        "status": "completed" if result.success else "failed",
                        "tool": skill_name,
                        "output": result.output[:1000] if result.success else "",
                        "error": result.error if not result.success else None,
                    }
                )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(tc.get("id") or ""),
                    "content": wrap_untrusted_tool_output(result.output or (result.error or "")),
                }
            )
    else:
        # 达到最大轮数：强制让模型基于现有信息收尾，避免无限循环
        messages.append(
            {"role": "user", "content": "技能调用次数已达上限，请基于现有信息直接给出最终回答。"}
        )
        try:
            final_text = await llm.chat(
                messages,
                scene=scene,
                usage_user_id=user_id,
                usage_category=CATEGORY_CHAT,
                api_key=llm_api_key,
                llm_config=llm_config,
            )
            if on_text:
                on_text(final_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("技能循环收尾回复失败: {}", exc)

    # 兜底：技能循环结束必须退出"思维链"并给出最终答复。
    # 若模型最后一轮只调了工具、没产出正文（或收尾回复失败），
    # 根据执行记录生成"已完成 + 失败步骤及原因"的总结，保证前端一定有结果。
    if not (final_text or "").strip() and records:
        done_names = [r["skill"] for r in records if r.get("success")]
        failed_records = [r for r in records if not r.get("success")]
        lines: list[str] = []
        if done_names:
            lines.append(f"已完成：{'、'.join(done_names)}")
        for r in failed_records:
            reason = str(r.get("error") or "").strip() or str(r.get("error_code") or "执行失败")
            lines.append(f"未完成：{r.get('skill')}（原因：{reason}）")
        if not lines:
            lines.append("任务执行完成")
        final_text = "任务执行结果：\n" + "\n".join(lines)
        if on_text:
            on_text(final_text)

    return redact_server_text(final_text), records, citations


async def run_skill_loop(
    llm,
    user_id: str,
    messages: list[dict],
    scene: str = "chat",
    conversation_id: str = "",
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_config: dict | None = None,
    on_text=None,
    on_progress=None,
    user_role: str = "user",
) -> tuple[str, list[dict], list[dict]]:
    """受控技能循环的稳定入口。

    所有生产场景统一走 LangGraph ``model -> before_tool -> ToolNode -> after_tool
    -> model``；图集中管理串行工具调用、进度事件、工具错误回填与调用上限。
    旧循环仅保留给非 Lumi mock/第三方适配对象，以及 LangGraph 本身不可用时的
    最后兼容降级，不能作为 office 的常规执行路径。
    """
    from app.core.llm import LLMClient

    # 办公 DAG 的原子节点仍由 LangGraphNodeRunner 编排；这里覆盖的是所有
    # "模型自主调用多工具" 的循环。无论场景，实际能力都继续由 scene/role
    # 白名单和 execute_tool_call 的审计、资源与用户隔离裁决。
    use_graph = llm is None or isinstance(llm, LLMClient)
    if use_graph:
        try:
            from app.agents.langchain.chat_graph import LangGraphChatRunner

            final_text, records, citations = await LangGraphChatRunner(
                user_id=user_id,
                scene=scene,
                conversation_id=conversation_id,
                api_key=llm_api_key,
                model=llm_model,
                base_url=llm_base_url,
                llm_config=llm_config,
                max_rounds=settings.AGENT_SKILLS_MAX_ROUNDS,
                on_progress=on_progress,
                user_role=user_role,
            ).run(messages)
            if final_text and on_text:
                on_text(final_text)
            return final_text, records, citations
        except Exception as exc:  # noqa: BLE001
            # 图适配层的供应商兼容性故障不应让请求整体失败；真正的工具权限和
            # 执行边界仍由兼容循环调用同一个 execute_tool_call 负责。
            if scene == "office" and llm_config:
                raise
            logger.warning("LangGraph 工具图失败，回退兼容执行器: {}", str(exc)[:300])

    return await _run_skill_loop_legacy(
        llm or LLMClient(),
        user_id,
        messages,
        scene=scene,
        conversation_id=conversation_id,
        llm_api_key=llm_api_key,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_config=llm_config,
        on_text=on_text,
        on_progress=on_progress,
        user_role=user_role,
        user_message=str(messages[-1].get("content") or "") if messages else "",
    )
