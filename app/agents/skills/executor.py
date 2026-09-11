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
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from loguru import logger

from app.agents.skills.base import Tool, SkillContext, SkillResult
from app.agents.skills.capability import ToolCapability, role_allows
from app.agents.skills.registry import ToolRegistry
from app.core.config import settings
from app.core.agent_security import redact_server_text, sanitize_server_result, wrap_untrusted_tool_output
from app.core.resource_policy import ResourcePolicyError, validate_client_path, validate_command
from app.core.database import async_session_factory
from app.models.db_models import ControlLog
from app.services import client_tools
from app.services.tool_output_projection import project_tool_output
from app.services.tool_output_pipeline import clean_assistant_text, normalize_execution_envelope
from lumi_contracts import ToolRequest
from app.services.usage import CATEGORY_CHAT, CATEGORY_SKILL


_WRITE_NAME_HINTS = (
    "write", "edit", "delete", "rename", "move", "create", "install",
    "send", "apply_patch", "kill", "rollback", "commit", "todo", "calendar",
)

# 普通聊天不是办公自动化入口。即使某个历史 Skill 的 ``scenes`` 元数据仍含
# ``chat``，也不能因此获得本机操作、写入、日程或文件管理等能力。文档检索、
# 可信的当前时间与联网查询属于“问答”范畴，保留在聊天白名单中。
_CHAT_SKILL_ALLOWLIST = {
    "web_search", "web_fetch", "DateTime", "Calculator", "AskUserQuestion",
}

_PROJECT_SCOPED_SKILLS = {
    "get_project_context", "run_static_check", "run_in_sandbox", "check_new_dependencies",
    "rollback_dependency_manifests",
}

# M3 ReAct 是动态选择工具的路径，不能把项目开发、通用文件系统和通用 Shell
# 一并交给模型。普通办公只保留业务办公、桌面/进程控制、系统信息，以及明确
# 审核过的检索和隔离脚本能力。开发工具仍可由显式的代码 Worker / M2 计划使用。
_OFFICE_REACT_ALLOWED_CATEGORIES = {
    "office", "desktop", "process", "system", "mcp", "filesystem", "development",
    "shell", "network", "interaction", "productivity", "orchestration",
}
_OFFICE_REACT_ALLOWED_SKILLS = {
    "web_fetch", "web_search", "Calculator", "DateTime", "SystemInfo",
    "OpenFile", "OpenApp", "OpenUrl", "ProcessList", "ProcessSignal",
    "python_exec", "create_office_document", "query_knowledge",
    "inspect_document_set", "read_document",
    "Read", "Edit", "Write", "Glob", "Grep", "FileStat", "Rename", "Delete",
    "Bash", "BashOutput", "KillShell",
    "AskUserQuestion", "TodoWrite", "Task", "Skill", "SlashCommand",
    "EnterPlanMode", "ExitPlanMode", "NotebookEdit",
    # Autonomous implementation/diagnostic tasks need a bounded execution
    # bridge for tests, builds and dependency checks.  These remain subject
    # to the normal client/sandbox permission and confirmation gates.
    "run_in_sandbox", "check_new_dependencies", "install_new_dependencies",
    "rollback_dependency_manifests", "run_static_check",
}
_OFFICE_REACT_DENIED_SKILLS = {"env"}
_bootstrap_expiry_alerts: set[tuple[str, str]] = set()


class ToolExecutionCoordinationUnavailable(RuntimeError):
    """节点工具调用无法取得可靠互斥租约。"""


@asynccontextmanager
async def _claim_tool_execution(tool_name: str, execution_scope: str):
    """在一个 DAG Job 内串行化同名工具的真实执行阶段。

    规划、参数提取和工具选择不应占用锁；只有已经通过权限与确认检查的
    ``call_skill`` / MCP 请求进入临界区。资源协调器同时使用进程内条件变量
    与 Redis 租约，因此不同节点可并发调用不同工具，而恢复/重试不会让同一
    Job 的两个节点同时驱动同一个工具实例。
    """
    if not execution_scope:
        yield
        return

    from app.agents.resource_coordination import (
        ResourceClaim,
        WriteResourceCoordinationUnavailable,
        resource_coordinator,
    )
    claim = ResourceClaim(
        key=f"global:tool-execution:job:{execution_scope}:tool:{tool_name}",
        mode="write",
    )
    if not await resource_coordinator.write_coordination_available([claim]):
        raise ToolExecutionCoordinationUnavailable("工具执行互斥协调服务不可用")
    try:
        async with resource_coordinator.claim([claim]):
            yield
    except WriteResourceCoordinationUnavailable as exc:
        raise ToolExecutionCoordinationUnavailable("工具执行互斥协调服务不可用") from exc


def _tool_coordination_failure(tool_name: str) -> SkillResult:
    return SkillResult(
        success=False,
        error="工具执行互斥协调服务暂不可用，已阻止并发执行",
        error_code="TOOL_EXECUTION_COORDINATION_UNAVAILABLE",
        retryable=True,
        metadata={"tool": tool_name},
    )


def tool_call_fingerprint(tool_name: str, args: dict, upstream_sha256: str = "") -> str:
    """Return a stable approval identity for tool arguments and upstream data."""
    payload = json.dumps(
        {
            "tool": str(tool_name or ""),
            "args": args if isinstance(args, dict) else {},
            "upstream_sha256": str(upstream_sha256 or ""),
        },
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
    upstream_sha256: str = "",
) -> bool:
    return tool_call_fingerprint(tool_name, args, upstream_sha256) in (confirmed_tool_calls or set())

