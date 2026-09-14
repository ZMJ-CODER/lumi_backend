"""ORM 模型聚合入口（**兼容层**）。

P6 把 29 个模型按域拆进了 :mod:`app.models.db`；本模块保留为**聚合入口**，
因此 Alembic（``target_metadata``）与既有的 ``from app.models.db_models import X``
全部照旧工作。新代码请直接从域模块取（``from app.models.db.memory import Memory``）。

它是**同包聚合**（不是跨包转发壳）：只 re-export ``app.models.db.*``，
因此不受门禁规则 4 约束。
"""

from __future__ import annotations
from app.models.db.user import (
    User,
    RefreshToken,
    UserPrompt,
    UserPreference,
    UserPreset,
)
from app.models.db.conversation import (
    Conversation,
    Message,
    ConversationMemoryState,
    ConversationSegment,
    Attachment,
)
from app.models.db.knowledge import (
    KnowledgeSpace,
    Document,
    DocumentChunk,
    Project,
    ProjectIndex,
    CodeEmbedding,
)
from app.models.db.memory import (
    Memory,
    MemoryProfile,
)
from app.models.db.office import (
    OfficeSession,
    OfficeTaskIndex,
)
from app.models.db.job import (
    EffectJournal,
    JobRun,
    JobStep,
)
from app.models.db.plugin import (
    UserMcpToolBinding,
    SkillTelemetryDaily,
    UserWorkflowSkill,
)
from app.models.db.audit import (
    ControlLog,
)
from app.models.db.usage import (
    LLMUsage,
    DailyTokenStat,
)

__all__ = [
    "Attachment",
    "CodeEmbedding",
    "ControlLog",
    "Conversation",
    "ConversationMemoryState",
    "ConversationSegment",
    "DailyTokenStat",
    "Document",
    "DocumentChunk",
    "EffectJournal",
    "JobRun",
    "JobStep",
    "KnowledgeSpace",
    "LLMUsage",
    "Memory",
    "MemoryProfile",
    "Message",
    "OfficeSession",
    "OfficeTaskIndex",
    "Project",
    "ProjectIndex",
    "RefreshToken",
    "SkillTelemetryDaily",
    "User",
    "UserMcpToolBinding",
    "UserPreference",
    "UserPreset",
    "UserPrompt",
    "UserWorkflowSkill",
]
