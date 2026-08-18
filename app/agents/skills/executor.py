"""技能执行器 —— LLM function calling 循环 + 参数校验 + 审计.

职责:
  - 按场景过滤可用的技能（category/permission 治理入口）
  - 把技能列表转成 function calling 工具定义交给 LLM
  - 解析并执行 LLM 发出的工具调用，结果回填对话继续循环
  - 高危技能（requires_confirmation）执行前拦截，等待用户确认
  - 每次调用写审计日志（control_logs）
"""

import json
import time
import uuid

from loguru import logger

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.capability import ToolCapability, role_allows
from app.agents.skills.registry import SkillRegistry
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.db_models import ControlLog
from app.services import client_tools
from app.services.usage import CATEGORY_CHAT, CATEGORY_SKILL


_WRITE_NAME_HINTS = (
    "write", "edit", "delete", "rename", "move", "create", "install",
    "send", "apply_patch", "kill", "rollback", "commit", "todo", "calendar",
)


def _skill_is_write(skill: Skill) -> bool:
    name = str(skill.name or "").lower()
    return bool(
        skill.write_op
        or skill.requires_confirmation
        or any(hint in name for hint in _WRITE_NAME_HINTS)
    )


def get_skills_for_scene(scene: str, user_role: str = "user") -> list[Skill]:
    """按场景过滤技能（scenes 白名单；空 = 全场景）.

    渐进开放写工具：AGENT_TOOL_WRITE_ENABLED=False 时隐藏写操作技能（只读先行）。
    """
    allow_write = bool(settings.AGENT_TOOL_WRITE_ENABLED)
    return [
        s
        for s in SkillRegistry.list()
        if s.supports_scene(scene)
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
    return ToolCapability(
        name=skill.name,
        description=skill.description,
        parameters=parameters,
        source="skill",
        permission=skill.permission,
        write_op=_skill_is_write(skill),
        requires_confirmation=bool(skill.requires_confirmation),
        confirmation_mode="client" if skill.environment == "client" else "server",
        idempotent=bool(skill.idempotent and not _skill_is_write(skill)),
        resource_templates=list(resource_templates),
    )


async def get_capabilities_for_scene(
    scene: str,
    user_role: str = "user",
) -> list[ToolCapability]:
    """统一能力目录；在暴露给 Planner/Executor 前完成权限和写开关过滤。"""
    capabilities = [_skill_capability(s) for s in get_skills_for_scene(scene, user_role)]
    try:
        from app.agents.mcp.manager import list_all_tools

        for tool in await list_all_tools():
            capability = ToolCapability(
                name=tool["name"],
                description=f"MCP({tool['server']})：{tool.get('description') or tool.get('raw_name')}",
                parameters=tool.get("input_schema") or {"type": "object", "properties": {}},
                source="mcp",
                server=tool["server"],
                raw_name=tool.get("raw_name"),
                permission=str(tool.get("permission") or "user"),
                write_op=bool(tool.get("write_op")),
                requires_confirmation=bool(tool.get("requires_confirmation")),
                confirmation_mode=str(tool.get("confirmation_mode") or "client"),
                idempotent=bool(tool.get("idempotent", False)),
                resource_templates=list(tool.get("resource_templates") or []),
                annotations=dict(tool.get("annotations") or {}),
            )
            if not role_allows(capability.permission, user_role):
                continue
            if capability.write_op and not settings.AGENT_TOOL_WRITE_ENABLED:
                continue
            capabilities.append(capability)
    except Exception as exc:  # noqa: BLE001
        logger.debug("发现 MCP 工具失败，继续使用本地 Skill: {}", exc)
    return capabilities


async def get_tools_for_scene(scene: str, user_role: str = "user") -> list[dict]:
    """统一工具目录：本地 Skill（含 system）+ 所有已连接的 MCP 工具."""
    return [c.to_tool_definition() for c in await get_capabilities_for_scene(scene, user_role)]


async def get_tool_capability(name: str, scene: str, user_role: str = "user") -> ToolCapability | None:
    for capability in await get_capabilities_for_scene(scene, user_role):
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


async def execute_tool_call(
    tool_call: dict,
    user_id: str,
    scene: str = "chat",
    conversation_id: str = "",
    on_notify=None,
    user_role: str = "user",
) -> SkillResult:
    """执行一次技能调用：校验 → 高危拦截 → 执行 → 审计."""
    fn = tool_call.get("function") or {}
    name = str(fn.get("name") or "")
    args = _parse_arguments(fn.get("arguments"))
    capability = await get_tool_capability(name, scene, user_role)
    if capability is None:
        registered = SkillRegistry.get(name)
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

        server_name, tool_name = mcp_target
        if capability.requires_confirmation and capability.confirmation_mode != "client":
            return SkillResult(
                success=False,
                error="该 MCP 操作需要用户确认",
                error_code="NEEDS_CONFIRMATION",
                retryable=False,
                metadata={"server": server_name, "tool": tool_name},
            )
        raw = await call_tool(server_name, tool_name, args)
        if raw is None:
            return SkillResult(
                success=False,
                error=f"MCP 工具不可用: {server_name}/{tool_name}",
                error_code="MCP_UNAVAILABLE",
                retryable=True,
                metadata={"server": server_name, "tool": tool_name},
            )
        return SkillResult(
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
        )

    skill = SkillRegistry.get(name)
    if not skill:
        return SkillResult(
            success=False,
            error=f"技能不存在: {name}",
            error_code="SKILL_NOT_FOUND",
            retryable=False,
            metadata={"skill": name},
        )
    if not role_allows(skill.permission, user_role):
        return SkillResult(
            success=False,
            error=f"技能 {name} 需要 {skill.permission} 权限",
            error_code="FORBIDDEN",
            retryable=False,
            metadata={"skill": name, "required": skill.permission, "actual": user_role},
        )

    # 高危操作：server/sandbox 技能执行前必须确认（暂不支持，一律拒绝）；
    # client 技能由用户端弹窗确认（执行体内部处理），不在此拦截
    if skill.requires_confirmation and skill.environment != "client":
        result = SkillResult(
            success=False,
            error="该操作属于高危行为，需要用户确认后才能执行",
            error_code="NEEDS_CONFIRMATION",
            retryable=False,
            metadata={"skill": name, "params": args},
        )
        await _record_skill_log(user_id, skill, args, result)
        return result

    context = SkillContext(
        user_id=user_id,
        scene=scene,
        conversation_id=conversation_id,
        on_notify=on_notify,
    )
    try:
        result = await skill.execute(args, context)
        try:
            from app.core.observability import inc_skill_call

            inc_skill_call(name, result.success)
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("技能执行异常: {} | {}", name, exc)
        result = SkillResult(
            success=False,
            error=str(exc) or "技能执行失败",
            error_code="EXEC_ERROR",
            retryable=True,
            metadata={"skill": name},
        )
        try:
            from app.core.observability import inc_skill_call

            inc_skill_call(name, False)
        except Exception:  # noqa: BLE001
            pass
    await _record_skill_log(user_id, skill, args, result)
    return result


async def _record_skill_log(
    user_id: str,
    skill: Skill,
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
    # 混合架构：配置了 MCP 服务器时，客户端技能优先走 MCP（可插拔）；失败回退轮询
    if settings.MCP_SERVERS:
        mcp_result = await _try_mcp_tool(skill_name, params)
        if mcp_result is not None:
            return mcp_result
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


async def _try_mcp_tool(skill_name: str, params: dict) -> SkillResult | None:
    """尝试通过 MCP 调用客户端技能；失败返回 None（走轮询兜底）."""
    try:
        from app.agents.mcp.manager import call_tool

        servers = settings.MCP_SERVERS or []
        if not servers:
            return None
        for server in servers:
            res = await call_tool(server.get("name", ""), skill_name, params)
            if res is None or not res.get("success"):
                continue
            return SkillResult(
                success=not bool(res.get("is_error")),
                output=str(res.get("content") or ""),
                metadata=res.get("metadata") or {},
            )
        return None
    except Exception:  # noqa: BLE001
        return None


async def run_skill_loop(
    llm,
    user_id: str,
    messages: list[dict],
    scene: str = "chat",
    conversation_id: str = "",
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    on_text=None,
    on_progress=None,
) -> tuple[str, list[dict], list[dict]]:
    """技能调用主循环.

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
    # 场景只负责安全策略，不再区分“普通对话工具”和“智能体任务工具”。
    # office 步骤可见全部 office/system Skill，以及动态发现的 MCP 工具。
    tool_scene = scene
    tools = await get_tools_for_scene(tool_scene)
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
                tc, user_id, scene, conversation_id, on_notify=emit_progress
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
                    "content": result.output or (result.error or ""),
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

    return final_text, records, citations
