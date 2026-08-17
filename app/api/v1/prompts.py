"""角色提示词 API：列出/创建/查看/删除可插拔角色提示词."""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user, require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.db_models import User
from app.models.prompt import CreatePromptRequest
from app.services.prompts import (
    create_user_prompt,
    delete_user_prompt,
    get_prompt,
    list_prompts,
)

router = APIRouter()


def _uid(payload: dict) -> str | None:
    return payload.get("sub") if payload else None


@router.get("")
async def list_prompt_characters(payload: dict = Depends(get_current_user)):
    """列出所有可用角色：内置 + 当前用户自定义."""
    items = await list_prompts(_uid(payload))
    return {"code": 0, "data": {"items": items}}


@router.post("")
async def create_prompt(
    req: CreatePromptRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """创建自定义角色，并自动切换为当前使用角色."""
    user_id = _uid(payload)
    try:
        uid = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        raise BadRequestException("令牌无效") from None
    user = await db.get(User, uid)
    if not user:
        raise NotFoundException("用户不存在")

    prompt = await create_user_prompt(user_id, req.name, req.description, req.content)
    db.add(prompt)
    await db.flush()
    user.prompt_id = str(prompt.id)  # 创建后自动替换当前角色
    await db.commit()

    return {
        "code": 0,
        "data": {
            "prompt_id": str(prompt.id),
            "name": prompt.name,
            "description": prompt.description or "",
            "content": prompt.content,
            "is_custom": True,
        },
        "message": "角色已创建并启用",
    }


@router.get("/{prompt_id}")
async def get_prompt_detail(prompt_id: str, payload: dict = Depends(get_current_user)):
    """查看单个角色（含提示词正文，设置页预览用）."""
    p = await get_prompt(prompt_id, _uid(payload))
    if not p:
        raise NotFoundException("角色不存在")
    return {"code": 0, "data": p}


@router.delete("/{prompt_id}")
async def delete_prompt(
    prompt_id: str,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    """删除自定义角色（仅本人）；若为当前使用角色则恢复默认."""
    user_id = _uid(payload)
    ok = await delete_user_prompt(user_id, prompt_id)
    if not ok:
        raise NotFoundException("自定义角色不存在或无权删除")
    try:
        user = await db.get(User, uuid.UUID(str(user_id)))
        if user and user.prompt_id == prompt_id:
            user.prompt_id = None
            await db.commit()
    except (ValueError, TypeError):
        pass
    return {"code": 0, "message": "已删除"}
