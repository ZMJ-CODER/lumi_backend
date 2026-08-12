"""技能执行器 —— LLM function calling 循环 + 参数校验 + 审计.

职责:
  - 按场景过滤可用的技能（category/permission 治理入口）
  - 把技能列表转成 function calling 工具定义交给 LLM
  - 解析并执行 LLM 发出的工具调用，结果回填对话继续循环
  - 高危技能（requires_confirmation）执行前拦截，等待用户确认
  - 每次调用写审计日志（control_logs）
"""

import json
import uuid
from datetime import datetime, timezone

from loguru import logger

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.registry import SkillRegistry
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.db_models import ControlLog
from app.services import client_tools
from app.services.usage import CATEGORY_CHAT, CATEGORY_SKILL


def get_skills_for_scene(scene: str) -> list[Skill]:
    """按场景过滤技能（scenes 白名单；空 = 全场景）."""
    return [s for s in SkillRegistry.list() if s.supports_scene(scene)]


def skills_to_tools(scene: str) -> list[dict]:
    """场景内技能 → function calling 工具定义."""
    return [s.to_tool_definition() for s in get_skills_for_scene(scene)]


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
) -> SkillResult:
    """执行一次技能调用：校验 → 高危拦截 → 执行 → 审计."""
    fn = tool_call.get("function") or {}
    name = str(fn.get("name") or "")
    args = _parse_arguments(fn.get("arguments"))

    skill = SkillRegistry.get(name)
    if not skill:
        return SkillResult(
            success=False,
            error=f"技能不存在: {name}",
            error_code="SKILL_NOT_FOUND",
            retryable=False,
            metadata={"skill": name},
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("技能执行异常: {} | {}", name, exc)
        result = SkillResult(
            success=False,
            error=str(exc) or "技能执行失败",
            error_code="EXEC_ERROR",
            retryable=True,
            metadata={"skill": name},
        )
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
) -> SkillResult:
    """客户端技能通用执行：创建待执行请求 → 用户端轮询执行 → 等待结果（超时取消）.

    供 client 环境技能（本地文件/项目操作）复用；key 不经过服务端。
    """
    if not user_id:
        return SkillResult(
            success=False,
            error="该技能需要登录后使用",
            error_code="INVALID_ARGS",
            retryable=False,
        )
    req = await client_tools.create_client_tool_request(
        user_id, skill_name, params, requires_confirmation
    )
    if not req:
        return SkillResult(
            success=False,
            error="该技能需要登录后使用",
            error_code="INVALID_ARGS",
            retryable=False,
        )
    result = await client_tools.await_result(user_id, req["request_id"])
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


async def run_skill_loop(
    llm,
    user_id: str,
    messages: list[dict],
    scene: str = "chat",
    conversation_id: str = "",
    llm_api_key: str | None = None,
    on_text=None,
) -> tuple[str, list[dict], list[dict]]:
    """技能调用主循环.

    流程：LLM function calling 决定技能 → 执行 → 结果回填 → 再调 LLM，
    直到 LLM 不再请求技能（输出最终回复）或达到最大轮数。

    Args:
        llm: LLMClient 实例
        messages: 当前对话消息列表（最后一个为用户消息）
        llm_api_key: BYOK 用户本次请求临时携带的 API key（用完即弃，不落库）
        on_text: 可选回调，每轮 assistant 文本产出时调用（流式输出用）

    Returns:
        (final_text, records, citations)
        - final_text: 最终回复文本
        - records: 技能调用记录 [{skill, success, error_code}]
        - citations: 技能返回的引用列表（web_search / query_knowledge）
    """
    skills = get_skills_for_scene(scene)
    if not skills:
        return "", [], []
    tools = skills_to_tools(scene)
    max_rounds = settings.AGENT_SKILLS_MAX_ROUNDS
    records: list[dict] = []
    citations: list[dict] = []
    final_text = ""
    messages = list(messages)

    for _ in range(max_rounds):
        content, tool_calls = await llm.chat_with_tools(
            messages,
            tools,
            scene=scene,
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
            result = await execute_tool_call(tc, user_id, scene, conversation_id, on_notify=on_text)
            records.append(
                {
                    "skill": tc.get("function", {}).get("name"),
                    "success": result.success,
                    "error_code": result.error_code,
                }
            )
            if result.metadata.get("citations"):
                citations.extend(result.metadata["citations"])
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

    return final_text, records, citations
