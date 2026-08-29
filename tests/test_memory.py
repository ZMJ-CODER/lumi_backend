# -*- coding: utf-8 -*-
"""长期记忆确定性测试：抽取解析 / 隐私丢弃 / 混合检索 / 过期与取代 / 画像聚合.

LLM 与嵌入全部 mock，不依赖外部模型，CI 可跑。
"""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.db_models import Memory, MemoryProfile, User
from app.services.memory import extraction, profile as profile_svc, retrieval


def _vec(value: float = 0.1) -> list[float]:
    return [value] * settings.EMBEDDING_DIMENSION


@asynccontextmanager
async def _db():
    """每测试独立引擎（NullPool），避免跨事件循环复用连接池."""
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _make_user(db) -> uuid.UUID:
    uid = uuid.uuid4()
    db.add(
        User(
            id=uid,
            username="mem-test",
            account=f"mem-test-{uid.hex[:12]}@example.com",
            password_hash="x",
            role="user",
        )
    )
    await db.commit()
    return uid


async def _cleanup(db, uid: uuid.UUID) -> None:
    await db.execute(sa_delete(Memory).where(Memory.user_id == uid))
    await db.execute(sa_delete(MemoryProfile).where(MemoryProfile.user_id == uid))
    await db.execute(sa_delete(User).where(User.id == uid))
    await db.commit()


def test_extract_facts_and_drop_pii():
    """抽取：正常事实落库，PII 事实被丢弃."""
    extract_payload = [
        {
            "fact": "用户最爱的编程语言是 Python",
            "memory_type": "preference",
            "privacy": "normal",
            "confidence": 0.95,
            "importance": 0.8,
            "privacy_reason": "",
            "placeholder": "",
        },
        {
            "fact": "用户的手机号是 13800138000",
            "memory_type": "identity",
            "privacy": "pii",
            "confidence": 0.99,
            "importance": 0.9,
            "privacy_reason": "phone",
            "placeholder": "",
        },
    ]

    async def run():
        async with _db() as db:
            uid = await _make_user(db)
            try:
                with (
                    patch.object(extraction, "_chat_turbo", new=AsyncMock(return_value=json.dumps(extract_payload))),
                    patch.object(extraction, "embed_texts", new=AsyncMock(return_value=[_vec(0.5) for _ in extract_payload])),
                ):
                    count = await extraction.extract_memories_from_dialog(
                        db,
                        str(uid),
                        "self-test-conv",
                        [
                            {"role": "user", "content": "我最爱的编程语言是 Python"},
                            {"role": "assistant", "content": "好的，记住啦"},
                        ],
                    )
                rows = (
                    await db.execute(
                        select(Memory).where(Memory.user_id == uid)
                    )
                ).scalars().all()
                return count, [r.fact for r in rows]
            finally:
                await _cleanup(db, uid)

    count, facts = asyncio.run(run())
    assert count == 1
    assert any("Python" in f for f in facts)
    assert not any("13800138000" in f for f in facts)