# 历史插件不必一次性逐文件改元数据；这里是普通办公 ReAct 已审核能力的
# 集中语义目录。插件自身声明优先，目录只补齐空字段。后续新 Skill 应直接
# 在 Skill 类上声明 domain / intent_tags 等字段。
_OFFICE_REACT_ROUTING_METADATA: dict[str, dict] = {
    "inspect_document_set": {"domain": "document", "intent_tags": ["多文档", "盘点", "定位", "找文件", "条款", "附件"], "preferred_over": ["office_doc_read"], "handoff_to": ["read_document"], "use_when": ["当前任务有两份及以上已授权附件，先定位目标", "用户问哪份文件包含某事实/条款"], "do_not_use_when": ["目标文档已唯一明确时，用 read_document", "需要正文内容时，盘点后用 read_document"], "selection_examples": ["“哪份附件写了付款期限？” → inspect_document_set"]},
    "read_document": {"domain": "document", "intent_tags": ["读取", "文档", "附件", "内容", "条款"], "preferred_over": ["office_doc_read"], "use_when": ["已有唯一、已授权的 doc_id，需要读取正文"], "do_not_use_when": ["多文档且目标未知时，先用 inspect_document_set", "只需检索长期知识库时，用 query_knowledge"], "selection_examples": ["“读取刚定位到的合同” → read_document"]},
    "office_doc_read": {"domain": "document", "intent_tags": ["阅读", "读取", "文档", "附件", "内容"], "preferred_over": ["document_qa"]},
    "office_doc_analyze": {"domain": "document", "intent_tags": ["分析", "解读", "文档", "表格", "附件"]},
    "office_doc_edit": {"domain": "document", "intent_tags": ["修改", "编辑", "文档", "批注", "修订"]},
    "create_office_document": {"domain": "document", "intent_tags": ["生成", "创建", "制作", "ppt", "pptx", "word", "docx", "excel", "xlsx", "演示文稿"]},
    "python_exec": {"domain": "data", "intent_tags": ["转换", "导出", "生成文件", "清洗", "合并", "拆分", "csv", "xlsx", "脚本"], "preferred_over": ["office_doc_read", "office_doc_analyze"]},
    "extract_info": {"domain": "document", "intent_tags": ["提取", "字段", "金额", "姓名", "信息"]},
    "invoice_parse": {"domain": "document", "intent_tags": ["发票", "报销", "税额", "金额"]},
    "document_qa": {"domain": "research", "intent_tags": ["文档问答", "资料", "回答", "引用"]},
    "query_knowledge": {"domain": "research", "intent_tags": ["知识库", "检索", "资料", "查询"], "use_when": ["答案在用户已入库的个人/公共知识库中"], "do_not_use_when": ["用户要求公开网页来源或新闻时，用 web_search", "当前办公附件有明确 doc_id 时，用 read_document"], "selection_examples": ["“根据我的知识库说明报销规则” → query_knowledge"]},
    "web_search": {"domain": "research", "intent_tags": ["联网", "搜索", "公开资料", "网页"], "use_when": ["明确要求联网、网页来源，或核实公开新闻/政策/外部事实"], "do_not_use_when": ["用户私有任务、对话、附件或知识库内容", "当前时间用 get_datetime；未来天气/行情用垂直数据 Skill", "通用常识且未要求来源时直接回答"], "selection_examples": ["“联网搜索本周 AI 政策并给来源” → web_search"]},
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
    "OpenApp": {"domain": "desktop", "intent_tags": ["打开应用", "启动", "软件", "wps", "excel", "word"]},
    "OpenFile": {"domain": "desktop", "intent_tags": ["打开文件", "预览文件"]},
    "OpenUrl": {"domain": "desktop", "intent_tags": ["打开网页", "网址", "链接"]},
    "AskUserQuestion": {"domain": "interaction", "intent_tags": ["询问", "确认", "选择"]},
    "ProcessList": {"domain": "system", "intent_tags": ["进程", "运行中", "状态"]},
    "ProcessSignal": {"domain": "system", "intent_tags": ["结束进程", "关闭进程"]},
    "speech_to_text": {"domain": "document", "intent_tags": ["语音", "转文字", "转写"]},
    "DateTime": {"domain": "system", "intent_tags": ["日期", "时间", "几点"], "use_when": ["询问当前日期、时间、星期"], "do_not_use_when": ["用户自己的今日待办或日程", "天气、汇率、行情等其他实时数据"], "selection_examples": ["“现在几点？” → DateTime"]},
    "Calculator": {"domain": "system", "intent_tags": ["计算", "算一下", "算术", "加减乘除", "百分比", "表达式"], "use_when": ["用户要求精确算术、百分比或括号表达式计算"], "do_not_use_when": ["仅需解释数学概念", "需要统计上传数据时先读取数据"], "selection_examples": ["“帮我算一下 (12873×47-912)÷13” → Calculator"]},
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


@dataclass(frozen=True)
class CapabilitySelection:
    """A safe trace of the candidate-routing decision.

    The trace intentionally contains capability metadata and numeric scores,
    never the user request, prompts, tool arguments, or tool results.
    """

    capabilities: list[ToolCapability]
    candidates: list[dict]
    scene: str
    top_score: float = 0.0
    second_score: float = 0.0
    score_margin: float = 0.0
    ambiguous: bool = False
    low_confidence: bool = False
    reason: str = "ranked"
    routing_mode: str = "lexical_fallback"

    def to_metadata(self, *, selection_round: int = 1) -> dict:
        return {
            "scene": self.scene,
            "selection_round": max(1, int(selection_round)),
            "injected_candidates": self.candidates,
            "candidate_count": len(self.candidates),
            "top_score": round(float(self.top_score), 3),
            "second_score": round(float(self.second_score), 3),
            "score_margin": round(float(self.score_margin), 3),
            "ambiguous": bool(self.ambiguous),
            "low_confidence": bool(self.low_confidence),
            "reason": self.reason,
            "routing_mode": self.routing_mode,
        }


def _skill_is_write(skill: Tool, params: dict | None = None) -> bool:
    name = str(skill.name or "").lower()
    # Mixed-action skills may be read-only for the current invocation (or when
    # exposing their namespace to the planner).  Their override takes
    # precedence over the coarse class-level write_op flag.
    if params is not None and hasattr(skill, "is_write_operation"):
        return bool(skill.is_write_operation(params))
    return bool(
        skill.write_op
        or skill.requires_confirmation
        or any(hint in name for hint in _WRITE_NAME_HINTS)
    )


def skill_runtime_unavailable(skill: Tool | None) -> tuple[str, str] | None:
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


def get_skills_for_scene(
    scene: str,
    user_role: str = "user",
    *,
    include_bootstrap: bool = False,
    include_internal: bool = False,
) -> list[Tool]:
    """按场景过滤可直接调用的原子工具（历史函数名暂保留）。

    渐进开放写工具：AGENT_TOOL_WRITE_ENABLED=False 时隐藏写操作技能（只读先行）。
    """
    allow_write = bool(settings.AGENT_TOOL_WRITE_ENABLED)
    tools = [
        s
        for s in ToolRegistry.list(include_internal=include_internal)
        if s.supports_scene(scene)
        and s.status != "disabled"
        and (settings.WEB_SEARCH_TOOL_ENABLED or s.name != "web_search")
        and (
            scene != "chat"
            or s.name in _CHAT_SKILL_ALLOWLIST
            # Bootstrap never exposes a tool by itself: the candidate selector
            # still requires its declared intent and unexpired date below.
            or (
                include_bootstrap
                and bool(s.bootstrap_intents)
                and _bootstrap_is_active(s.bootstrap_until)
            )
        )
        and (allow_write or not _skill_is_write(s))
        and role_allows(s.permission, user_role)
    ]
    if include_internal:
        # Skill-owned execution implementations are kept out of discovery and
        # Function Calling, but remain addressable by trusted Workflow/Worker
        # code through the explicit internal path.
        tools.extend(
            s for s in getattr(ToolRegistry, "_skill_implementations", {}).values()
            if s.supports_scene(scene)
            and s.status != "disabled"
            and (allow_write or not _skill_is_write(s))
            and role_allows(s.permission, user_role)
        )
    return tools


def skills_to_tools(scene: str, user_role: str = "user") -> list[dict]:
    """场景内技能 → function calling 工具定义."""
    return [s.to_tool_definition() for s in get_skills_for_scene(scene, user_role)]


def _bootstrap_is_active(value: str) -> bool:
    if not value:
        return False
    try:
        return date.fromisoformat(value) >= date.today()
    except ValueError:
        return False


def _skill_capability(skill: Tool) -> ToolCapability:
    parameters = skill.parameters_schema if isinstance(skill.parameters_schema, dict) else {}
    resource_templates = (
        skill.resource_templates if isinstance(skill.resource_templates, list) else []
    )
    routing = _OFFICE_REACT_ROUTING_METADATA.get(skill.name, {})
    capability = ToolCapability(
        name=skill.name,
        version=skill.version,
        status=skill.status,
        schema_fingerprint=skill.schema_fingerprint,
        replacement_skill_id=skill.replacement_skill_id,
        description=skill.description,
        category=skill.category,
        resource=str(getattr(skill, "resource", "") or skill.category),
        action_type="write" if _skill_is_write(skill) else "read",
        idempotency_type=("non_idempotent" if not skill.idempotent else "natural_key"),
        domain=str(getattr(skill, "domain", "") or routing.get("domain") or skill.category),
        intent_tags=list(getattr(skill, "intent_tags", None) or routing.get("intent_tags") or []),
        conflicts_with=list(getattr(skill, "conflicts_with", None) or routing.get("conflicts_with") or []),
        preferred_over=list(getattr(skill, "preferred_over", None) or routing.get("preferred_over") or []),
        use_when=list(getattr(skill, "use_when", None) or routing.get("use_when") or []),
        do_not_use_when=list(getattr(skill, "do_not_use_when", None) or routing.get("do_not_use_when") or []),
        selection_examples=list(getattr(skill, "selection_examples", None) or routing.get("selection_examples") or []),
        result_contract=str(getattr(skill, "result_contract", "") or routing.get("result_contract") or ""),
        handoff_to=list(getattr(skill, "handoff_to", None) or routing.get("handoff_to") or []),
        bootstrap_intents=list(getattr(skill, "bootstrap_intents", None) or routing.get("bootstrap_intents") or []),
        bootstrap_until=str(getattr(skill, "bootstrap_until", "") or routing.get("bootstrap_until") or ""),
        parameters=parameters,
        source="tool",
        environment=str(skill.environment or "server"),
        permission=skill.permission,
        write_op=_skill_is_write(skill),
        requires_confirmation=bool(skill.requires_confirmation),
        confirmation_mode="client" if skill.environment == "client" else "server",
        idempotent=bool(skill.idempotent and not _skill_is_write(skill)),
        resource_templates=list(resource_templates),
        plan_required_fields=list(getattr(skill, "plan_required_fields", None) or []),
        annotations={
            "cost_estimate": skill.cost_estimate,
            "success_rate": skill.success_rate,
        },
    )
    from app.agents.skills.discovery import enrich_tool_capability

    return enrich_tool_capability(capability)


async def get_capabilities_for_scene(
    scene: str,
    user_role: str = "user",
    user_id: str = "",
    include_nonstable: bool = False,
    include_bootstrap: bool = False,
    include_internal: bool = False,
) -> list[ToolCapability]:
    """统一能力目录；在暴露给 Planner/Executor 前完成权限和写开关过滤。"""
    capabilities = [
        _skill_capability(s)
        for s in get_skills_for_scene(
            scene,
            user_role,
            include_bootstrap=include_bootstrap,
            include_internal=include_internal,
        )
        if skill_runtime_unavailable(s) is None
        and (include_nonstable or s.status == "stable")
    ]
    if bool(getattr(settings, "AGENT_BASE_TOOLS_ONLY", False)) and not include_internal:
        from app.agents.skills.discovery import base_tool_names

        allowed_base = base_tool_names()
        if allowed_base:
            capabilities = [item for item in capabilities if item.name in allowed_base]
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
    # 外部 MCP 绑定也必须服从基础工具迁移开关；否则用户绑定的旧工具会
    # 绕过 L0 白名单重新进入 Function Calling 候选池。
    if bool(getattr(settings, "AGENT_BASE_TOOLS_ONLY", False)) and not include_internal:
        from app.agents.skills.discovery import base_tool_names

        allowed_base = base_tool_names()
        if allowed_base:
            capabilities = [item for item in capabilities if item.name in allowed_base]
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


async def get_desktop_mcp_capabilities(user_id: str, scene: str, user_role: str) -> list[ToolCapability]:
    """Discover trusted Electron plugin Tools for Skill dependency checks.

    These capabilities are intentionally absent from the ordinary model Tool
    pool.  A Workflow Skill must declare them before its runner can invoke the
    qualified name.
    """
    if not user_id:
        return []
    from app.agents.mcp.manager import list_tools, server_is_healthy
    from app.agents.mcp.desktop_connections import desktop_connections

    capabilities: list[ToolCapability] = []
    for server_name in desktop_connections.desktop_server_names():
        for remote in await list_tools(server_name):
            raw_name = str(remote.get("name") or "").strip()
            if not raw_name:
                continue
            permission = str(remote.get("permission") or "user")
            if not role_allows(permission, user_role):
                continue
            capabilities.append(ToolCapability(
                name=f"mcp__{server_name}__{raw_name}",
                version=str(remote.get("version") or "1.0.0"), status="stable",
                description=str(remote.get("description") or ""), category="mcp",
                domain=str(remote.get("domain") or "desktop"),
                parameters=remote.get("input_schema") if isinstance(remote.get("input_schema"), dict) else {"type": "object", "properties": {}},
                source="mcp", environment="client", server=server_name, raw_name=raw_name,
                permission=permission, write_op=bool(remote.get("write_op")),
                requires_confirmation=bool(remote.get("requires_confirmation")),
                confirmation_mode=str(remote.get("confirmation_mode") or "client"),
                idempotent=bool(remote.get("idempotent")),
                resource_templates=list(remote.get("resource_templates") or []),
                annotations={
                    "provider": "desktop_mcp",
                    "availability_hint": "available" if server_is_healthy(server_name) else "offline",
                    "trusted_local_provider": True,
                },
            ))
    return capabilities


async def get_workspace_action_capabilities(
    user_id: str,
    scene: str,
    user_role: str,
    workspace_id: str,
    allowed_raw: frozenset[str] | set[str],
) -> list[ToolCapability]:
    """Build capabilities for an arbitrary workspace capability group.

    Used by stage-based tool windows (read → stage-write → sandbox → commit).
    Only the Electron connection that registered ``workspace_id`` is queried,
    and only tools whose raw name is in ``allowed_raw`` are returned.
    """
    from app.services.workspace_context import resolve_workspace_desktop

    if not user_id or not str(workspace_id or "").strip() or not allowed_raw:
        return []
    route = resolve_workspace_desktop(user_id, str(workspace_id).strip())
    server_name = str(route.get("server_name") or "")
    if not server_name:
        return []
    from app.agents.mcp.manager import list_tools, server_is_healthy

    healthy = server_is_healthy(server_name)
    capabilities: list[ToolCapability] = []
    for remote in await list_tools(server_name):
        raw_name = str(remote.get("name") or "").strip()
        if not raw_name or raw_name not in allowed_raw:
            continue
        permission = str(remote.get("permission") or "user")
        if not role_allows(permission, user_role):
            continue
        capabilities.append(ToolCapability(
            name=f"mcp__{server_name}__{raw_name}",
            version=str(remote.get("version") or "1.0.0"),
            status="stable",
            description=str(remote.get("description") or ""),
            category="workspace",
            domain="workspace",
            parameters=remote.get("input_schema") if isinstance(remote.get("input_schema"), dict)
            else {"type": "object", "properties": {}},
            source="mcp",
            environment="client",
            server=server_name,
            raw_name=raw_name,
            permission=permission,
            write_op=bool(remote.get("write_op")),
            requires_confirmation=bool(remote.get("requires_confirmation")),
            confirmation_mode=str(remote.get("confirmation_mode") or "client"),
            idempotent=bool(remote.get("idempotent")),
            resource_templates=list(remote.get("resource_templates") or []),
            annotations={
                "provider": "desktop_mcp",
                "workspace_id": str(workspace_id).strip(),
                "workspace_stage_tool": True,
                "availability_hint": "available" if healthy else "offline",
                "trusted_local_provider": True,
            },
        ))
    return capabilities


async def get_workspace_navigator_capability(
    user_id: str,
    scene: str,
    user_role: str,
    workspace_id: str,
) -> list[ToolCapability]:
    """模型可见的**唯一**工作区读取能力：``workspace_navigator``（统一聚合入口）。

    目录枚举（list）、文件名/内容检索（search）、单文件原子读取（read）都由该入口
    按 action 分发到后端处理器，再复用 Electron 现有原子工具与格式解析器。模型不需要
    （也看不到）workspace_catalog / workspace_list / workspace_stat / workspace_read /
    workspace_search / workspace_content_extract 这些内部名字，否则工具槽位会被同一
    读取域的多个别名占满。

    ``workspace_id`` 不由模型填写：执行时由 ``execute_tool_call`` 用服务端注入的
    ``authorized_workspace_id`` 覆盖。
    """
    from app.services.workspace_context import (
        WORKSPACE_INTERNAL_READ_CAPABILITIES,
        WORKSPACE_NAVIGATOR,
        WORKSPACE_READ_TOOL_NAMES,
        resolve_workspace_desktop,
    )

    if not user_id or not str(workspace_id or "").strip():
        return []
    route = resolve_workspace_desktop(user_id, str(workspace_id).strip())
    server_name = str(route.get("server_name") or "")
    if not server_name:
        return []
    from app.agents.mcp.manager import list_tools, server_is_healthy

    try:
        advertised = [item for item in await list_tools(server_name) if isinstance(item, dict)]
    except Exception:  # noqa: BLE001 - 工具发现失败时视为离线
        advertised = []
    if not any(str(item.get("name") or "") in WORKSPACE_READ_TOOL_NAMES for item in advertised):
        return []
    healthy = server_is_healthy(server_name)
    permission = "user"
    for item in advertised:
        if str(item.get("name") or "") in WORKSPACE_INTERNAL_READ_CAPABILITIES:
            permission = str(item.get("permission") or "user")
            break
    if not role_allows(permission, user_role):
        return []
    return [ToolCapability(
        name=f"mcp__{server_name}__{WORKSPACE_NAVIGATOR}",
        version="1.0.0",
        status="stable",
        description=(
            "浏览、搜索并读取当前工作区的本地资料（唯一的读取入口）。"
            "action=list 列出目录条目（不读正文，默认不返回隐藏项与 node_modules/构建/缓存）；"
            "action=search 按关键词检索文件名或内容，匹配是 OR（任一关键词命中即返回，按相关度排序），"
            "只返回命中位置与少量上下文；action=read 一次只读取一个文件并返回结构化正文，"
            "内容过长时用 cursor 继续。读取目录请用 list，不要用 read。"
        ),
        category="workspace",
        domain="workspace",
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "search", "read"],
                    "description": "要执行的动作",
                },
                "path": {
                    "type": "string",
                    "description": "工作区内的相对路径；list 省略=根目录，read 必填且必须是单个文件",
                },
                "query": {
                    "type": "string",
                    "description": (
                        "search 的检索词：关键词或短句。空格与 | * [ ( ^ $ . 等都按分隔符处理，"
                        "任一关键词命中即返回（OR，按相关度排序），不是正则、不是严格 AND。"
                    ),
                },
                "search_path": {"type": "string", "description": "search 的限定目录（可选）"},
                "search_mode": {
                    "type": "string",
                    "enum": ["auto", "filename", "content"],
                    "description": "search 模式，默认 auto（先文件名/路径再内容）",
                },
                "depth": {
                    "type": "integer",
                    "description": "list 的递归深度：1（默认，只列当前目录）到 4，超出被钳制到 4",
                },
                "cursor": {
                    "type": "string",
                    "description": "同一次 list/search/read 的后续分页游标，原样回传即可",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "read 每页返回的字符数（分页粒度，不是文件可读总量）",
                },
                "read_to_end": {
                    "type": "boolean",
                    "description": (
                        "read 是否按页连续读取直到文件结束（默认 false=每次一页）。"
                        "为 true 时会消费 cursor 直到读完或达到页数上限；达到上限仍返回 "
                        "has_more=true 与 cursor，继续读取直到 has_more 为 false 才算读完整份。"
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "description": "list/search 本页最大条目数（上限 200）",
                },
                "include_ignored": {
                    "type": "boolean",
                    "description": "list 是否返回被忽略条目（隐藏项与 node_modules/构建/缓存目录），默认 false",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        source="mcp",
        environment="client",
        server=server_name,
        raw_name=WORKSPACE_NAVIGATOR,
        permission=permission,
        write_op=False,
        requires_confirmation=False,
        confirmation_mode="client",
        idempotent=True,
        resource_templates=[],
        annotations={
            "provider": "desktop_mcp",
            "workspace_id": str(workspace_id).strip(),
            "workspace_read_domain": True,
            "unified_read": True,
            "navigator_actions": ["list", "search", "read"],
            "internal_atomic_tools": sorted(WORKSPACE_INTERNAL_READ_CAPABILITIES),
            "availability_hint": "available" if healthy else "offline",
            "trusted_local_provider": True,
        },
    )]


# 历史名保留：旧调用方仍可 import，但返回的同样是聚合入口。
get_workspace_reading_capabilities = get_workspace_navigator_capability


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


async def select_capabilities_with_trace(
    request: str,
    scene: str,
    user_role: str = "user",
    limit: int = 8,
    allowed_names: set[str] | None = None,
    allowed_categories: set[str] | None = None,
    denied_names: set[str] | None = None,
    user_id: str = "",
    skill_allowed_tools: set[str] | None = None,
) -> CapabilitySelection:
    """Select a legal capability namespace and retain a safe routing trace.

    This is a cheap first stage only.  The selected capabilities have already
    passed scene, role, write-toggle and runtime-availability checks.
    """
    capabilities = await get_capabilities_for_scene(
        scene,
        user_role,
        user_id,
        include_bootstrap=(scene == "chat"),
    )
    if allowed_names is not None or allowed_categories is not None or denied_names or skill_allowed_tools is not None:
        capabilities = [
            capability
            for capability in capabilities
            if capability.name not in (denied_names or set())
            and (skill_allowed_tools is None or capability.name in skill_allowed_tools)
            and (
                allowed_names is None and allowed_categories is None
                or capability.name in (allowed_names or set())
                or capability.category in (allowed_categories or set())
                or (
                    scene == "chat"
                    and _bootstrap_is_active(capability.bootstrap_until)
                    and bool(capability.bootstrap_intents)
                )
            )
        ]
    if not capabilities:
        return CapabilitySelection([], [], scene, reason="no_authorized_capabilities")
    # L1/L2 规模化候选收窄：先按域索引定位，再进行工具级排序；保留原有
    # 评分作为 tie-breaker，确保旧评测和插件兼容。
    try:
        from app.agents.skills.discovery import search_tools

        # L2 discovery supplies an ordering hint; the established scorer still
        # owns the final candidate set so legacy Skill/L3 allowlists remain
        # compatible while the namespace scales.
        discovered = search_tools(request, capabilities, limit=max(limit, 5))
        if discovered:
            order = {item.name: index for index, item in enumerate(discovered)}
            capabilities = sorted(capabilities, key=lambda item: order.get(item.name, len(order)))
    except Exception:  # noqa: BLE001
        pass
    request_terms = _routing_terms(request)
    lower = (request or "").casefold()
    preferred_domains = _preferred_domains(request)
    # Do not seed unrelated tools into every request.  The model only sees a
    # small legal candidate set; semantic/lexical ranking may add a capability
    # when the request actually supports it.
    preferred: set[str] = set()
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
            {"python_exec", "shell_exec", "Read", "Write"},
    }
    for markers, names in groups.items():
        if any(marker in lower for marker in markers):
            preferred.update(names)

    routing_mode = "lexical_fallback"
    try:
        from app.agents.skills.routing import semantic_routing_mode, semantic_scores

        semantic = await semantic_scores(request, capabilities)
        routing_mode = semantic_routing_mode() if semantic else "lexical_fallback"
    except Exception:  # noqa: BLE001
        semantic = {}
    ranked: list[tuple[float, int, ToolCapability, bool]] = []
    bootstrap_names: set[str] = set()
    for index, capability in enumerate(capabilities):
        searchable = " ".join([
            capability.name,
            capability.description,
            capability.domain,
            " ".join(capability.intent_tags),
        ])
        overlap = len(request_terms & _routing_terms(searchable))
        tag_overlap = len(request_terms & _routing_terms(" ".join(capability.intent_tags)))
        boundary_overlap = len(request_terms & _routing_terms(" ".join(capability.use_when)))
        exclusion_overlap = len(request_terms & _routing_terms(" ".join(capability.do_not_use_when)))
        score = overlap * 10 + tag_overlap * 18 + boundary_overlap * 14 + (30 if capability.name in preferred else 0)
        # A matching exclusion is a soft penalty only: authorization and the
        # explicit `conflicts_with` / `preferred_over` graph remain the hard
        # boundaries, while this prevents adjacent tools winning on a broad tag.
        score -= exclusion_overlap * 7
        score += semantic.get(capability.name, 0.0) * float(settings.SKILL_ROUTING_SEMANTIC_WEIGHT)
        metadata = capability.annotations if isinstance(capability.annotations, dict) else {}
        bootstrap = _bootstrap_is_active(capability.bootstrap_until)
        if bootstrap and capability.bootstrap_intents:
            bootstrap = bool(request_terms & _routing_terms(" ".join(capability.bootstrap_intents)))
        if bootstrap:
            bootstrap_names.add(capability.name)
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
            elif capability.name in {"Read", "Write", "office_doc_read", "office_doc_analyze"}:
                score -= 35
        ranked.append((score, -index, capability, bootstrap))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    selected: list[ToolCapability] = []
    selected_names: set[str] = set()
    for capability in capabilities:
        if capability.name in bootstrap_names and capability.name not in selected_names:
            selected.append(capability)
            selected_names.add(capability.name)
            if len(selected) >= max(1, limit):
                break
    base_limit = max(1, limit)
    # Keep one strictly bounded slot for a tied K+1 candidate. Explicit tool
    # intent may use that same slot; it never expands the safety ceiling.
    hard_limit = base_limit + max(0, int(settings.SKILL_CANDIDATE_MAX_OVERFLOW))
    last_selected_score: float | None = None
    for score, _, capability, _ in ranked:
        if len(selected) >= base_limit:
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
        last_selected_score = score
    # A near-tied next candidate may be semantically equivalent to the Kth
    # candidate. Keep a bounded overflow rather than making wording decide it.
    if last_selected_score is not None and len(selected) < hard_limit:
        for score, _, capability, _ in ranked:
            if capability.name in selected_names:
                continue
            # Never let bounded overflow bypass hard replacement/conflict
            # contracts.  It may soften a score boundary, not widen the tool
            # namespace with a method already ruled out by a selected one.
            if any(name in selected_names for name in capability.conflicts_with):
                continue
            if any(capability.name in item.conflicts_with for item in selected):
                continue
            if any(capability.name in item.preferred_over for item in selected):
                continue
            # Zero-evidence candidates are not semantic ties.  Including them
            # turns a Top-K pool into a generic tool catalogue and hides real
            # recall misses from observability.
            if score <= 0:
                continue
            if last_selected_score - score > float(settings.SKILL_CANDIDATE_TIE_EPSILON):
                break
            selected.append(capability)
            selected_names.add(capability.name)
            if len(selected) >= hard_limit:
                break
    # ``ranked`` 已以 score 和原始注册顺序作为稳定 tie-breaker 排序；保留该顺序
    # 才能让优先关系真实影响模型看到的工具排列。
    scores = {capability.name: score for score, _, capability, _ in ranked}
    selected_bootstrap = {capability.name for _, _, capability, bootstrap in ranked if bootstrap}
    candidate_rows = [
        {
            "name": capability.name,
            "version": capability.version,
            "score": round(float(scores.get(capability.name, 0.0)), 3),
            "bootstrap": capability.name in selected_bootstrap,
            "availability_hint": str((capability.annotations or {}).get("availability_hint") or "available"),
        }
        for capability in selected
    ]
    # Compute the runner-up from the legal ranked pool, not only the injected
    # Top-K rows. This keeps margin meaningful when a caller asks for limit=1.
    ordered_scores = sorted((float(row["score"]) for row in candidate_rows), reverse=True)
    top_score = ordered_scores[0] if ordered_scores else 0.0
    if len(ordered_scores) > 1:
        second_score = ordered_scores[1]
    elif candidate_rows:
        top_name = str(candidate_rows[0].get("name") or "")
        second_score = max(
            (float(score) for score, _, capability, _ in ranked
             if capability.name != top_name and float(score) > 0),
            default=0.0,
        )
    else:
        second_score = 0.0
    score_margin = top_score - second_score
    ambiguous = (
        len(ordered_scores) > 1
        and score_margin < float(settings.SKILL_CANDIDATE_MARGIN_THRESHOLD)
    )
    return CapabilitySelection(
        capabilities=selected,
        candidates=candidate_rows,
        scene=scene,
        top_score=top_score,
        second_score=second_score,
        score_margin=score_margin,
        ambiguous=ambiguous,
        low_confidence=bool(candidate_rows) and (
            top_score < float(settings.SKILL_CANDIDATE_LOW_CONFIDENCE_SCORE) or ambiguous
        ),
        reason="bootstrap" if any(row["bootstrap"] for row in candidate_rows) else "ranked",
        routing_mode=routing_mode,
    )


