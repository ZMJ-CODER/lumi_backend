"""``app.models.db``：ORM 模型按域拆分（P6）。

================================  ================================================
模块                               内容
================================  ================================================
``user``                          用户、刷新令牌、自定义提示词、个性化偏好
``conversation``                  会话、消息、会话记忆状态、会话摘要、附件
``knowledge``                     知识空间、文档、分块、本地项目与代码索引
``memory``                        长期记忆事实与用户画像
``office``                        办公文档会话、近期任务索引
``job``                           副作用日志、任务与步骤投影
``plugin``                        用户批准的 MCP 工具、能力遥测、用户 Workflow Skill
``audit``                         操控日志
``usage``                         LLM 用量原始记录与按日聚合
================================  ================================================

**这个 ``__init__`` 刻意 import 全部兄弟模块**：SQLAlchemy 的 relationship 目标写在
注解字符串里（``Mapped[list["Message"]]``），由 registry 按类名解析；如果某个入口只
import 了一个子模块，mapper 配置时会找不到目标类。聚合入口 ``app.models.db_models``
仍然可用（Alembic 与既有 import 都走它）。
"""

from app.models.db import (
    audit,
    conversation,
    job,
    knowledge,
    memory,
    office,
    plugin,
    usage,
    user,
)

__all__ = [
    "audit",
    "conversation",
    "job",
    "knowledge",
    "memory",
    "office",
    "plugin",
    "usage",
    "user",
]
