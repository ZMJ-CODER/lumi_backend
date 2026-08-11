"""长期记忆抽取管线：LLM 抽取 → 隐私分级 → 去重/合并/矛盾处理 → 落库.

数据流（见 docs/MEMORY_DESIGN.md §5）:
  对话消息 → qwen-turbo 结构化抽取 → PII 正则兜底
  → L0 明文 / L1 加密 + 占位符 / L2 丢弃
  → 与已有记忆比对（embedding 余弦 + LLM 判定）
  → 插入 / 强化 / supersede → memories 表
"""

import json
import re
import uuid
from typing import Literal

from httpx import AsyncClient
from loguru import logger
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.crypto import encrypt_memory_text
from app.core.redis import get_redis
from app.models.db_models import Memory
from app.services.rag.embeddings import embed_texts

# ── L2 PII 正则兜底（命中即不落库）──
PII_PATTERNS: dict[str, re.Pattern] = {
    "phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "bank_card": re.compile(r"(?<!\d)\d{16,19}(?!\d)"),
}

EXTRACT_SYSTEM_PROMPT = """你是记忆抽取助手。从对话中提取关于用户的长期有效事实。
规则：
1. 只抽取对后续对话有复用价值的信息，忽略寒暄、即时性提问与一次性请求；
2. 输出 JSON 数组，元素格式：
   {"memory_type": "identity|preference|experience|goal",
    "fact": "一句话事实，用'用户'作主语",
    "importance": 0~1, "confidence": 0~1,
    "privacy": "normal|sensitive|pii",
    "privacy_reason": "判定理由（privacy 非 normal 时必填）",
    "topic_tag": "健康|财务|家庭|社交|出行|习惯（仅 sensitive 时填写）",
    "placeholder": "脱敏描述，不含具体值（仅 sensitive 时填写）"}
3. privacy=pii（身份证/手机/邮箱/银行卡/精确住址等可直接定位个人的信息）时，fact 置空；
4. privacy=sensitive（健康/财务/家庭等私密但单条不足以精确定位）时，placeholder 用占位描述代替具体值；
5. 健康类信息（疾病、慢性病、体检异常、用药等）必须标记为 sensitive；
6. 只输出 JSON，不要任何解释文字。
"""

MERGE_SYSTEM_PROMPT = """你是记忆合并助手。对比"新事实"与"已有记忆"，判断关系并输出 JSON：
{"action": "duplicate|supplement|contradiction|new", "merged_fact": "合并后的一句话事实"}
- duplicate：语义重复，保留已有即可；
- supplement：新事实补充/完善了已有记忆 → merged_fact 为合并后的完整表述；
- contradiction：两者矛盾，新事实更新旧事实 → merged_fact 为新事实；
- new：无关 → merged_fact 为空。
只输出 JSON。
"""


class ExtractedFact(BaseModel):
    """LLM 抽取的单条事实（结构化输出）."""

    memory_type: Literal["identity", "preference", "experience", "goal"]
    fact: str
    importance: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    privacy: Literal["normal", "sensitive", "pii"]
    privacy_reason: str = ""
    topic_tag: str = ""
    placeholder: str = ""


class MergeResult(BaseModel):
    """去重/合并判定结果."""

    action: Literal["duplicate", "supplement", "contradiction", "new"]
    merged_fact: str = ""


def _looks_like_pii(text: str) -> str | None:
    """正则兜底：返回命中的 PII 类型（无则 None）."""
    for name, pattern in PII_PATTERNS.items():
        if pattern.search(text or ""):
            return name
    return None


def _strip_code_fence(text: str) -> str:
    """去掉 LLM 输出可能带的 ```json 围栏."""
    return re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())


async def _chat_turbo(system_prompt: str, user_content: str, max_tokens: int = 2048) -> str:
    """调用轻量模型（默认 qwen-turbo，与编排器摘要同一模式）."""
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
        data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()


def _parse_facts(raw: str) -> list[ExtractedFact]:
    """解析 LLM 输出为 ExtractedFact 列表（失败返回空，不阻塞抽取）."""
    text = _strip_code_fence(raw)
    payload = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.S)
        if m:
            try:
                payload = json.loads(m.group(0))
            except json.JSONDecodeError:
                payload = None
    if not isinstance(payload, list):
        return []
    facts: list[ExtractedFact] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            facts.append(ExtractedFact(**item))
        except ValidationError:
            continue
    return facts


def _build_dialog(messages: list[dict]) -> str:
    """消息列表 → 对话文本（用户/助手交替，限制长度）."""
    parts: list[str] = []
    total = 0
    for m in messages:
        role = "用户" if m.get("role") == "user" else "助手"
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        line = f"{role}: {content}"
        total += len(line)
        if total > settings.MEMORY_EXTRACTION_MAX_DIALOG_CHARS:
            break
        parts.append(line)
    return "\n".join(parts)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（嵌入已 L2 归一化，点积即余弦）."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b)))


