"""Skill 与 MCP 共用的工具能力、安全和调度元数据。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolCapability(BaseModel):
    name: str
    version: str = "1.0.0"
    status: str = "stable"
    schema_fingerprint: str = ""
    replacement_skill_id: str = ""
    description: str = ""
    category: str = "general"
    resource: str = ""
    action_type: str = "read"  # read / write
    idempotency_type: str = "natural_key"
    deprecated_by: str | None = None
    domain: str = ""
    intent_tags: list[str] = Field(default_factory=list)
    conflicts_with: list[str] = Field(default_factory=list)
    preferred_over: list[str] = Field(default_factory=list)
    use_when: list[str] = Field(default_factory=list)
    do_not_use_when: list[str] = Field(default_factory=list)
    selection_examples: list[str] = Field(default_factory=list)
    result_contract: str = ""
    handoff_to: list[str] = Field(default_factory=list)
    bootstrap_intents: list[str] = Field(default_factory=list)
    bootstrap_until: str = ""
    parameters: dict = Field(default_factory=lambda: {"type": "object", "properties": {}})
    source: str = "skill"  # skill / mcp
    environment: str = "server"  # server / sandbox / client
    server: str | None = None
    raw_name: str | None = None
    permission: str = "user"
    write_op: bool = False
    requires_confirmation: bool = False
    confirmation_mode: str = "server"  # server / client / none
    idempotent: bool = True
    resource_templates: list[str] = Field(default_factory=list)
    plan_required_fields: list[str] = Field(default_factory=list)
    annotations: dict = Field(default_factory=dict)
    #: 审批档位声明（A 自动 / B 例行 / C 始终确认）：空 = 由能力副作用派生。
    #: 插件/Provider **声明一次**，审批窗口与动作窗口都从注册表读它，不再各处写死表。
    risk_tier: str = ""
    #: 审批策略声明（``none`` / ``confirm`` …）：空 = 由 ``requires_confirmation`` 派生。
    approval_policy: str = ""
    #: **能力归属声明**（形如 ``workspace.read``）：空 = 由静态映射表派生。
    #:
    #: 新工具（插件/Provider 提供的、静态表还不认识的）必须能声明自己属于哪个能力，
    #: 否则"声明一次 → 能力映射/MCP 目标/动作窗口/审批/Provider 路由 全部自动派生"
    #: 就断在第一步。已登记的工具不受影响：静态映射表优先，声明不能改写既有归属
    #: （路由/租约/审批都依赖那张表）。
    capability: str = ""
    #: **资源类型声明**（``workspace`` / ``memory`` …）：空 = 由能力绑定推导。
    #: 统一能力跨多种资源，新资源（如任务记忆）靠它声明自己属于哪一类。
    resource_type: str = ""

    def to_tool_definition(self) -> dict:
        flags = []
        if self.write_op:
            flags.append("写操作")
        if self.requires_confirmation:
            flags.append("需要用户确认")
        if self.permission != "user":
            flags.append(f"权限：{self.permission}")
        suffix = f"（{'；'.join(flags)}）" if flags else ""
        selection = []
        if self.use_when:
            selection.append("适用：" + "；".join(self.use_when[:3]))
        if self.do_not_use_when:
            selection.append("不要用于：" + "；".join(self.do_not_use_when[:3]))
        if self.selection_examples:
            selection.append("例：" + "；".join(self.selection_examples[:2]))
        if self.result_contract:
            selection.append("返回：" + self.result_contract)
        guidance = "\n" + "\n".join(selection) if selection else ""
        schema = dict(self.parameters or {})
        if self.name in {"Read", "Write", "Edit", "Glob", "Grep", "Bash"}:
            props = dict(schema.get("properties") or {})
            props.setdefault("project_id", {"type": "string", "description": "可选的已授权项目 ID"})
            schema["properties"] = props
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.description}{suffix}{guidance}",
                "parameters": schema,
            },
        }


def role_allows(required: str, actual: str) -> bool:
    levels = {"user": 0, "admin": 1, "superadmin": 2}
    return levels.get(str(actual or "user"), 0) >= levels.get(str(required or "user"), 0)
