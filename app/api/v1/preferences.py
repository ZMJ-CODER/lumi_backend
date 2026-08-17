"""用户个性化偏好 API —— 按用户隔离 + 多端同步.

覆盖：智能体头像 / 全局主题背景 / 回复风格 / 声音设置 / 用户保存的"方案"（角色方案、声音方案）。
所有数据按 user_id 存取：首次使用返回默认值，一个用户的个性化设置不会影响其他用户；
登录后在任意设备修改并保存，其他设备下次登录/启动时自动同步。
"""

import json
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.db_models import User, UserPreference, UserPreset
from app.models.user import PreferencesUpdateRequest, PresetCreateRequest
from app.services.prompts import get_prompt

router = APIRouter()

PRESET_LIMIT_PER_KIND = 20
_DEFAULT_VOICE = {
    "voice": "",
    "rate": 0,
    "pitch": 0,
    "referenceAudio": "",
    "referenceName": "",
}


def _uid(payload: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload["sub"]))
    except (ValueError, TypeError, KeyError) as exc:
        raise BadRequestException("无效的用户身份") from exc


def _decode_voice(raw: str | None) -> dict:
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, dict):
                return {**_DEFAULT_VOICE, **v}
        except (ValueError, TypeError):
            pass
    return dict(_DEFAULT_VOICE)


async def _get_pref(db: AsyncSession, uid: uuid.UUID) -> UserPreference | None:
    return await db.get(UserPreference, uid)


async def _prefs_dict(db: AsyncSession, uid: uuid.UUID) -> dict:
    """当前用户的完整偏好（无记录时返回默认值）."""
    user = await db.get(User, uid)
    pref = await _get_pref(db, uid)
    return {
        "avatar": (pref.avatar if pref and pref.avatar else ""),
        "background_image": (pref.background_image if pref and pref.background_image else ""),
        "reply_style": (pref.reply_style if pref and pref.reply_style else "long"),
        "email_client": (pref.email_client if pref and pref.email_client else ""),
        "voice": _decode_voice(pref.voice if pref else None),
        "prompt_id": (user.prompt_id or "") if user else "",
    }