async def select_capabilities_for_request(
    request: str,
    scene: str,
    user_role: str = "user",
    limit: int = 8,
    allowed_names: set[str] | None = None,
    allowed_categories: set[str] | None = None,
    denied_names: set[str] | None = None,
    user_id: str = "",
    skill_allowed_tools: set[str] | None = None,
) -> list[ToolCapability]:
    """Compatibility wrapper for callers that only need the candidate list."""
    selection = await select_capabilities_with_trace(
        request,
        scene,
        user_role,
        limit,
        allowed_names,
        allowed_categories,
        denied_names,
        user_id,
        skill_allowed_tools,
    )
    return selection.capabilities


async def select_capabilities_for_skill(
    request: str,
    scene: str,
    skill,
    user_role: str = "user",
    limit: int = 8,
    user_id: str = "",
) -> list[ToolCapability]:
    """按 Workflow Skill 的 ``allowed_tools`` 收窄候选 Tool。"""
    return await select_capabilities_for_request(
        request,
        scene,
        user_role=user_role,
        limit=limit,
        user_id=user_id,
        skill_allowed_tools=set(skill.allowed_tools) if skill.allowed_tools else None,
    )


def _selection_with_capabilities(selection: CapabilitySelection, capabilities: list[ToolCapability], *, reason: str | None = None) -> CapabilitySelection:
    names = {item.name for item in capabilities}
    candidates = [item for item in selection.candidates if item.get("name") in names]
    ordered_scores = sorted((float(item.get("score") or 0.0) for item in candidates), reverse=True)
    top_score = ordered_scores[0] if ordered_scores else 0.0
    second_score = ordered_scores[1] if len(ordered_scores) > 1 else 0.0
    score_margin = top_score - second_score
    ambiguous = (
        len(ordered_scores) > 1
        and score_margin < float(settings.SKILL_CANDIDATE_MARGIN_THRESHOLD)
    )
    return CapabilitySelection(
        capabilities=capabilities,
        candidates=candidates,
        scene=selection.scene,
        top_score=top_score,
        second_score=second_score,
        score_margin=score_margin,
        ambiguous=ambiguous,
        low_confidence=bool(candidates) and (
            top_score < float(settings.SKILL_CANDIDATE_LOW_CONFIDENCE_SCORE) or ambiguous
        ),
        reason=reason or selection.reason,
        routing_mode=selection.routing_mode,
    )