def test_extracted_memories_receive_type_specific_expiry():
    """抽取落库时，有限生命周期记忆写入绝对到期时间。"""
    extract_payload = [
        {
            "fact": "用户姓名是测试用户",
            "memory_type": "identity",
            "privacy": "normal",
            "confidence": 0.95,
            "importance": 0.8,
        },
        {
            "fact": "用户偏好深色主题",
            "memory_type": "preference",
            "privacy": "normal",
            "confidence": 0.95,
            "importance": 0.8,
        },
        {
            "fact": "用户上周完成了项目复盘",
            "memory_type": "experience",
            "privacy": "normal",
            "confidence": 0.95,
            "importance": 0.8,
        },
        {
            "fact": "用户计划在年底前完成认证",
            "memory_type": "goal",
            "privacy": "normal",
            "confidence": 0.95,
            "importance": 0.8,
        },
    ]

    async def run():
        async with _db() as db:
            uid = await _make_user(db)
            try:
                with (
                    patch.object(extraction, "_chat_turbo", new=AsyncMock(return_value=json.dumps(extract_payload))),
                    patch.object(extraction, "embed_texts", new=AsyncMock(return_value=[_vec(0.5)] * len(extract_payload))),
                ):
                    count = await extraction.extract_memories_from_dialog(
                        db,
                        str(uid),
                        "self-test-conv",
                        [{"role": "user", "content": "请记住我的偏好、经历和目标"}],
                    )
                rows = (
                    await db.execute(select(Memory).where(Memory.user_id == uid))
                ).scalars().all()
                return count, {row.memory_type: row for row in rows}
            finally:
                await _cleanup(db, uid)

    count, memories = asyncio.run(run())
    assert count == 4
    assert memories["identity"].expire_at is None
    for memory_type, days in (("preference", 90), ("experience", 45), ("goal", 180)):
        memory = memories[memory_type]
        assert memory.expire_at is not None
        assert memory.created_at is not None
        assert abs((memory.expire_at - memory.created_at).total_seconds() - timedelta(days=days).total_seconds()) < 1


def test_hybrid_retrieval_and_expiry():
    """混合检索：相关事实命中、无关/过期/已取代的事实不返回."""

    async def run():
        async with _db() as db:
            uid = await _make_user(db)
            try:
                now = datetime.now(timezone.utc)
                db.add_all(
                    [
                        Memory(user_id=uid, fact="用户喜欢吃火锅", memory_type="preference", privacy_level=0, embedding=_vec(0.9), importance=0.8),
                        Memory(user_id=uid, fact="用户喜欢打羽毛球", memory_type="preference", privacy_level=0, embedding=_vec(0.2), importance=0.5),
                        Memory(user_id=uid, fact="用户喜欢吃寿司", memory_type="preference", privacy_level=0, embedding=_vec(0.1), importance=0.5, is_deleted=True),
                        Memory(user_id=uid, fact="过期的旧偏好", memory_type="preference", privacy_level=0, embedding=_vec(0.3), importance=0.5, expire_at=now - timedelta(hours=1)),
                    ]
                )
                await db.commit()
                with patch.object(retrieval, "embed_query", new=AsyncMock(return_value=_vec(0.9))):
                    items = await retrieval.search_user_memories(db, str(uid), "想吃火锅", top_k=10)
                return [i["fact"] for i in items]
            finally:
                await _cleanup(db, uid)

    facts = asyncio.run(run())
    assert any("火锅" in f for f in facts)
    assert not any("寿司" in f for f in facts)      # 已取代（软删除）
    assert not any("过期" in f for f in facts)       # 已过期


def test_profile_build():
    """画像聚合：基于活跃事实生成结构化画像，版本递增."""
    profile_json = (
        '{"identity": {"名字": "张三"}, "preferences": ["Python", "火锅"],'
        ' "goals": [], "privacy": []}'
    )

    async def run():
        async with _db() as db:
            uid = await _make_user(db)
            try:
                db.add(
                    Memory(user_id=uid, fact="用户叫张三", memory_type="identity", privacy_level=0, embedding=_vec(0.4), importance=0.9)
                )
                db.add(
                    Memory(user_id=uid, fact="用户喜欢 Python", memory_type="preference", privacy_level=0, embedding=_vec(0.4), importance=0.7)
                )
                await db.commit()
                with patch.object(profile_svc, "_chat_turbo", new=AsyncMock(return_value=profile_json)):
                    p1 = await profile_svc.build_user_profile(db, str(uid))
                    await db.commit()
                    v1 = p1.version
                    p2 = await profile_svc.build_user_profile(db, str(uid))
                    await db.commit()
                v2 = p2.version
                return p1, v1, v2
            finally:
                await _cleanup(db, uid)

    p1, v1, v2 = asyncio.run(run())
    assert p1 is not None and p1.profile["identity"].get("名字") == "张三"
    assert "Python" in p1.profile["preferences"]
    assert v2 == v1 + 1
