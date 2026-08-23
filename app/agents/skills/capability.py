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
    server: str | None = None
    raw_name: str | None = None
    permission: str = "user"
    write_op: bool = False
    requires_confirmation: bool = False
    confirmation_mode: str = "server"  # server / client / none
    idempotent: bool = True
    resource_templates: list[str] = Field(default_factory=list)
    annotations: dict = Field(default_factory=dict)

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
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": f"{self.description}{suffix}{guidance}",
                "parameters": self.parameters,
            },
        }


def role_allows(required: str, actual: str) -> bool:
    levels = {"user": 0, "admin": 1, "superadmin": 2}
    return levels.get(str(actual or "user"), 0) >= levels.get(str(required or "user"), 0)