def request_has_explicit_tool_intent(request: str) -> bool:
    """Conservative signal used only for recall-miss monitoring, not routing."""
    lower = (request or "").casefold()
    markers = (
        "联网", "网页来源", "公开资料", "网上查", "搜索一下", "检索一下",
        "知识库", "库里查", "现在几点", "当前时间", "今天几号",
        "算一下", "计算", "打开", "启动",
    )
    return any(marker in lower for marker in markers)


def selection_requires_escalation(selection: CapabilitySelection, request: str) -> bool:
    """Return whether an ambiguous tool pool must stop before model selection.

    Margin is a safety gate only when the user explicitly requests an external
    capability. Broad conversational requests may keep their bounded pool so
    the model can answer without a needless clarification round.
    """
    # 候选分数过近时不让模型盲猜。即使是只读工具，错误选择也会造成
    # 语义漂移或把私有数据请求误送到公开搜索，因此统一升级澄清/复核。
    # 只读候选的 margin 只用于遥测和模型内部裁决，不再阻断请求。写入或
    # 需要确认的候选仍然保留安全闸门：在无法唯一判断副作用时，必须等待
    # 明确授权/确认，不能让模型自行猜测。
    return any(
        bool(item.write_op or item.requires_confirmation)
        for item in selection.capabilities
    ) and bool(selection.ambiguous)


