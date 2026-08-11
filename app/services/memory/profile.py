"""用户画像聚合：活跃事实 → qwen-turbo → memory_profile JSON.

见 docs/MEMORY_DESIGN.md §7：画像常驻注入，事实库按需召回。
"""

import json
import re
import uuid
from datetime import datetime, timezone

from httpx import AsyncClient
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import get_redis
from app.models.db_models import Memory, MemoryProfile

PROFILE_SYSTEM_PROMPT = """你是用户画像聚合助手。根据用户的长期记忆事实，生成用户画像 JSON：
{
  "identity": {"关键项": "值"},
  "preferences": ["偏好1", "偏好2"],
  "goals": [{"目标": "...", "状态": "进行中|已完成"}],
  "privacy": [{"占位": "脱敏描述", "level": 1}]
}
规则：
1. identity 只保留稳定背景（职业/城市/家庭等），不写具体隐私值；
2. privacy 只能放脱敏占位描述（如"健康类隐私 1 条"），不得出现具体内容；
3. 无内容的分组用空数组，不要编造。
只输出 JSON。
"""


def _strip_code_fence(text: str) -> str:
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())


async def _chat_turbo(system_prompt: str, user_content: str, max_tokens: int = 1024) -> str:
    async with AsyncClient(
        base_url=settings.QWEN_BASE_URL,
        headers={"Authorization": f"Bearer {settings.QWEN_API_KEY}"},
        timeout=120,
    ) as client:
        resp = await client.post(
            "/chat/completions",
            json={
                "model": settings.MEMORY_EXTRACTION_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
        )
        resp.raise_for_status()
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()


async def build_user_profile(session: AsyncSession, user_id: str) -> MemoryProfile | None:
    """聚合用户活跃事实生成画像并 upsert；无事实时返回 None."""
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        logger.warning("画像生成跳过：无效 user_id={}", user_id)
        return None

    facts = (
        await session.execute(
            select(Memory)
            .where(Memory.user_id == uid, Memory.is_deleted.is_(False))
            .order_by(Memory.importance.desc())
            .limit(100)
        )
    ).scalars().all()
    if not facts:
        return None

    lines = "\n".join(f"- [{m.memory_type}] {m.fact}" for m in facts)
    try:
        raw = await _chat_turbo(PROFILE_SYSTEM_PROMPT, f"用户记忆：\n{lines}")
        data = json.loads(_strip_code_fence(raw))
    except Exception as exc:  # noqa: BLE001
        logger.warning("画像生成 LLM 调用失败: {}", exc)
        return None

    profile = {
        "identity": data.get("identity") or {},
        "preferences": data.get("preferences") or [],
        "goals": data.get("goals") or [],
        "privacy": data.get("privacy") or [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    existing = await session.get(MemoryProfile, uid)
    if existing:
        existing.profile = profile
        existing.version = (existing.version or 1) + 1
        result = existing
    else:
        result = MemoryProfile(user_id=uid, profile=profile, version=1)
        session.add(result)
    await session.commit()

    try:
        r = get_redis()
        await r.delete(f"mem:user:{uid}")
    except Exception:  # noqa: BLE001
        pass
    logger.debug("[Memory] 画像生成完成: user={} version={}", user_id, result.version)
    return result
