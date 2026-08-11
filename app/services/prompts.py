"""角色提示词服务：内置目录（app/prompts/*.md）+ 用户自定义（user_prompts 表）.

内置文件格式：Markdown，头部为 YAML 风格 frontmatter（--- 分隔）：
---
id: lumi_default
name: 默认助手
description: ...
tags: [默认, 全能]
---
（提示词正文）
"""

import re
import uuid
from pathlib import Path

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.db_models import UserPrompt

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _prompts_dir() -> Path:
    return Path(settings.PROMPTS_DIR)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 frontmatter，返回 (meta, 正文)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    meta: dict = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    content = text[m.end():].strip()
    return meta, content


def _list_builtin() -> list[dict]:
    """列出内置角色（不含正文）."""
    results: list[dict] = []
    for path in sorted(_prompts_dir().glob("*.md")):
        meta, _ = _parse_frontmatter(path.read_text(encoding="utf-8"))
        pid = meta.get("id") or path.stem
        raw_tags = meta.get("tags", "")
        tags = [t.strip() for t in raw_tags.strip("[]").split(",") if t.strip()] if raw_tags else []
        results.append(
            {
                "prompt_id": pid,
                "name": meta.get("name", pid),
                "description": meta.get("description", ""),
                "tags": tags,
                "is_custom": False,
            }
        )
    return results


def _get_builtin(prompt_id: str) -> dict | None:
    """按 id 获取内置角色（含正文）."""
    for path in _prompts_dir().glob("*.md"):
        meta, content = _parse_frontmatter(path.read_text(encoding="utf-8"))
        if (meta.get("id") or path.stem) == prompt_id:
            return {
                "prompt_id": prompt_id,
                "name": meta.get("name", prompt_id),
                "description": meta.get("description", ""),
                "tags": [t.strip() for t in (meta.get("tags", "")).strip("[]").split(",") if t.strip()],
                "content": content,
                "is_custom": False,
            }
    return None


def _to_uid(user_id: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return None


async def list_prompts(user_id: str | None = None) -> list[dict]:
    """列出所有可用角色：内置 + 当前用户自定义（含 is_custom 标记）."""
    items = _list_builtin()
    uid = _to_uid(user_id) if user_id else None
    if uid:
        async with async_session_factory() as session:
            rows = (
                await session.execute(
                    select(UserPrompt)
                    .where(UserPrompt.user_id == uid)
                    .order_by(UserPrompt.created_at.desc())
                )
            ).scalars().all()
        items.extend(
            {
                "prompt_id": str(r.id),
                "name": r.name,
                "description": r.description or "",
                "tags": ["自定义"],
                "is_custom": True,
            }
            for r in rows
        )
    return items


async def get_prompt(prompt_id: str, user_id: str | None = None) -> dict | None:
    """按 id 获取角色（含正文）；用户自定义仅本人可见."""
    builtin = _get_builtin(prompt_id)
    if builtin:
        return builtin
    uid = _to_uid(user_id) if user_id else None
    if not uid:
        return None
    try:
        pid = uuid.UUID(prompt_id)
    except (ValueError, TypeError):
        return None
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(UserPrompt).where(UserPrompt.id == pid, UserPrompt.user_id == uid)
            )
        ).scalar_one_or_none()
    if not row:
        return None
    return {
        "prompt_id": str(row.id),
        "name": row.name,
        "description": row.description or "",
        "content": row.content,
        "is_custom": True,
    }


async def get_prompt_content(prompt_id: str, user_id: str | None = None) -> str | None:
    """按 id 获取提示词正文；不存在返回 None."""
    p = await get_prompt(prompt_id, user_id)
    return p["content"] if p else None


async def create_user_prompt(
    user_id: str, name: str, description: str, content: str
) -> UserPrompt:
    """创建用户自定义角色（返回 ORM 对象，未提交事务由调用方处理）."""
    uid = _to_uid(user_id)
    if uid is None:
        raise ValueError("无效的用户 ID")
    return UserPrompt(
        user_id=uid,
        name=name.strip(),
        description=description.strip() or None,
        content=content.strip(),
    )


async def delete_user_prompt(user_id: str, prompt_id: str) -> bool:
    """删除用户自定义角色（仅本人）；返回是否删除成功."""
    uid = _to_uid(user_id)
    if uid is None:
        return False
    try:
        pid = uuid.UUID(prompt_id)
    except (ValueError, TypeError):
        return False
    async with async_session_factory() as session:
        result = await session.execute(
            delete(UserPrompt).where(UserPrompt.id == pid, UserPrompt.user_id == uid)
        )
        await session.commit()
        return bool(result.rowcount)
