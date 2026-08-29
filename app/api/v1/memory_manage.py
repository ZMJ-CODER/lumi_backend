"""用户记忆管理 API —— 用户可查看/检索/删除自己的长期记忆与画像.

与 /admin/memories（仅超管）不同：这里只操作当前登录用户自己的数据。
用途：让用户"看得见"AI 记住了什么，方便验证/排查记忆链路，也支持主动遗忘。
"""

import time
import uuid

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.redis import get_redis
from app.core.read_view_cache import ReadViewTimer, get_read_view, invalidate_memory_view, memory_view_key, set_read_view
from app.models.db_models import Memory, MemoryProfile
from app.services.memory.retrieval import search_user_memories

router = APIRouter()

MEMORY_CACHE_KEY = "mem:user:{user_id}"  # 与 orchestrator 保持一致


def _uid(payload: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload["sub"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise BadRequestException("无效的用户身份") from exc


def _memory_dict(m: Memory) -> dict:
    return {
        "memory_id": str(m.id),
        "fact": m.fact,  # L1 为脱敏占位符，不含密文
        "memory_type": m.memory_type,
        "privacy_level": m.privacy_level,
        "importance": m.importance,
        "confidence": m.confidence,
        "access_count": m.access_count,
        "is_deleted": m.is_deleted,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "expire_at": m.expire_at.isoformat() if m.expire_at else None,
    }


async def _invalidate_profile_cache(uid: uuid.UUID) -> None:
    try:
        r = get_redis()
        await r.delete(MEMORY_CACHE_KEY.format(user_id=str(uid)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("清理画像缓存失败: {}", exc)
    await invalidate_memory_view(str(uid))


@router.get("")
async def get_my_memory(payload: dict = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """查看我的长期记忆画像 + 活跃事实列表."""
    uid = _uid(payload)
    endpoint = "memory_view"
    timer = ReadViewTimer(endpoint)
    cache_key = memory_view_key(str(uid))
    cached = await get_read_view(cache_key, endpoint=endpoint, timer=timer)
    if cached is not None:
        return cached

    await timer.checkout(db)
    profile = await timer.query(db.get(MemoryProfile, uid))
    total = (
        await timer.query(
            db.execute(
                select(func.count())
                .select_from(Memory)
                .where(Memory.user_id == uid, Memory.is_deleted.is_(False))
            )
        )
    ).scalar_one()
    facts = (
        (
            await timer.query(
                db.execute(
                    select(Memory)
                    .where(Memory.user_id == uid, Memory.is_deleted.is_(False))
                    .order_by(Memory.importance.desc(), Memory.created_at.desc())
                    .limit(200)
                )
            )
        )
        .scalars()
        .all()
    )
    response_started = time.perf_counter()
    response = {
        "code": 0,
        "data": {
            "profile": {
                "content": (profile.profile if profile else {}),
                "version": profile.version if profile else 0,
                "updated_at": profile.updated_at.isoformat() if profile and profile.updated_at else None,
            },
            "facts": [_memory_dict(m) for m in facts],
            "total": total,
        },
    }
    timer.observe("response_build", response_started)
    await set_read_view(
        cache_key,
        response,
        endpoint=endpoint,
        ttl_seconds=settings.READ_VIEW_MEMORY_TTL_SECONDS,
        timer=timer,
    )
    return response


@router.get("/search")
async def search_my_memory(
    q: str = Query(..., min_length=1, description="检索问题/关键词，模拟对话中的记忆召回"),
    k: int = Query(default=10, ge=1, le=50),
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """模拟召回：输入一个问题，返回当前会命中/注入哪些记忆."""
    uid = _uid(payload)
    items = await search_user_memories(db, str(uid), q, top_k=k)
    return {"code": 0, "data": {"query": q, "items": items}}


@router.delete("/{memory_id}")
async def delete_my_memory(
    memory_id: str,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除一条自己的记忆（物理删除）."""
    uid = _uid(payload)
    try:
        mid = uuid.UUID(memory_id)
    except (ValueError, TypeError) as exc:
        raise BadRequestException("memory_id 无效") from exc
    mem = await db.get(Memory, mid)
    if not mem or mem.user_id != uid:
        raise NotFoundException("记忆不存在")
    await db.delete(mem)
    await db.commit()
    await _invalidate_profile_cache(uid)
    return {"code": 0, "data": {"deleted": memory_id}}


@router.delete("")
async def clear_my_memory(payload: dict = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """清空我的全部记忆与画像."""
    uid = _uid(payload)
    await db.execute(sa_delete(Memory).where(Memory.user_id == uid))
    await db.execute(sa_delete(MemoryProfile).where(MemoryProfile.user_id == uid))
    await db.commit()
    await _invalidate_profile_cache(uid)
    return {"code": 0, "data": {"cleared": True}}


@router.post("/rebuild")
async def rebuild_my_profile(payload: dict = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    """手动重建我的用户画像（聚合活跃事实）."""
    uid = _uid(payload)
    from app.services.memory.profile import build_user_profile

    profile = await build_user_profile(db, str(uid))
    await db.commit()
    await _invalidate_profile_cache(uid)
    return {
        "code": 0,
        "data": {
            "profile": (profile.profile if profile else {}),
            "version": profile.version if profile else 0,
            "updated_at": profile.updated_at.isoformat() if profile and profile.updated_at else None,
        },
    }


@router.post("/self-test")
async def run_memory_self_test(
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """一键自检记忆链路：真实 抽取 → 召回 → 注入 → 问答，返回每步结果并清理测试数据."""
    uid = _uid(payload)
    fact_key = "LumiTestLang"
    test_sentence = f"我最喜欢的测试编程语言是 {fact_key}，因为它很好记。"
    report: list[dict] = []
    items: list[dict] = []

    try:
        # 1. 抽取链路（真实 LLM + 嵌入）
        try:
            from app.services.memory.extraction import extract_memories_from_dialog

            count = await extract_memories_from_dialog(
                db,
                str(uid),
                "memory-self-test",
                [
                    {"role": "user", "content": test_sentence},
                    {"role": "assistant", "content": "好的，记住了。"},
                ],
            )
            rows = (
                await db.execute(
                    select(Memory).where(Memory.user_id == uid, Memory.fact.like(f"%{fact_key}%"))
                )
            ).scalars().all()
            hit = any(fact_key in (m.fact or "") for m in rows)
            report.append({"step": "抽取", "ok": hit, "detail": f"新增 {count} 条，命中测试事实: {hit}"})
        except Exception as exc:  # noqa: BLE001
            report.append({"step": "抽取", "ok": False, "detail": str(exc)[:200]})

        # 2. 兜底直插（抽取未命中时仍可验证后续链路）
        existing = (
            await db.execute(
                select(Memory).where(Memory.user_id == uid, Memory.fact.like(f"%{fact_key}%"))
            )
        ).scalars().all()
        if not existing:
            try:
                from app.services.rag.embeddings import embed_texts
                from app.services.memory.lifecycle import expire_at_for_memory_type

                vec = (await embed_texts([f"用户喜欢的测试语言是 {fact_key}"]))[0]
                mem = Memory(
                    user_id=uid,
                    fact=f"用户喜欢的测试语言是 {fact_key}",
                    memory_type="preference",
                    privacy_level=0,
                    embedding=vec,
                    importance=0.8,
                    confidence=0.99,
                    expire_at=expire_at_for_memory_type("preference"),
                )
                db.add(mem)
                await db.commit()
                await _invalidate_profile_cache(uid)
                report.append({"step": "兜底直插", "ok": True, "detail": "抽取未命中，已直插测试事实"})
            except Exception as exc:  # noqa: BLE001
                report.append({"step": "兜底直插", "ok": False, "detail": str(exc)[:200]})

        # 3. 召回链路（真实混合检索）
        try:
            items = await search_user_memories(db, str(uid), f"我喜欢什么测试语言 {fact_key}", top_k=5)
            hit = any(fact_key in (i.get("fact") or "") for i in items)
            report.append({"step": "召回", "ok": hit, "detail": f"命中 {hit}，返回 {len(items)} 条"})
        except Exception as exc:  # noqa: BLE001
            report.append({"step": "召回", "ok": False, "detail": str(exc)[:200]})

        # 4. 注入链路（画像 + 按需事实）
        try:
            from app.services.orchestrator import orchestrator

            profile, facts = await orchestrator.get_memory_context(
                str(uid), query=f"测试语言 {fact_key}"
            )
            hit = any(fact_key in (f.get("fact") or "") for f in facts)
            report.append({"step": "注入", "ok": hit, "detail": f"命中 {hit}，画像{'有' if profile else '无'}"})
        except Exception as exc:  # noqa: BLE001
            report.append({"step": "注入", "ok": False, "detail": str(exc)[:200]})

        # 5. 问答链路（真实 LLM）
        try:
            from app.core.llm import LLMClient

            memory_line = ", ".join(str(i.get("fact")) for i in items if fact_key in (i.get("fact") or ""))
            reply = await LLMClient().chat(
                [
                    {"role": "system", "content": "你是记忆验证助手，根据给出的记忆直接、简短地回答。"},
                    {
                        "role": "user",
                        "content": f"用户记忆里有：{memory_line or '（无）'}\n问题：用户提到过的测试编程语言叫什么？",
                    },
                ],
                scene="chat",
                max_tokens=64,
                temperature=0,
            )
            hit = fact_key.lower() in (reply or "").lower()
            report.append({"step": "问答", "ok": hit, "detail": f"命中 {hit}：{(reply or '')[:100]}"})
        except Exception as exc:  # noqa: BLE001
            report.append({"step": "问答", "ok": False, "detail": str(exc)[:200]})

        passed = all(r.get("ok") for r in report)
        return {"code": 0, "data": {"passed": passed, "report": report}}
    finally:
        # 清理测试产生的记忆（按测试事实关键词）
        try:
            await db.execute(
                sa_delete(Memory).where(
                    Memory.user_id == uid, Memory.fact.like(f"%{fact_key}%")
                )
            )
            await db.commit()
        except Exception:  # noqa: BLE001
            pass
        await _invalidate_profile_cache(uid)
