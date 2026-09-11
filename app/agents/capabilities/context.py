"""阶段 1：现有能力的 Agent 执行上下文（能力调用需要的授权信息）。

能力 Provider 不自己解析用户原文、也不从模型参数里取工作区/项目——这些是**服务端
授权事实**。``AgentExecutionContext`` 就是这份事实的唯一载体，由调用方（Broker /
执行节点）从 ``routing`` / 服务端上下文构造，Provider 只读。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.plugins import SessionBinding


@dataclass(slots=True)
class AgentExecutionContext:
    """一次能力调用可用的授权上下文（只读）。"""

    binding: SessionBinding
    user_role: str = "user"
    #: 服务端授权的工作区（模型/插件传值一律忽略，防止越权读别的目录）。
    authorized_workspace_id: str = ""
    #: 服务端授权的项目 ID 白名单。
    authorized_project_ids: tuple[str, ...] = ()
    #: 用户原文（Provider 只用于读取语义，不做路由决策）。
    request: str = ""
    #: 已通过的审批指纹集合（由审批服务写入，Provider 不自行判定）。
    confirmed_tool_calls: frozenset[str] = frozenset()
    #: 额外的只读元数据（不参与鉴权，仅用于展示/审计）。
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def user_id(self) -> str:
        return self.binding.user_id

    @property
    def conversation_id(self) -> str:
        return self.binding.conversation_id

    @property
    def device_id(self) -> str:
        return self.binding.device_id

    @property
    def workspace_id(self) -> str:
        """生效工作区：授权值优先，其次会话绑定值。"""
        return str(self.authorized_workspace_id or self.binding.workspace_id or "")

    def project_allowed(self, project_id: str) -> bool:
        text = str(project_id or "").strip()
        if not text:
            return False
        return text in set(self.authorized_project_ids)

    @classmethod
    def from_metadata(
        cls,
        *,
        user_id: str,
        user_role: str = "user",
        conversation_id: str = "",
        workspace_id: str = "",
        device_id: str = "",
        session_id: str = "",
        request: str = "",
        project_ids: list[str] | tuple[str, ...] | None = None,
        confirmed_tool_calls: frozenset[str] | set[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "AgentExecutionContext":
        """由既有字段构造（调用方不必手拼 ``SessionBinding``）。"""
        return cls(
            binding=SessionBinding(
                user_id=str(user_id or ""),
                conversation_id=str(conversation_id or ""),
                workspace_id=str(workspace_id or ""),
                device_id=str(device_id or ""),
                session_id=str(session_id or ""),
            ),
            user_role=str(user_role or "user"),
            authorized_workspace_id=str(workspace_id or ""),
            authorized_project_ids=tuple(
                str(item) for item in (project_ids or ()) if str(item or "").strip()
            ),
            request=str(request or ""),
            confirmed_tool_calls=frozenset(str(item) for item in (confirmed_tool_calls or ())),
            metadata=dict(metadata or {}),
        )


__all__ = ["AgentExecutionContext"]
