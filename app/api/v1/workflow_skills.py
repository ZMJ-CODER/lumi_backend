"""用户私有 Workflow Skill 管理接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.models.db_models import UserWorkflowSkill
from app.models.workflow_skill import CreateWorkflowSkillRequest, UpdateWorkflowSkillRequest
from app.agents.skills.registry import SkillRegistry
from app.services.user_workflow_skills import (
    _validate_definition,
    input_names,
    refresh_user_workflow_registry,
    validate_input_schema,
    validate_user_skill_name,
)


router = APIRouter()


def _user_uuid(payload: dict) -> uuid.UUID:
    try:
        return uuid.UUID(str(payload["sub"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BadRequestException("无效用户身份") from exc


def _view(item: UserWorkflowSkill) -> dict:
    return {
        "id": str(item.id), "name": item.name, "display_name": item.display_name,
        "description": item.description, "category": item.category, "scenes": item.scenes,
        "allowed_tools": item.allowed_tools, "steps": item.steps, "input_schema": item.input_schema,
        "status": item.status, "version": item.version, "visibility": "private", "source": "user",
    }


def _developer_view(item) -> dict:
    input_schema = getattr(item, "input_schema", {})
    return {
        "id": None, "name": item.name, "display_name": item.name,
        "description": item.description, "category": item.category, "scenes": item.scenes,
        "allowed_tools": item.allowed_tools, "steps": None,
        "input_schema": input_schema if isinstance(input_schema, dict) else {},
        "status": item.status, "version": item.version, "visibility": "public", "source": "developer",
    }


async def _owned(session: AsyncSession, skill_id: str, user_id: uuid.UUID) -> UserWorkflowSkill:
    try:
        parsed_id = uuid.UUID(skill_id)
    except ValueError as exc:
        raise NotFoundException("Skill 不存在") from exc
    item = await session.scalar(
        select(UserWorkflowSkill).where(UserWorkflowSkill.id == parsed_id, UserWorkflowSkill.user_id == user_id)
    )
    if item is None:
        raise NotFoundException("Skill 不存在")
    return item


@router.get("")
async def list_workflow_skills(payload: dict = Depends(require_auth), db: AsyncSession = Depends(get_db)):
    user_id = _user_uuid(payload)
    rows = (await db.scalars(select(UserWorkflowSkill).where(UserWorkflowSkill.user_id == user_id))).all()
    public = [
        item
        for item in SkillRegistry.list_visible(str(user_id))
        if item.source == "developer" and item.status != "disabled"
    ]
    return {"code": 0, "data": {"items": [_developer_view(item) for item in public] + [_view(row) for row in rows]}}


@router.post("")
async def create_workflow_skill(
    req: CreateWorkflowSkillRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_uuid(payload)
    steps = [step.model_dump() for step in req.steps]
    try:
        validate_user_skill_name(req.name)
        validate_input_schema(req.input_schema)
        _validate_definition(req.allowed_tools, steps, input_names=input_names(req.input_schema))
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    exists = await db.scalar(
        select(UserWorkflowSkill.id).where(UserWorkflowSkill.user_id == user_id, UserWorkflowSkill.name == req.name)
    )
    if exists:
        raise BadRequestException("已存在同名私有 Skill")
    item = UserWorkflowSkill(
        user_id=user_id, name=req.name, display_name=req.display_name, description=req.description,
        category=req.category, scenes=req.scenes, allowed_tools=req.allowed_tools, steps=steps,
        input_schema=req.input_schema,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    await refresh_user_workflow_registry(db, str(user_id))
    return {"code": 0, "data": _view(item)}


@router.patch("/{skill_id}")
async def update_workflow_skill(
    skill_id: str,
    req: UpdateWorkflowSkillRequest,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_uuid(payload)
    item = await _owned(db, skill_id, user_id)
    values = req.model_dump(exclude_unset=True)
    if "steps" in values:
        values["steps"] = [step.model_dump() for step in values["steps"]]
    allowed_tools = values.get("allowed_tools", item.allowed_tools)
    steps = values.get("steps", item.steps)
    try:
        effective_input_schema = values.get("input_schema", item.input_schema)
        validate_input_schema(effective_input_schema)
        _validate_definition(allowed_tools, steps, input_names=input_names(effective_input_schema))
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    for key, value in values.items():
        setattr(item, key, value)
    item.version += 1
    await db.commit()
    await db.refresh(item)
    await refresh_user_workflow_registry(db, str(user_id))
    return {"code": 0, "data": _view(item)}


@router.delete("/{skill_id}")
async def delete_workflow_skill(
    skill_id: str,
    payload: dict = Depends(require_auth),
    db: AsyncSession = Depends(get_db),
):
    user_id = _user_uuid(payload)
    item = await _owned(db, skill_id, user_id)
    await db.delete(item)
    await db.commit()
    await refresh_user_workflow_registry(db, str(user_id))
    return {"code": 0, "data": {"id": skill_id, "deleted": True}}
