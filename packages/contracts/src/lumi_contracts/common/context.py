"""服务端上下文与数据敏感级别。

安全边界（与方案第七节一致）：``user_id`` / ``workspace_id`` / ``device_id`` /
审批状态等身份字段**必须由服务端注入**，插件与模型传入的值一律不可信。

因此本模块只提供"服务端构造"的数据结构；它不提供任何"从模型/插件参数解析身份"
的入口。任何 `from_arguments()` 之类的便利方法都属于反模式，故意不提供。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Sensitivity(StrEnum):
    """数据敏感级别：决定投影阶段的脱敏强度与是否允许外发。"""

    PUBLIC = "PUBLIC"
    INTERNAL = "INTERNAL"
    CONFIDENTIAL = "CONFIDENTIAL"
    CREDENTIAL = "CREDENTIAL"


class ServerContext(BaseModel):
    """服务端注入的执行上下文（冻结：插件不可改写身份字段）。"""

    model_config = ConfigDict(frozen=True)

    user_id: str = ""
    user_role: str = "user"
    conversation_id: str = ""
    job_id: str = ""
    workspace_id: str = ""
    device_id: str = ""
    # 允许的副作用等级 / 数据敏感级别（由策略层按授权快照注入）。
    data_sensitivity: Sensitivity = Sensitivity.INTERNAL
    # 审批快照标识：只有服务端能填；插件不得据此伪造"已批准"。
    approval_grant_hash: str = ""
    # 可访问的附加资源范围（只允许收窄，不允许插件扩展）。
    office_doc_ids: tuple[str, ...] = ()
    authorized_project_ids: tuple[str, ...] = ()
    request_id: str = ""
    trace_id: str = ""
    extra: dict = Field(default_factory=dict)

    def child(self, **overrides: object) -> "ServerContext":
        """派生一个子上下文；身份字段只能由服务端代码显式覆盖。"""
        return self.model_copy(update=dict(overrides))


__all__ = ["Sensitivity", "ServerContext"]
