"""办公技能公共工具：LLM 调用封装 + 合规敏感词表.

放在 app/services 下保证插件文件（按独立模块加载）可正常导入。
"""

import time

from app.agents.skills.base import SkillContext
from app.core.agent_security import UNTRUSTED_CONTENT_RULES
from app.core.llm import LLMClient
from app.services.response_format import OFFICE_RESPONSE_FORMAT_COMPACT
from app.services.usage import CATEGORY_SKILL


async def _emit_output(context: SkillContext | None, text: str) -> None:
    callback = context.on_output if context else None
    if not callback or not text:
        return
    result = callback(text)
    if hasattr(result, "__await__"):
        await result


async def office_llm(
    context: SkillContext | None,
    system: str,
    user: str,
    *,
    max_tokens: int = 4000,
    temperature: float = 0.4,
    format_response: bool = True,
    stream: bool = False,
) -> str:
    """调用用户配置的办公模型生成文本（技能用途）.

    默认约束为桌面气泡友好的 Markdown，使单步骤任务不经最终汇总时也具备可读结构。
    需要严格原文/JSON 的 Skill 可显式关闭。
    """
    llm = LLMClient()
    if format_response:
        system = f"{system}\n\n{OFFICE_RESPONSE_FORMAT_COMPACT}"
    if context and context.skill_prompt:
        system = f"{system}\n\n[当前 Workflow Skill 的业务流程提示]\n{context.skill_prompt[:12000]}"
    system = f"{system}\n\n{UNTRUSTED_CONTENT_RULES}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    kwargs = {
        "scene": context.scene if context else "office",
        "max_tokens": max_tokens,
        "temperature": temperature,
        "usage_user_id": context.user_id if context else None,
        "usage_category": CATEGORY_SKILL,
        "disable_reasoning_effort": True,
        "api_key": context.llm_api_key if context else None,
        "llm_config": context.llm_config if context else None,
    }
    if not stream or not (context and context.on_output):
        return await llm.chat(messages, **kwargs)
    parts: list[str] = []
    # Keep a small tail buffered so internal route sentinels cannot leak one
    # token at a time through SSE before DirectLlmAgent classifies the result.
    pending = ""
    sentinel = "ROUTE_UPGRADE_"
    started = time.perf_counter()
    first_delta_at: float | None = None
    # Do not accidentally remove the caller's output budget while switching to
    # astream.  Previously chat_stream ignored max_tokens, which let a simple
    # writing request consume the provider default and appear to run forever.
    async for delta in llm.chat_stream(messages, **kwargs):
        if first_delta_at is None:
            first_delta_at = time.perf_counter()
            from loguru import logger

            logger.info(
                "办公文本流首字节: job={} latency_ms={}",
                str(context.job_id or context.conversation_id or "")[:8],
                int((first_delta_at - started) * 1000),
            )
        parts.append(delta)
        pending += delta
        if sentinel in pending:
            # The caller will convert this into a controlled reroute result;
            # never stream the control marker to the client.
            continue
        safe_len = max(0, len(pending) - len(sentinel) + 1)
        if safe_len:
            await _emit_output(context, pending[:safe_len])
            pending = pending[safe_len:]
    output = "".join(parts)
    if sentinel not in output and pending:
        await _emit_output(context, pending)
    from loguru import logger

    logger.info(
        "办公文本流完成: job={} duration_ms={} chars={}",
        str(context.job_id or context.conversation_id or "")[:8],
        int((time.perf_counter() - started) * 1000),
        len(output),
    )
    return output


# 合规审查：基础敏感词表（命中即提示，交由 LLM 结合上下文判定）
SENSITIVE_WORDS = [
    "赌博", "博彩", "色情", "裸聊", "毒品", "海洛因", "冰毒", "枪支", "弹药",
    "诈骗", "洗钱", "传销", "非法集资", "刷单", "代开发票", "假证", "黑客攻击",
    "木马", "病毒制作", "破解", "外挂", "翻墙", "境内外勾结", "颠覆", "邪教",
    "传谣", "造谣", "泄露国家秘密", "间谍", "恐怖", "自杀", "自残",
]