async def _list_presets_db(
    db: AsyncSession, uid: uuid.UUID, kind: str | None
) -> list[dict]:
    stmt = select(UserPreset).where(UserPreset.user_id == uid)
    if kind:
        stmt = stmt.where(UserPreset.kind == kind)
    rows = (await db.execute(stmt.order_by(UserPreset.created_at.asc()))).scalars().all()
    return [
        {
            "preset_id": str(r.id),
            "kind": r.kind,
            "name": r.name,
            "payload": json.loads(r.payload) if r.payload else {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("")
async def get_preferences(payload: dict = Depends(require_auth)):
    """获取当前用户全部个性化偏好 + 方案列表."""
    uid = _uid(payload)
    async with async_session_factory() as db:
        data = await _prefs_dict(db, uid)
        presets = await _list_presets_db(db, uid, None)
    return {"code": 0, "data": {**data, "presets": presets}}


@router.put("")
async def update_preferences(
    req: PreferencesUpdateRequest, payload: dict = Depends(require_auth)
):
    """更新个性化偏好（部分字段可缺省；不存在则创建）."""
    uid = _uid(payload)
    async with async_session_factory() as db:
        pref = await _get_pref(db, uid)
        if pref is None:
            pref = UserPreference(user_id=uid)
            db.add(pref)
        if req.avatar is not None:
            pref.avatar = req.avatar or None
        if req.background_image is not None:
            pref.background_image = req.background_image or None
        if req.reply_style is not None:
            pref.reply_style = req.reply_style
        if req.email_client is not None:
            pref.email_client = (req.email_client or "").strip()[:32]
        if req.voice is not None:
            pref.voice = json.dumps({**_DEFAULT_VOICE, **req.voice}, ensure_ascii=False)
        await db.commit()
        data = await _prefs_dict(db, uid)
    return {"code": 0, "data": data}


@router.delete("")
async def reset_preferences(payload: dict = Depends(require_auth)):
    """恢复默认个性化设置（删除该用户的偏好记录）."""
    uid = _uid(payload)
    async with async_session_factory() as db:
        pref = await _get_pref(db, uid)
        if pref is not None:
            await db.delete(pref)
            await db.commit()
    return {"code": 0, "data": {"reset": True}}


@router.get("/presets")
async def list_presets(
    kind: str | None = Query(default=None, description="character / voice，缺省返回全部"),
    payload: dict = Depends(require_auth),
):
    uid = _uid(payload)
    async with async_session_factory() as db:
        return {"code": 0, "data": await _list_presets_db(db, uid, kind)}


@router.post("/presets")
async def create_preset(req: PresetCreateRequest, payload: dict = Depends(require_auth)):
    """把当前设置保存为命名方案（角色方案 / 声音方案）."""
    uid = _uid(payload)
    name = req.name.strip()
    if not name:
        raise BadRequestException("方案名称不能为空")
    if req.kind == "character":
        prompt_id = str(req.payload.get("prompt_id") or "")
        if prompt_id and await get_prompt(prompt_id, str(uid)) is None:
            raise BadRequestException("角色方案引用的角色不存在")
    async with async_session_factory() as db:
        count = (
            await db.execute(
                select(func.count())
                .select_from(UserPreset)
                .where(UserPreset.user_id == uid, UserPreset.kind == req.kind)
            )
        ).scalar_one()
        if count >= PRESET_LIMIT_PER_KIND:
            raise BadRequestException(f"方案数量已达上限（{PRESET_LIMIT_PER_KIND} 个）")
        preset = UserPreset(
            user_id=uid,
            kind=req.kind,
            name=name,
            payload=json.dumps(req.payload, ensure_ascii=False),
        )
        db.add(preset)
        await db.commit()
        await db.refresh(preset)
    return {
        "code": 0,
        "data": {
            "preset_id": str(preset.id),
            "kind": preset.kind,
            "name": preset.name,
            "payload": req.payload,
        },
    }


@router.post("/presets/{preset_id}/activate")
async def activate_preset(preset_id: str, payload: dict = Depends(require_auth)):
    """启用某个方案：角色方案切换 prompt_id（可选连带回复风格）；声音方案应用声音设置."""
    uid = _uid(payload)
    try:
        pid = uuid.UUID(preset_id)
    except (ValueError, TypeError) as exc:
        raise BadRequestException("无效的方案 ID") from exc
    async with async_session_factory() as db:
        preset = await db.get(UserPreset, pid)
        if preset is None or preset.user_id != uid:
            raise NotFoundException("方案不存在")
        data = json.loads(preset.payload) if preset.payload else {}
        if preset.kind == "character":
            prompt_id = str(data.get("prompt_id") or "")
            if prompt_id and await get_prompt(prompt_id, str(uid)) is None:
                raise BadRequestException("角色方案引用的角色不存在")
            user = await db.get(User, uid)
            if user is not None:
                user.prompt_id = prompt_id or None
            style = str(data.get("reply_style") or "")
            if style in ("long", "short"):
                pref = await _get_pref(db, uid)
                if pref is None:
                    pref = UserPreference(user_id=uid)
                    db.add(pref)
                pref.reply_style = style
        elif preset.kind == "voice":
            pref = await _get_pref(db, uid)
            if pref is None:
                pref = UserPreference(user_id=uid)
                db.add(pref)
            pref.voice = json.dumps({**_DEFAULT_VOICE, **data}, ensure_ascii=False)
        await db.commit()
        data_out = await _prefs_dict(db, uid)
    return {"code": 0, "data": data_out}


@router.delete("/presets/{preset_id}")
async def delete_preset(preset_id: str, payload: dict = Depends(require_auth)):
    uid = _uid(payload)
    try:
        pid = uuid.UUID(preset_id)
    except (ValueError, TypeError) as exc:
        raise BadRequestException("无效的方案 ID") from exc
    async with async_session_factory() as db:
        preset = await db.get(UserPreset, pid)
        if preset is None or preset.user_id != uid:
            raise NotFoundException("方案不存在")
        await db.delete(preset)
        await db.commit()
    return {"code": 0, "data": {"deleted": preset_id}}
