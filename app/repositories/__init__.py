"""编排服务使用的持久化边界。

仓储刻意暴露贴合应用的操作，而非将 Redis 客户端或 SQLAlchemy 会话泄漏给
规划代码。

**记忆仓储不在这里**：它是 Memory 域的 Port，家已经搬到 ``app.memory.repository``
（P4 域 3）。让这个聚合入口再转发一遍会把它变成**跨包桥接**（门禁规则 4 会拦下，
理由与 ``app/contracts/plugins`` 相同：一个形状只能有一个家）。调用方改为直接
``from app.memory.repository import MemoryRepository``。
"""

from app.repositories.effect_journal_repository import (
    EffectJournalRepository,
    EffectJournalUnavailable,
    PostgresEffectJournalRepository,
)
from app.repositories.job_repository import JobRepository, StateStoreJobRepository
from app.repositories.project_repository import ProjectRepository, SqlAlchemyProjectRepository

__all__ = [
    "EffectJournalRepository",
    "EffectJournalUnavailable",
    "JobRepository",
    "ProjectRepository",
    "PostgresEffectJournalRepository",
    "SqlAlchemyProjectRepository",
    "StateStoreJobRepository",
]
