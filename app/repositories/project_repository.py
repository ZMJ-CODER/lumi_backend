"""Project index repository used by planners."""

from __future__ import annotations

from typing import Protocol


class ProjectRepository(Protocol):
    async def list_projects(self, user_id: str) -> list[dict]: ...

    async def list_project_files(
        self, user_id: str, project_id: str, limit: int = 50
    ) -> list[str]: ...


class SqlAlchemyProjectRepository:
    """Default adapter for the existing project-index service.

    Session creation stays here, so planner tests can inject a small fake
    repository and never need a database connection.
    """

    async def list_projects(self, user_id: str) -> list[dict]:
        from app.core.database import async_session_factory
        from app.services import project_index

        async with async_session_factory() as session:
            projects = await project_index.list_projects(session, user_id)
        return [
            {"id": str(project.id), "name": str(project.name or "")}
            for project in projects
        ]

    async def list_project_files(
        self, user_id: str, project_id: str, limit: int = 50
    ) -> list[str]:
        from app.core.database import async_session_factory
        from app.services import project_index

        async with async_session_factory() as session:
            return await project_index.list_project_files(
                session, user_id, project_id, limit=limit
            )
