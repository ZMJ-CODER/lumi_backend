"""用户偏好服务 —— 业务层读写（技能/编排里不直接碰 API）. """

from __future__ import annotations

import uuid

from loguru import logger


def _uid(user_id: str):
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return None


async def get_email_client(user_id: str) -> str:
    """读取用户默认邮件客户端（空 = 系统默认）. """
    uid = _uid(user_id)
    if uid is None:
        return ""
    try:
        from app.core.database import async_session_factory
        from app.models.db_models import UserPreference

        async with async_session_factory() as db:
            pref = await db.get(UserPreference, uid)
            return str(pref.email_client or "").strip() if pref else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Prefs] 读取邮件客户端偏好失败: {}", exc)
        return ""


async def set_email_client(user_id: str, client: str) -> str:
    """保存用户默认邮件客户端（仅保存识别出的客户端名；空 = 系统默认）. """
    uid = _uid(user_id)
    if uid is None:
        return ""
    client = (client or "").strip().lower()[:32]
    if not client:
        return ""
    try:
        from app.core.database import async_session_factory
        from app.models.db_models import UserPreference

        async with async_session_factory() as db:
            pref = await db.get(UserPreference, uid)
            if pref is None:
                pref = UserPreference(user_id=uid)
                db.add(pref)
            pref.email_client = client
            await db.commit()
        return client
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Prefs] 保存邮件客户端偏好失败: {}", exc)
        return client
