"""L1 隐私解密门：意图判定（关键词预筛 + LLM 二次确认）+ 审计落库.

见 docs/MEMORY_DESIGN.md §3.4/§6：L1 默认只注入占位符，
仅当用户明确要求且话题在白名单内时才解密注入，每次解密写 control_logs 审计。
"""

import json
import re
import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import decrypt_memory_text, hash_memory_text
from app.core.llm import LLMClient
from app.models.db_models import ControlLog, Memory
from app.services.usage import CATEGORY_PRIVACY_CONFIRM

# 可被用户显式请求解密的 L1 话题（健康类白名单外，永不自动解密）
DECRYPT_WHITELIST_TOPICS = {"财务", "家庭", "社交", "出行", "习惯"}

_ASK_RE = re.compile(r"告诉|说下|说说|查一下|查查|多少|是什么|我的|查看", re.IGNORECASE)

DECRYPT_CONFIRM_PROMPT = """你是隐私访问审核助手。判断用户消息是否在明确要求查看某条隐私信息。
已知隐私项（均为脱敏描述）：
{items}

用户消息：{message}

只输出 JSON：{{"allow": true/false, "memory_ids": ["命中的隐私项 id"]}}
规则：只有用户明确要求"告诉我/查看/查一下"对应隐私内容时才允许；闲聊、隐喻、无关问题一律拒绝。
只输出 JSON。
"""


def _topic_of_placeholder(placeholder: str) -> str:
    """从占位符 `[L1][话题] 描述` 中提取话题标签."""
    m = re.search(r"\[L1\]\[([^\]]+)\]", placeholder or "")
    return m.group(1) if m else ""


def _keyword_hit(message: str, placeholder: str) -> bool:
    """关键词预筛：消息含索取类词，且话题词出现在消息中."""
    if not _ASK_RE.search(message or ""):
        return False
    topic = _topic_of_placeholder(placeholder)
    return bool(topic) and topic in (message or "")


async def _llm_confirm(
    candidates: list[Memory], message: str, user_id: str | None = None
) -> list[Memory]:
    """LLM 二次确认：避免把闲聊误判为隐私索取."""
    items = "\n".join(f"- {m.fact} (id={m.id})" for m in candidates)
    try:
        raw = await LLMClient().chat(
            [
                {"role": "system", "content": DECRYPT_CONFIRM_PROMPT.format(items=items, message=message)},
                {"role": "user", "content": "请审核。"},
            ],
            base_url=settings.QWEN_BASE_URL,
            api_key=settings.QWEN_API_KEY,
            model=settings.MEMORY_EXTRACTION_MODEL,
            max_tokens=256,
            temperature=0,
            usage_user_id=user_id,
            usage_category=CATEGORY_PRIVACY_CONFIRM,
            disable_reasoning_effort=True,
        )
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        data = json.loads(raw)
        if not data.get("allow"):
            return []
        allowed = {str(x) for x in data.get("memory_ids", [])}
        return [m for m in candidates if str(m.id) in allowed]
    except Exception as exc:  # noqa: BLE001
        logger.warning("隐私确认 LLM 调用失败，按拒绝处理: {}", exc)
        return []


async def resolve_decrypt_candidates(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    facts: list[dict],
    message: str,
) -> list[dict]:
    """从本轮召回事实中选出需要解密注入的 L1 记忆，并写审计日志.

    Returns:
        [{"memory_id": ..., "plaintext": ...}, ...]
    """
    if not settings.MEMORY_DECRYPT_ENABLED:
        return []
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return []
    candidate_ids = [f["memory_id"] for f in facts if f.get("privacy_level") == 1]
    if not candidate_ids:
        return []

    memories = (
        await session.execute(select(Memory).where(Memory.id.in_(candidate_ids)))
    ).scalars().all()
    pre = [
        m
        for m in memories
        if _keyword_hit(message, m.fact or "")
        and _topic_of_placeholder(m.fact or "") in DECRYPT_WHITELIST_TOPICS
    ]
    if not pre:
        return []

    if settings.MEMORY_DECRYPT_LLM_CONFIRM_ENABLED:
        pre = await _llm_confirm(pre, message, str(uid))

    results: list[dict] = []
    for m in pre:
        try:
            plaintext = decrypt_memory_text(m.fact_encrypted or "", str(uid), m.key_version or 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆解密失败: memory={} err={}", m.id, exc)
            continue
        session.add(
            ControlLog(
                user_id=uid,
                action="memory_decrypt",
                target=str(m.id),
                success=True,
                detail=json.dumps(
                    {
                        "conversation_id": conversation_id,
                        "policy": "user_request",
                        "memory_type": m.memory_type,
                        "request_hash": hash_memory_text(message),
                        "data_hash": hash_memory_text(plaintext),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        results.append({"memory_id": str(m.id), "plaintext": plaintext})
    if results:
        await session.commit()
        logger.debug("[Memory] L1 解密注入: user={} count={}", user_id, len(results))
    return results