async def _existing_active_memories(session: AsyncSession, uid: uuid.UUID) -> list[Memory]:
    stmt = (
        select(Memory)
        .where(
            Memory.user_id == uid,
            Memory.is_deleted.is_(False),
            Memory.embedding.isnot(None),
        )
        .order_by(Memory.importance.desc())
        .limit(200)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _merge_with_existing(
    session: AsyncSession,
    uid: uuid.UUID,
    fact: ExtractedFact,
    embedding: list[float],
) -> tuple[str, str, Memory | None]:
    """与已有记忆比对。返回 (action, merged_fact, candidate)."""
    existing = await _existing_active_memories(session, uid)
    candidates = [
        m for m in existing if _cosine(m.embedding or [], embedding) >= settings.MEMORY_SIMILARITY_THRESHOLD
    ]
    if not candidates:
        return "new", fact.fact, None
    candidate = candidates[0]
    candidate_lines = "\n".join(f"- {m.fact}" for m in candidates[:3])
    user_content = f"新事实：{fact.fact}\n\n已有记忆：\n{candidate_lines}"
    try:
        raw = await _chat_turbo(MERGE_SYSTEM_PROMPT, user_content, max_tokens=512)
        result = MergeResult(**json.loads(_strip_code_fence(raw)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆合并判定失败，按 new 处理: {}", exc)
        return "new", fact.fact, candidate
    return result.action, result.merged_fact, candidate


async def extract_memories_from_dialog(
    session: AsyncSession,
    user_id: str,
    conversation_id: str,
    messages: list[dict],
) -> int:
    """从一段对话批量抽取并落库长期记忆。返回新增事实数."""
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        logger.warning("记忆抽取跳过：无效 user_id={}", user_id)
        return 0
    try:
        cid = uuid.UUID(str(conversation_id))
    except (ValueError, TypeError):
        cid = None

    dialog = _build_dialog(messages)
    if not dialog.strip():
        return 0

    try:
        raw = await _chat_turbo(
            EXTRACT_SYSTEM_PROMPT,
            f"对话内容：\n\n{dialog}",
            max_tokens=settings.MEMORY_EXTRACTION_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("记忆抽取 LLM 调用失败: {}", exc)
        return 0

    facts = _parse_facts(raw)
    if not facts:
        logger.debug("[Memory] 抽取未产出有效事实: user={}", user_id)
        return 0

    inserted = 0
    for f in facts:
        # L2：PII 直接丢弃（不落库、不进向量）
        pii_type = _looks_like_pii(f.fact)
        if f.privacy == "pii" or pii_type:
            logger.info(
                "[Memory] 丢弃 PII 事实 type={} reason={}",
                pii_type or f.privacy,
                f.privacy_reason,
            )
            continue
        # 低置信度：宁缺毋滥（用户不可自管理）
        if f.confidence < settings.MEMORY_EXTRACTION_MIN_CONFIDENCE:
            continue

        if f.privacy == "sensitive":
            if not f.placeholder.strip():
                continue
            encrypt_blob, key_version = encrypt_memory_text(f.fact, str(uid))
            inject_text = f.placeholder.strip()
            indexable = f.placeholder.strip()
            privacy_level = 1
        else:
            encrypt_blob, key_version, indexable = None, settings.MEMORY_ENCRYPTION_KEY_VERSION, None
            inject_text = f.fact.strip()
            privacy_level = 0

        if not inject_text:
            continue

        try:
            embedding = (await embed_texts([indexable or inject_text]))[0]
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆向量化失败，跳过该事实: {}", exc)
            continue

        action, merged_fact, candidate = await _merge_with_existing(session, uid, f, embedding)

        if action == "duplicate" and candidate:
            # 重复：强化已有记忆（访问次数 + 重要度取高），不重复插入
            candidate.access_count = (candidate.access_count or 0) + 1
            if f.importance > (candidate.importance or 0):
                candidate.importance = f.importance
            continue

        if action in ("supplement", "contradiction") and candidate:
            # 补充/矛盾：旧事实软删除（superseded_by 回填），插入新事实
            candidate.is_deleted = True
            text_to_store = (merged_fact or inject_text).strip()
            if f.privacy == "sensitive" and merged_fact:
                encrypt_blob, key_version = encrypt_memory_text(merged_fact, str(uid))
                text_to_store = inject_text
        else:
            text_to_store = inject_text

        mem = Memory(
            user_id=uid,
            fact=text_to_store,
            fact_encrypted=encrypt_blob,
            fact_indexable=indexable,
            memory_type=f.memory_type,
            privacy_level=privacy_level,
            embedding=embedding,
            importance=f.importance,
            confidence=f.confidence,
            source_conversation_id=cid,
            key_version=key_version,
        )
        session.add(mem)
        if action in ("supplement", "contradiction") and candidate:
            await session.flush()
            candidate.superseded_by = mem.id
        inserted += 1

    await session.commit()
    # 失效用户记忆缓存（画像/缓存由阶段 3 使用）
    try:
        r = get_redis()
        await r.delete(f"mem:user:{uid}")
    except Exception:  # noqa: BLE001
        pass
    logger.debug("[Memory] 抽取完成: user={} conv={} new={}", user_id, conversation_id, inserted)
    return inserted