def record_candidate_selection(
    selection: CapabilitySelection,
    *,
    request: str,
    user_id: str = "",
    job_id: str = "",
    selection_round: int = 1,
    model_called: str | None = None,
) -> dict:
    """Emit bounded selection telemetry; never stores request text or prompts."""
    metadata = selection.to_metadata(selection_round=selection_round)
    names = [str(item.get("name") or "") for item in selection.candidates]
    metadata["model_called"] = model_called
    metadata["not_called_candidates"] = [name for name in names if name and name != model_called]
    try:
        from app.core.observability import inc_skill_routing_mode
        from app.monitoring.context import MonitorContext
        from app.monitoring.logger import monitor_logger

        inc_skill_routing_mode(selection.scene, selection.routing_mode)
        monitor_logger.info(
            "工具候选池已确定",
            event_type="tool_candidate_selection",
            category="tool_selection",
            code="TOOL_CANDIDATE_SELECTION",
            context=MonitorContext(job_id=job_id or None, execution_id=job_id or None, user_id=user_id or None, component="skill_router"),
            metadata=metadata,
        )
        if request_has_explicit_tool_intent(request) and (not names or selection.low_confidence):
            monitor_logger.warning(
                "明确工具意图的候选池置信度偏低，可能存在召回漏失",
                event_type="tool_candidate_low_confidence",
                category="tool_selection",
                code="TOOL_CANDIDATE_LOW_CONFIDENCE",
                context=MonitorContext(job_id=job_id or None, execution_id=job_id or None, user_id=user_id or None, component="skill_router"),
                metadata={**metadata, "explicit_tool_intent": True},
            )
        expiry_window = max(0, int(settings.SKILL_BOOTSTRAP_EXPIRING_DAYS))
        today = date.today()
        for capability in selection.capabilities:
            if not capability.bootstrap_until:
                continue
            try:
                expires = date.fromisoformat(capability.bootstrap_until)
            except ValueError:
                continue
            if not today <= expires <= today + timedelta(days=expiry_window):
                continue
            alert_key = (capability.name, today.isoformat())
            if alert_key in _bootstrap_expiry_alerts:
                continue
            _bootstrap_expiry_alerts.add(alert_key)
            monitor_logger.warning(
                "Skill bootstrap 即将到期，请审阅候选命中和模型选择率后修复契约",
                event_type="tool_candidate_bootstrap_expiring",
                category="tool_selection",
                code="BOOTSTRAP_EXPIRING",
                context=MonitorContext(component="skill_router"),
                metadata={
                    "skill": capability.name,
                    "version": capability.version,
                    "bootstrap_until": capability.bootstrap_until,
                    "days_remaining": (expires - today).days,
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug("候选池监控记录失败，继续执行")
    return metadata


async def get_chat_capabilities_with_trace(
    request: str,
    user_role: str = "user",
    user_id: str = "",
    limit: int = 5,
) -> CapabilitySelection:
    selection = await select_capabilities_with_trace(
        request,
        "chat",
        user_role,
        limit=max(1, limit),
        allowed_names=_CHAT_SKILL_ALLOWLIST,
        user_id=user_id,
    )
    request_terms = _routing_terms(request)
    supported = [
        item for item in selection.capabilities
        if request_terms & _routing_terms(
            " ".join([
                item.name,
                item.domain,
                *item.intent_tags,
                *item.use_when,
                *item.bootstrap_intents,
            ])
        )
    ]
    return _selection_with_capabilities(selection, supported[:max(1, limit)], reason="supported" if supported else "no_positive_evidence")


async def get_chat_capabilities_for_request(
    request: str,
    user_role: str = "user",
    user_id: str = "",
    limit: int = 5,
) -> list[ToolCapability]:
    """Return the smallest chat tool namespace that can satisfy this request."""
    return (await get_chat_capabilities_with_trace(request, user_role, user_id, limit)).capabilities


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
    return (await get_office_react_capabilities_with_trace(
        request, user_role, limit, excluded_names, user_id
    )).capabilities


async def get_office_react_capabilities_with_trace(
    request: str,
    user_role: str = "user",
    limit: int = 8,
    excluded_names: set[str] | None = None,
    user_id: str = "",
) -> CapabilitySelection:
    selection = await select_capabilities_with_trace(
        request,
        "office",
        user_role,
        limit,
        allowed_names=_OFFICE_REACT_ALLOWED_SKILLS,
        allowed_categories=_OFFICE_REACT_ALLOWED_CATEGORIES,
        denied_names=_OFFICE_REACT_DENIED_SKILLS,
        user_id=user_id,
    )
    # Once the request contains a clear domain marker, unrelated generic tools
    # should not fill the remaining candidate slots.  Keep knowledge retrieval
    # available for document requests because it is the read-only companion to
    # document analysis; all other domains stay within their declared scope.
    capabilities = selection.capabilities
    preferred_domains = _preferred_domains(request)
    if preferred_domains:
        allowed_domains = set(preferred_domains)
        if "document" in preferred_domains:
            allowed_domains.add("research")
        scoped = [item for item in capabilities if item.domain in allowed_domains]
        if scoped:
            capabilities = scoped
    excluded_names = excluded_names or set()
    capabilities = [item for item in capabilities if item.name not in excluded_names]
    return _selection_with_capabilities(selection, capabilities, reason="scoped" if preferred_domains else selection.reason)


async def get_tools_for_scene(
    scene: str,
    user_role: str = "user",
    user_id: str = "",
    *,
    include_internal: bool = False,
) -> list[dict]:
    """返回 Function Calling 可见的原子 Tool + 已绑定 MCP 工具。

    Workflow Skill 永远不从这里出现；它们只能由 Planner/Worker 选择。
    """
    capabilities = await get_capabilities_for_scene(
        scene, user_role, user_id, include_internal=include_internal
    )
    return [capability.to_tool_definition() for capability in capabilities]


async def get_tool_capability(
    name: str,
    scene: str,
    user_role: str = "user",
    user_id: str = "",
    *,
    include_internal: bool = False,
) -> ToolCapability | None:
    # Execution already has an exact, governed tool name.  Rebuilding the
    # whole scene catalog here needlessly queries user MCP bindings,
    # telemetry, and semantic-index warmup before every local call.  Besides
    # adding latency, an unavailable database could block a completely local
    # tool.  Apply the same lifecycle/scene/role/write gates directly first;
    # dynamic discovery remains the fallback for MCP capabilities.
    registered = ToolRegistry.get(name)
    registered_is_public = name in getattr(ToolRegistry, "_tools", {})
    if registered is not None and (registered_is_public or include_internal):
        write_allowed = bool(settings.AGENT_TOOL_WRITE_ENABLED) or not _skill_is_write(registered)
        stable = registered.status == "stable"
        scene_allowed = registered.supports_scene(scene)
        role_allowed = role_allows(registered.permission, user_role)
        base_allowed = True
        if bool(getattr(settings, "AGENT_BASE_TOOLS_ONLY", False)) and not include_internal:
            from app.agents.skills.discovery import base_tool_names

            allowed_base = base_tool_names()
            base_allowed = not allowed_base or name in allowed_base
        if (
            write_allowed
            and stable
            and scene_allowed
            and role_allowed
            and base_allowed
            and skill_runtime_unavailable(registered) is None
        ):
            return _skill_capability(registered)
    for capability in await get_capabilities_for_scene(
        scene, user_role, user_id, include_internal=include_internal
    ):
        if capability.name == name:
            return capability
    if name.startswith("mcp__"):
        for capability in await get_desktop_mcp_capabilities(user_id, scene, user_role):
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
    if skill_name != "Delete" or bool(args.get("recursive")):
        return False
    message = str(user_message or "").strip()
    if not message or not _EXPLICIT_DELETE_RE.search(message):
        return False
    target = str(args.get("file_path") or args.get("path") or "").strip().replace("\\", "/")
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


def _navigator_result_count(payload: dict) -> int:
    """workspace_navigator 信封中的结果条数（用于审计与决策信号，不含正文）。"""
    if not isinstance(payload, dict):
        return 0
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    total = 0
    for key in ("entries", "matches", "sections"):
        value = data.get(key)
        if isinstance(value, list):
            total += len(value)
    return total


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


def _validate_tool_arguments(schema: dict, args: dict) -> tuple[str | None, list[str]]:
    """Validate every native tool call with the same contract as MCP tools."""
    if not isinstance(schema, dict):
        return None, []
    try:
        from jsonschema import Draft202012Validator

        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(args or {}), key=lambda item: list(item.path))
        if not errors:
            return None, []
        missing: list[str] = []
        for error in errors:
            if error.validator == "required":
                for field in error.validator_value:
                    if field not in (args or {}):
                        missing.append(str(field))
        return str(errors[0].message)[:300], sorted(set(missing))
    except Exception:
        # A malformed legacy schema must not take down the executor; the tool
        # implementation remains responsible for its own defensive checks.
        return None, []


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
    approval_context_sha256: str = "",
    office_doc_ids: tuple[str, ...] | list[str] | None = None,
    authorized_project_ids: tuple[str, ...] | list[str] | None = None,
    authorized_workspace_id: str = "",
    on_output=None,
    execution_scope: str = "",
    allowed_tools: set[str] | None = None,
    allow_internal: bool = False,
    mcp_call_id: str | None = None,
    capability_lease_service: Any = None,
) -> SkillResult:
    """执行一次技能调用：校验 → 高危拦截 → 执行 → 审计。

    ``execution_scope`` 仅由 DAG 节点执行器注入。它将同一 Job 中的同名工具
    调用串行化，而不会影响普通聊天会话或不同工具的节点级并发。

    ``capability_lease_service`` 供测试/定制注入租约服务；缺省时能力路由自建一个。
    它只在 ``AGENT_CAPABILITY_ROUTING_MODE != off`` 时被使用。
    """
    original_fn = tool_call.get("function") or {}
    name = str(original_fn.get("name") or "").strip()
    args = _parse_arguments(original_fn.get("arguments"))
    if not isinstance(args, dict):
        # 参数必须是对象：模型偶尔会传数组/标量，这里给可自纠的错误而不是 500。
        return SkillResult(
            success=False,
            error="工具参数必须是 JSON 对象",
            error_code="INVALID_ARGS",
            retryable=False,
            metadata={"tool": name},
        )
    # Reserved policy fields can never originate from a model tool call.
    args.pop("_lumi_execution_policy", None)
    # 注意：MCP 工具**不套 ToolRequest 外壳**——调用直接走 MCP client（参数的
    # 权威定义是 MCP 自己的 inputSchema），这里只做"参数必须是对象"的通用校验。
    if allowed_tools is not None and name not in allowed_tools:
        return SkillResult(
            success=False,
            error=f"当前 Skill 未授权调用工具: {name}",
            error_code="SKILL_TOOL_FORBIDDEN",
            retryable=False,
            metadata={"tool": name},
        )
    # Router v2 灰度：工具级风控（SafetyGuard 两层策略的环境敏感强校验）。
    if getattr(settings, "TASK_ROUTER_V2_ENABLED", False):
        try:
            from app.services.safety_adapter import enforce_tool_safety

            allowed, safety_action, message, code = enforce_tool_safety(tool_call)
            if not allowed:
                return SkillResult(
                    success=False,
                    error=message,
                    error_code=code,
                    retryable=False,
                    metadata={"tool": name, "safety_action": safety_action.value},
                )
        except Exception as exc:  # noqa: BLE001 - 风控适配异常按拒绝执行处理
            logger.warning("工具级风控校验失败，按拒绝执行处理: {}", str(exc)[:160])
            return SkillResult(
                success=False,
                error="安全策略校验失败，已阻止该工具调用。",
                error_code="SAFETY_CHECK_FAILED",
                retryable=False,
                metadata={"tool": name},
            )
    capability = await get_tool_capability(
        name, scene, user_role, user_id, include_internal=allow_internal
    )
    if capability is None:
        registered = ToolRegistry.get(name)
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

    validation_error, missing_fields = _validate_tool_arguments(capability.parameters, args)
    if validation_error:
        return SkillResult(
            success=False,
            error="工具参数不完整或格式不正确",
            error_code="INVALID_PARAMS",
            retryable=False,
            metadata={
                "tool": name,
                "validation_message": validation_error,
                "missing_fields": missing_fields,
                "user_action_required": bool(missing_fields),
            },
        )

    # 项目/代码工具必须绑定到提交时由服务端注入的项目范围。模型不能仅
    # 通过传入 project_id，或在提示词中声称“这是我的项目”，扩大授权。
    # 旧项目工具规范化为 Read/Write/Glob/Grep/Bash 后，仍必须保留项目
    # 授权边界；只有携带 project_id 的调用才走项目范围校验。
    project_scoped = name in _PROJECT_SCOPED_SKILLS or (
        name in {"Read", "Write", "Edit", "Glob", "Grep", "Bash"}
        and bool(args.get("project_id"))
    )
    if project_scoped:
        requested_project = str(args.get("project_id") or "").strip()
        allowed_projects = {str(value).strip() for value in (authorized_project_ids or ()) if str(value).strip()}
        if not requested_project or requested_project not in allowed_projects:
            return SkillResult(
                success=False,
                error="未授权访问该项目；请在任务提交时明确选择项目后重试",
                error_code="PROJECT_SCOPE_REQUIRED",
                retryable=False,
                metadata={"tool": name},
            )

    # 客户端文件/命令参数也必须经过统一资源策略。该校验发生在创建
    # Redis 客户端请求之前，网页或模型无法借参数把后端源码、凭据目录
    # 伪装成普通工作区资源。
    if name in {"Read", "Write", "Edit", "Glob", "Grep", "NotebookEdit"}:
        path_value = args.get("file_path") or args.get("notebook_path") or args.get("path")
        if path_value:
            try:
                validate_client_path(str(path_value), field="file_path")
            except ResourcePolicyError as exc:
                return SkillResult(success=False, error=str(exc), error_code="RESOURCE_FORBIDDEN", retryable=False, metadata={"tool": name})
    if name == "Bash":
        try:
            validate_command(str(args.get("command") or ""), cwd=str(args.get("cwd") or ""))
        except ResourcePolicyError as exc:
            return SkillResult(success=False, error=str(exc), error_code="RESOURCE_FORBIDDEN", retryable=False, metadata={"tool": name})

    # ── 能力路由门禁（灰度旁路；默认 off = 零开销，行为与旧版逐字相同）──
    # 位置：参数校验与资源策略之后、MCP 分支之前——即"授权已确认，但还没决定谁执行"。
    from app.agents.skills.capability_route import try_capability_route

    routed = await try_capability_route(
        tool_name=name,
        args=args,
        user_id=user_id,
        user_role=user_role,
        conversation_id=conversation_id,
        workspace_id=str(authorized_workspace_id or ""),
        authorized_project_ids=authorized_project_ids,
        lease_service=capability_lease_service,
        task_id=execution_scope or None,
        call_id=mcp_call_id,
        # 既有工具级审批的**确切指纹**：命中即视为该能力调用已获批准（参数变了就不命中）。
        approved_tool_calls=confirmed_tool_calls,
        upstream_sha256=approval_context_sha256,
    )
    if routed is not None:
        return routed

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
        if tool_name.startswith(("workspace_", "sandbox_")):
            trusted_workspace = str(authorized_workspace_id or "").strip()
            requested_workspace = str(args.get("workspace_id") or "").strip()
            if tool_name in {"workspace_read", "workspace_navigator"}:
                # 统一读取入口的工作区由服务端注入；模型不传（也不应传）workspace_id。
                requested_workspace = trusted_workspace
            if not trusted_workspace:
                return SkillResult(
                    success=False,
                    error="当前任务没有已选择的工作区；请先在办公模式中新建或打开项目",
                    error_code="WORKSPACE_SCOPE_REQUIRED",
                    retryable=False,
                    metadata={"server": server_name, "tool": tool_name},
                )
            if requested_workspace != trusted_workspace:
                return SkillResult(
                    success=False,
                    error="工具请求的工作区不属于当前任务",
                    error_code="WORKSPACE_SCOPE_FORBIDDEN",
                    retryable=False,
                    metadata={"server": server_name, "tool": tool_name},
                )
        if tool_name == "workspace_navigator":
            # 唯一模型可见的读取入口：action=list/search/read 由后端聚合服务分发到
            # 内部处理器，再复用 Electron 原子工具与格式解析器。
            # workspace_id 一律取服务端注入值，模型传值被忽略。
            from app.services.workspace_navigator import (
                ACTIONS as NAVIGATOR_ACTIONS,
                WorkspaceNavigatorService,
                model_text as navigator_model_text,
            )

            action = str(args.get("action") or "").strip().casefold()
            if action not in NAVIGATOR_ACTIONS:
                return SkillResult(
                    success=False,
                    error=f"workspace_navigator.action 只能是 list/search/read，收到：{action or '（空）'}",
                    error_code="INVALID_ACTION",
                    retryable=False,
                    metadata={"tool": tool_name, "action": action, "allowed": list(NAVIGATOR_ACTIONS)},
                )
            navigator = WorkspaceNavigatorService(
                user_id=user_id,
                user_role=user_role,
                workspace_id=str(authorized_workspace_id or "").strip(),
                conversation_id=conversation_id,
                request=str(user_message or ""),
            )
            payload = await navigator.execute(action, args)
            payload_status = str(payload.get("status") or "error")
            ok = payload_status in {"ok", "partial", "empty"}
            return SkillResult(
                status="success" if ok else "failed",
                output=navigator_model_text(payload),
                data=payload,
                content_type="structured",
                error=None if ok else str((payload.get("error") or {}).get("message") or "读取失败"),
                error_code=None if ok else str((payload.get("error") or {}).get("code") or "") or None,
                retryable=False,
                metadata={
                    "tool": "workspace_navigator",
                    "unified_read": True,
                    "navigator_action": action,
                    "result_count": _navigator_result_count(payload),
                    "workspace_version": (payload.get("meta") or {}).get("workspace_version"),
                },
            )
        if tool_name == "workspace_read":
            # 唯一读取能力：目录/定位/解析/分页全部在统一读取服务内部完成。
            from app.services.workspace_reader import WorkspaceReader, json_dumps

            reader = WorkspaceReader(
                user_id=user_id,
                user_role=user_role,
                workspace_id=str(authorized_workspace_id or "").strip(),
                conversation_id=conversation_id,
            )
            payload = await reader.read(
                str(args.get("request") or user_message or ""),
                path=str(args.get("path") or ""),
                cursor=str(args.get("cursor") or ""),
                max_chars=int(args.get("max_chars") or 12000),
            )
            read_status = str(payload.get("status") or "failed")
            readable = read_status in {"success", "partial", "empty"}
            return SkillResult(
                status=read_status,
                output=json_dumps(payload),
                data=payload,
                content_type="structured",
                error=None if readable else str(payload.get("summary") or "读取失败"),
                error_code=str((payload.get("meta") or {}).get("error_code") or "") or None,
                retryable=False,
                metadata={"tool": "workspace_read", "unified_read": True},
            )
        validation_error = _validate_mcp_arguments(capability.parameters, args)
        if validation_error:
            return SkillResult(
                success=False,
                error=validation_error,
                error_code="INVALID_ARGS",
                retryable=False,
                metadata={"server": server_name, "tool": tool_name},
            )
        # 统一审批策略（ApprovalPolicyEngine）：工作区/沙箱工具不再只依赖工具
        # 自身 requires_confirmation，而是按 三档（A 自动 / B 例行 / C 始终确认）
        # + 执行授权快照（approval_mode）集中判定。Skill/Workflow 无法自行绕过。
        engine_decision = None
        policy_meta: dict = {}
        is_workspace_mcp = mcp_target is not None and tool_name.startswith(("workspace_", "sandbox_"))
        if is_workspace_mcp:
            from app.agents.skills.approval_policy import classify_tool_risk, should_confirm

            tier, _risk, _reason = classify_tool_risk(name, args)
            if tier == "auto":
                engine_decision = None  # A 档：默认自动，无需加载上下文
            else:
                wsid_for_policy = str(authorized_workspace_id or "").strip() or str(args.get("workspace_id") or "")
                ws_available = bool(authorized_workspace_id)
                approval_mode = "manual_commit"
                policy_expires_at = ""
                if wsid_for_policy:
                    try:
                        from app.services.workspace_context import load_workspace_context

                        ctx_policy = await load_workspace_context(
                            user_id, workspace_id=wsid_for_policy
                        )
                        ws_available = ctx_policy.available
                        approval_mode = ctx_policy.approval_mode
                        policy_expires_at = str(getattr(ctx_policy, "expires_at", "") or "")
                    except Exception:  # noqa: BLE001 - 快照缺失按保守默认
                        ws_available = bool(authorized_workspace_id)
                engine_decision = should_confirm(
                    tool=name,
                    arguments=args,
                    workspace_context={
                        "workspace_available": ws_available,
                        "approval_mode": approval_mode,
                        "expires_at": policy_expires_at,
                    },
                    execution_grant={"approval_mode": approval_mode},
                )
                policy_meta = {
                    "policy_decision": engine_decision.decision,
                    "policy_risk": engine_decision.risk,
                    "policy_reason": engine_decision.reason,
                    "policy_scope": engine_decision.scope,
                }
                if engine_decision.decision == "deny":
                    return SkillResult(
                        success=False,
                        error=engine_decision.reason or "工作区策略禁止该操作",
                        error_code="WORKSPACE_POLICY_DENIED",
                        retryable=False,
                        metadata={
                            "server": server_name, "tool": tool_name,
                            **policy_meta,
                        },
                    )
        # Desktop MCP is the single approval authority for client-confirmed
        # workspace actions.  Let the call reach Electron so it can apply the
        # local workspace policy, show one final confirmation when needed, and
        # return pending_approval.  Server-confirmed providers still stop here.
        client_owns_confirmation = bool(
            is_workspace_mcp and capability.confirmation_mode == "client"
        )
        need_user_confirm = (not client_owns_confirmation) and (bool(
            engine_decision is not None and engine_decision.decision == "require_confirmation"
        ) or bool(
            engine_decision is None
            and capability.requires_confirmation
            and capability.confirmation_mode != "client"
        ))
        if need_user_confirm and not is_tool_call_confirmed(name, args, confirmed_tool_calls, approval_context_sha256):
            reason = (engine_decision.reason if engine_decision is not None else "该 MCP 操作需要用户确认")
            return SkillResult(
                success=False,
                error=reason,
                error_code="NEEDS_CONFIRMATION",
                retryable=False,
                metadata={
                    "server": server_name,
                    "tool": tool_name,
                    "approval_fingerprint": tool_call_fingerprint(name, args, approval_context_sha256),
                    **policy_meta,
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
            async with _claim_tool_execution(name, execution_scope):
                # Route the call to the desktop that registered the workspace
                # (device-aware MCP routing).  The parsed capability name keeps
                # its original server, but the authoritative connection is the
                # one recorded on the workspace manifest; a task started from
                # another device is forwarded to that Electron.
                routed_server = server_name
                call_workspace_id = str(authorized_workspace_id or "").strip()
                call_device_id = ""
                if mcp_target and (tool_name.startswith(("workspace_", "sandbox_")) or call_workspace_id):
                    try:
                        from app.services.workspace_context import resolve_workspace_desktop

                        route = resolve_workspace_desktop(user_id, call_workspace_id or str(args.get("workspace_id") or ""))
                        call_device_id = str(route.get("device_id") or "")
                        if route.get("server_name"):
                            routed_server = str(route["server_name"])
                    except Exception:  # noqa: BLE001 - 路由解析失败仍按原 server 调用
                        call_device_id = ""
                raw = await call_tool(
                    routed_server,
                    tool_name,
                    args,
                    call_id=mcp_call_id,
                    task_id=conversation_id or None,
                    on_progress=on_notify,
                    user_id=user_id,
                    device_id=call_device_id,
                    workspace_id=call_workspace_id,
                    conversation_id=conversation_id or "",
                )
        except ToolExecutionCoordinationUnavailable:
            return _tool_coordination_failure(name)
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
        result = sanitize_server_result(normalize_execution_envelope(raw, tool_name=tool_name))
        # 服务端可见的工作区写操作成功后，版本缓存应当失效；文件变化也可由
        # Electron 版本通知或下一轮 workspace_diff 版本探测兜底。
        if (
            result.success
            and tool_name in {"workspace_commit", "sandbox_commit", "workspace_rollback", "workspace_stage_delete"}
            and call_workspace_id
        ):
            try:
                from app.services.workspace_context import invalidate_workspace_context

                await invalidate_workspace_context(call_workspace_id)
            except Exception:  # noqa: BLE001
                pass
        await _record_skill_telemetry(
            capability, scene, result, int((time.perf_counter() - started_at) * 1000)
        )
        audit_scope: dict = dict(policy_meta or {})
        if call_workspace_id:
            audit_scope["workspace_id"] = str(call_workspace_id)
        if call_device_id:
            audit_scope["device_id"] = str(call_device_id)
        await _record_skill_log(user_id, capability, args, result, scope_meta=audit_scope or None)
        return result

    skill = ToolRegistry.get(name)
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
        skill.requires_confirmation_for(args)
        and skill.environment != "client"
        and not explicit_user_delete
        and not is_tool_call_confirmed(name, args, confirmed_tool_calls, approval_context_sha256)
    ):
        result = SkillResult(
            success=False,
            error="该操作属于高危行为，需要用户确认后才能执行",
            error_code="NEEDS_CONFIRMATION",
            retryable=False,
            metadata={
                "skill": name,
                "params": args,
                "approval_fingerprint": tool_call_fingerprint(name, args, approval_context_sha256),
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
        office_doc_ids=tuple(str(value) for value in (office_doc_ids or ()) if str(value).strip()),
        authorized_project_ids=tuple(str(value) for value in (authorized_project_ids or ()) if str(value).strip()),
        workspace_id=str(authorized_workspace_id or "").strip(),
    )
    # 进程内（backend/sandbox）工具才走契约 ToolRequest：它承载本地命令的
    # 参数对象校验、关联标识与幂等/审批绑定。MCP 工具在上面的分支里已经直接
    # 调用了 MCP client，不重复包一层。
    tool_request = ToolRequest(
        tool_name=name,
        arguments=args,
        call_id=str(mcp_call_id or tool_call.get("id") or ""),
        idempotency_key=str(tool_call.get("id") or ""),
        approval_fingerprint=str(approval_context_sha256 or ""),
    )
    args = tool_request.arguments
    # All registered Skills now pass through the MCP gateway.  The gateway
    # chooses Electron MCP for client capabilities and an in-process adapter
    # for backend/sandbox capabilities, preserving one timeout/result path.
    from app.agents.mcp.manager import call_skill

    started_at = time.perf_counter()
    try:
        async with _claim_tool_execution(name, execution_scope):
            raw = await call_skill(
                skill,
                args,
                context=context,
                task_id=conversation_id or None,
                on_progress=on_notify,
                execution_policy=execution_policy,
                call_id=tool_request.call_id or None,
            )
    except ToolExecutionCoordinationUnavailable:
        return _tool_coordination_failure(name)
    # ``call_skill`` crosses the local/MCP boundary with the canonical
    # ExecutionOutput envelope.  Normalize once here so callers receive the
    # same object regardless of transport; do not reconstruct legacy
    # success/content/metadata fields.
    # 信封 → 契约 → ToolOutput 的转换只在契约适配器内部接触裸字典。
    result = normalize_execution_envelope(raw, tool_name=name)
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
    skill: Tool | ToolCapability,
    params: dict,
    result: SkillResult,
    *,
    scope_meta: dict | None = None,
) -> None:
    """技能调用审计：control_logs 表（失败不阻塞主流程）.

    ``scope_meta`` 可携带工作区/设备/审批策略结果等执行上下文，随 detail 落库。
    """
    try:
        uid = uuid.UUID(str(user_id)) if user_id else None
        if uid is None:
            return
        detail = {
            "error_code": result.error_code,
            "error": result.error,
            "output": result.output[:500],
        }
        # 契约审计投影：把"调了什么/结果如何/耗时/错误码"结构化落库，
        # 排障不必再从自由文本里猜（不含业务正文）。
        from app.contracts.projections import result_audit

        audit = result_audit(result, tool_name=str(getattr(skill, "name", "") or ""))
        if audit:
            detail["audit"] = audit
        if scope_meta:
            detail["scope_meta"] = scope_meta
        async with async_session_factory() as session:
            session.add(
                ControlLog(
                    user_id=uid,
                    action=f"skill:{skill.name}",
                    target=json.dumps(params, ensure_ascii=False)[:500],
                    success=result.success,
                    detail=json.dumps(detail, ensure_ascii=False)[:2000],
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
        # Best effort: the timeout may race a late client result. Marking the
        # request cancelled makes clients discard it even if they poll after
        # the server-side workflow has already moved on.
        try:
            await client_tools.cancel_request(user_id, req["request_id"])
        except Exception:  # noqa: BLE001
            pass
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
            data=result.get("data"),
            content_type=str(result.get("content_type") or "text"),
            metadata=result.get("metadata") or {},
        )
    return SkillResult(
        success=False,
        error=str(result.get("error") or "客户端执行失败"),
        error_code=str(result.get("error_code") or (result.get("metadata") or {}).get("error_code") or "EXEC_ERROR"),
        retryable=False,
        data=result.get("data"),
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

        assistant_message = {
            "role": "assistant", "content": content or None, "tool_calls": tool_calls
        }
        # DeepSeek thinking-mode tool loops require the opaque reasoning
        # payload on the assistant message that issued the call.  The unified
        # response normalizer may carry it on each normalized call; lift it to
        # the message level before appending the tool result.
        if tool_calls and isinstance(tool_calls[0], dict):
            reasoning = tool_calls[0].get("reasoning_content")
            if reasoning is not None:
                assistant_message["reasoning_content"] = reasoning
        messages.append(assistant_message)
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
                    "content": wrap_untrusted_tool_output(_result_for_model(result)),
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

    return clean_assistant_text(redact_server_text(final_text)), records, citations


def _result_for_model(result: SkillResult) -> str:
    """Legacy-loop equivalent of the LangChain model-facing result contract."""
    return project_tool_output(result)


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

    # 办公 DAG 的原子节点仍由 NodeExecutionRunner 编排；这里覆盖的是所有
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
