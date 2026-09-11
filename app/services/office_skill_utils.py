"""办公技能公共工具：LLM 调用封装 + 合规敏感词表.

放在 app/services 下保证插件文件（按独立模块加载）可正常导入。
"""

import time

from app.agents.skills.base import SkillContext
from app.core.agent_security import UNTRUSTED_CONTENT_RULES
from app.core.llm import LLMClient
from app.services.response_format import OFFICE_RESPONSE_FORMAT_COMPACT
from app.services.usage import CATEGORY_SKILL

# 内部路由哨兵：属于执行控制数据，绝不允许作为答案文本进入用户可见输出。
# 定义只保留这一处，缓冲（本模块）与判定（DirectLlmAgent）必须引用同一个常量，
# 否则会出现"缓冲拦不住、判定认不出"的半截标记泄漏。
ROUTE_SENTINEL_PREFIX = "ROUTE_UPGRADE_"
# 模型实际输出的形态是 markdown 包裹的 ``[[ROUTE_UPGRADE_RAG]]``。缓冲判定要以这个
# 完整形态为基准，否则开头的 ``[[`` 会被当成普通正文先流出去。
ROUTE_MARKER_PATTERN = "[[ROUTE_UPGRADE_"


def possible_route_sentinel_start(text: str, sentinel: str) -> int:
    """返回缓冲区里"可能是一个路由标记开头"的最早位置（没有则 -1）。

    从尾巴往前扫：任何比 ``len(sentinel)`` 更早的位置都已经确定不可能是标记的开头。
    这样形如 ``[``、``[[``、``[[ROUTE_UP`` 的碎片会被拦住，而正常长文本依然立即流出。
    """
    value = str(text or "")
    marker = str(sentinel or "")
    if not value or not marker:
        return -1
    for start in range(max(0, len(value) - len(marker) + 1), len(value)):
        tail = value[start:]
        if len(tail) < len(marker) and marker.startswith(tail):
            return start
    return -1


def sentinel_prefix_tail_len(text: str, sentinel: str) -> int:
    """正文末尾有多少个字符是"某个更长标记前缀的尾巴"（最多 ``len(sentinel)-1``）。

    把这种"看起来像标记开头"的后缀一起扣住，用户才不会先看到 ``[[``。
    """
    value = str(text or "")
    marker = str(sentinel or "")
    if not value or not marker:
        return 0
    for length in range(min(len(value), len(marker) - 1), 0, -1):
        if marker.startswith(value[-length:]):
            return length
    return 0


async def _emit_output(context: SkillContext | None, text: str) -> None:
    callback = context.on_output if context else None
    if not callback or not text:
        return
    result = callback(text)
    if hasattr(result, "__await__"):
        await result


async def _emit_process(context: SkillContext | None, text: str) -> None:
    """Send model-side progress to the process channel, never to answer text."""
    callback = context.on_notify if context else None
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
    # Workflow skills also have a real streaming path.  It must use the same
    # protocol firewall as direct chat: DeepSeek-compatible providers may emit
    # full-width DSML and a ``tool_calls`` wrapper as plain text.  Passing those
    # chunks straight to office_stream makes the protocol visible in the user
    # bubble and appears as a blocked/non-streaming response.
    from lumi_orch.protocol import (
        ModelStreamProtocolParser,
        TextToolStripper,
        chunk_to_events,
        strip_tool_markup,
    )

    parts: list[str] = []
    # Keep a small tail buffered so internal route sentinels cannot leak one
    # token at a time through SSE before DirectLlmAgent classifies the result.
    pending = ""
    sentinel = ROUTE_SENTINEL_PREFIX
    marker = ROUTE_MARKER_PATTERN
    started = time.perf_counter()
    first_delta_at: float | None = None
    parser = ModelStreamProtocolParser()
    stripper = TextToolStripper()
    sentinel_seen = False
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
        # Normalize/strip provider protocol incrementally.  Only ``delta``
        # events reach the answer stream; tool/process events are internal.
        for piece in stripper.feed(str(delta or "")):
            process_prefix = stripper.drain_process()
            if process_prefix:
                await _emit_process(context, process_prefix)
            for chunk in parser.feed(piece):
                for event in chunk_to_events(chunk):
                    event_type = str(event.get("type") or "")
                    if event_type == "process":
                        await _emit_process(context, str(event.get("content") or ""))
                    elif event_type == "delta":
                        clean = strip_tool_markup(str(event.get("content") or ""))
                        if clean:
                            parts.append(clean)
                            pending += clean
        if sentinel in pending:
            # 一旦出现路由哨兵：丢弃已缓冲文本（哨兵前那一小段对用户没有意义），
            # 且不再发出任何 delta，也不再继续消费 provider。调用方会把它转换成
            # 受控的重新路由结果。
            pending = ""
            sentinel_seen = True
            break
        # 只放出"确定不可能是路由标记开头"的部分：从缓冲区尾部往前找可能成为标记
        # 前缀的位置，之前的内容立即流出。否则形如 "[["、"[[ROUTE_UP" 的碎片会先于
        # 完整标记被流式发出（用户会在回答里看到半个标记）。
        marker_start = possible_route_sentinel_start(pending, marker)
        safe_len = len(pending) if marker_start < 0 else marker_start
        # 尾巴上"像标记开头"的短后缀（"[[" 这类）一并扣住。
        safe_len = max(0, safe_len - sentinel_prefix_tail_len(pending[:safe_len], marker))
        if safe_len:
            await _emit_output(context, pending[:safe_len])
            pending = pending[safe_len:]
    # Flush both protocol layers at end-of-stream.  Any incomplete DSML is
    # discarded by the firewall rather than being returned as answer text.
    for piece in stripper.flush():
        process_prefix = stripper.drain_process()
        if process_prefix:
            await _emit_process(context, process_prefix)
        for chunk in parser.feed(piece):
            for event in chunk_to_events(chunk):
                if event.get("type") == "delta":
                    clean = strip_tool_markup(str(event.get("content") or ""))
                    if clean:
                        parts.append(clean)
                        pending += clean
                elif event.get("type") == "process":
                    await _emit_process(context, str(event.get("content") or ""))
    for chunk in parser.finalize():
        for event in chunk_to_events(chunk):
            if event.get("type") == "delta":
                clean = strip_tool_markup(str(event.get("content") or ""))
                if clean:
                    parts.append(clean)
                    pending += clean
            elif event.get("type") == "process":
                await _emit_process(context, str(event.get("content") or ""))
    output = "".join(parts)
    if not sentinel_seen and sentinel not in output and pending:
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
