"""本地项目结构索引服务 —— 服务器只存"代码地图"，不存代码正文.

用途：指挥层/代码 agent 通过关键词检索定位相关文件（返回项目内相对路径），
真正的文件读写由 client 技能在用户本地执行（方案 A）。
"""

import uuid

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import Project, ProjectIndex
from app.models.project import ProjectFileIndex


def _uid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def create_project(
    session: AsyncSession,
    user_id: str,
    name: str,
    root_label: str,
    files: list[ProjectFileIndex],
) -> Project:
    uid = _uid(user_id)
    if uid is None:
        raise ValueError("无效的用户 ID")
    project = Project(
        user_id=uid,
        name=name.strip() or "未命名项目",
        root_label=root_label[:500] or None,
        file_count=len(files),
        total_size=sum(f.size for f in files),
        status="ready",
    )
    session.add(project)
    await session.flush()
    if files:
        session.add_all(
            ProjectIndex(
                project_id=project.id,
                file_path=f.path,
                symbols=f.symbols or None,
                summary=f.summary or None,
                file_size=f.size,
            )
            for f in files
        )
    return project


async def list_projects(session: AsyncSession, user_id: str) -> list[Project]:
    uid = _uid(user_id)
    if uid is None:
        return []
    rows = (
        await session.execute(
            select(Project).where(Project.user_id == uid).order_by(Project.created_at.desc())
        )
    ).scalars().all()
    return list(rows)


async def delete_project(session: AsyncSession, user_id: str, project_id: str) -> bool:
    uid = _uid(user_id)
    pid = _uid(project_id)
    if uid is None or pid is None:
        return False
    project = await session.get(Project, pid)
    if not project or project.user_id != uid:
        return False
    await session.execute(delete(ProjectIndex).where(ProjectIndex.project_id == pid))
    await session.delete(project)
    return True


async def search_project(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """在项目索引中检索相关文件（关键词：路径/符号/摘要）."""
    uid = _uid(user_id)
    pid = _uid(project_id)
    if uid is None or pid is None or not query.strip():
        return []
    project = await session.get(Project, pid)
    if not project or project.user_id != uid:
        return []
    like = f"%{query.strip()}%"
    rows = (
        await session.execute(
            select(ProjectIndex)
            .where(
                ProjectIndex.project_id == pid,
                (
                    ProjectIndex.file_path.ilike(like)
                    | ProjectIndex.symbols.ilike(like)
                    | ProjectIndex.summary.ilike(like)
                ),
            )
            .order_by(ProjectIndex.file_path.asc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "file_path": r.file_path,
            "symbols": r.symbols or "",
            "summary": r.summary or "",
            "size": r.file_size,
        }
        for r in rows
    ]


async def list_project_files(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    limit: int = 50,
) -> list[str]:
    """列出项目内文件路径（供规划器上下文，帮助 LLM 指定目标文件）."""
    uid = _uid(user_id)
    pid = _uid(project_id)
    if uid is None or pid is None:
        return []
    project = await session.get(Project, pid)
    if not project or project.user_id != uid:
        return []
    rows = (
        await session.execute(
            select(ProjectIndex.file_path)
            .where(ProjectIndex.project_id == pid)
            .order_by(ProjectIndex.file_path.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def find_file_by_path(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    file_path: str,
) -> dict | None:
    """按相对路径精确查找项目索引中的文件（忽略大小写，兼容 / 与 \\）.

    code agent 显式指定 target_file 时先经此校验，避免把目录/乱猜路径当文件读取。
    """
    uid = _uid(user_id)
    pid = _uid(project_id)
    if uid is None or pid is None or not str(file_path or "").strip():
        return None
    project = await session.get(Project, pid)
    if not project or project.user_id != uid:
        return None
    norm = str(file_path).strip().replace("\\", "/").lstrip("./")
    if not norm or norm in (".", "/"):
        return None
    row = (
        await session.execute(
            select(ProjectIndex).where(
                ProjectIndex.project_id == pid,
                func.lower(ProjectIndex.file_path) == norm.lower(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "file_path": row.file_path,
        "symbols": row.symbols or "",
        "summary": row.summary or "",
        "size": row.file_size,
    }
