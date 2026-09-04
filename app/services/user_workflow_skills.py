"""用户私有声明式 Workflow Skill 的存取与执行适配。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.skills.base import SkillContext, ToolOutput, WorkflowSkill
from app.agents.skills.registry import SkillRegistry, ToolRegistry
from app.models.db_models import UserWorkflowSkill


_UNSAFE_DEFINITION_TOKENS = ("http://", "https://", "file://", "import ", "exec(", "eval(", "lambda ", "subprocess")


def _is_safe_literal(value: Any, *, depth: int = 0) -> bool:
    """用户流程定义只接受受限 JSON 字面量和简单输入占位符。"""
    if depth > 4:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        text = value.casefold()
        return len(value) <= 4_000 and not any(token in text for token in _UNSAFE_DEFINITION_TOKENS)
    if isinstance(value, list):
        return len(value) <= 32 and all(_is_safe_literal(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 32 and all(
            isinstance(key, str)
            and len(key) <= 120
            and _is_safe_literal(item, depth=depth + 1)
            for key, item in value.items()
        )
    return False


def _uid(value: str) -> uuid.UUID:
    return uuid.UUID(str(value))


def _validate_definition(
    allowed_tools: list[str],
    steps: list[dict[str, Any]],
    *,
    input_names: set[str] | None = None,
) -> None:
    # 用户 Skill 可以组合经治理的内部办公能力，但这些能力仍不会进入
    # Function Calling 公共候选池。是否可被用户 Skill 引用由
    # ``user_workflow_allowed`` 再做一次显式门控。
    valid_tools = {
        tool.name
        for tool in ToolRegistry.list(include_internal=True)
        if tool.status == "stable" and tool.user_workflow_allowed and not tool.write_op
    }
    requested = {str(name).strip() for name in allowed_tools if str(name).strip()}
    if not requested:
        raise ValueError("必须至少选择一个已注册 Tool")
    unknown = requested - valid_tools
    if unknown:
        raise ValueError("包含未注册或不可声明的 Tool: " + ", ".join(sorted(unknown)))
    for step in steps:
        tool = str(step.get("tool") or "").strip()
        if tool not in requested:
            raise ValueError(f"步骤工具未在 allowed_tools 声明: {tool}")
        if not _is_safe_literal(step.get("arguments") or {}):
            raise ValueError("步骤参数只允许受限 JSON 字面量，不能包含脚本、命令或 URL")
        if input_names is not None:
            unknown_inputs = _placeholder_names(step.get("arguments") or {}) - input_names
            if unknown_inputs:
                raise ValueError("步骤引用了未声明的输入: " + ", ".join(sorted(unknown_inputs)))


def validate_input_schema(input_schema: dict[str, Any]) -> None:
    """限制用户 Skill 输入为浅层字段定义，避免将 JSON Schema 当执行载体。"""
    if not isinstance(input_schema, dict) or not _is_safe_literal(input_schema):
        raise ValueError("input_schema 仅允许受限 JSON 字面量")
    properties = input_schema.get("properties", {})
    if properties and (not isinstance(properties, dict) or len(properties) > 16):
        raise ValueError("input_schema.properties 必须为不超过 16 项的对象")
    for name, spec in properties.items():
        if not isinstance(name, str) or not name.isidentifier() or not isinstance(spec, dict):
            raise ValueError("input_schema 字段必须是合法标识符")
        if spec.get("format") == "uri":
            raise ValueError("用户 Skill 不允许 URL 类型输入")


def input_names(input_schema: dict[str, Any]) -> set[str]:
    """返回已经通过校验的用户输入名。"""
    return set((input_schema.get("properties") or {}).keys())


def _placeholder_names(value: Any) -> set[str]:
    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        return {value[2:-2]}
    if isinstance(value, list):
        return set().union(*(_placeholder_names(item) for item in value)) if value else set()
    if isinstance(value, dict):
        return set().union(*(_placeholder_names(item) for item in value.values())) if value else set()
    return set()


def validate_user_skill_name(name: str) -> None:
    """拒绝占用 Tool 或开发者公共 Skill 名称的用户自建 Skill。"""
    normalized = str(name or "").strip()
    if ToolRegistry.get(normalized) is not None:
        raise ValueError("用户 Skill 名称不能与已注册 Tool 重名")
    if SkillRegistry.get_workflow(normalized) is not None:
        raise ValueError("用户 Skill 名称不能与开发者公共 Skill 重名")


class DeclarativeUserWorkflowSkill(WorkflowSkill):
    """从数据库加载的私有 Skill；步骤仅能调用已批准 Tool。"""

    def __init__(self, record: UserWorkflowSkill):
        self.name = record.name
        self.description = record.description
        self.version = f"1.0.{record.version}"
        self.status = "stable" if record.status == "enabled" else "disabled"
        self.category = record.category
        self.scenes = list(record.scenes or ["office"])
        self.allowed_tools = list(record.allowed_tools or [])
        self.input_schema = dict(record.input_schema or {})
        self.steps = list(record.steps or [])
        self.owner_user_id = str(record.user_id)
        self.visibility = "private"
        self.source = "user"

    async def run(self, params: dict, context: SkillContext, invoke_tool) -> ToolOutput:
        outputs: list[str] = []
        for index, step in enumerate(self.steps, 1):
            tool = str(step.get("tool") or "")
            arguments = dict(step.get("arguments") or {})
            # 显式占位符只读取调用时给出的输入，不支持模板表达式或任意求值。
            arguments = {key: _resolve_input(value, params) for key, value in arguments.items()}
            result = await invoke_tool(tool, arguments)
            if not result.success:
                return ToolOutput(
                    success=False,
                    error=result.error or f"第 {index} 步执行失败",
                    error_code=result.error_code or "EXEC_ERROR",
                    retryable=result.retryable,
                    metadata={"failed_step": index, "tool": tool},
                )
            outputs.append(result.output)
        return ToolOutput(success=True, output="\n\n".join(item for item in outputs if item))


def _resolve_input(value: Any, params: dict[str, Any]) -> Any:
    """递归解析唯一允许的占位符 ``{{input_name}}``，不执行表达式。"""
    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        return params.get(value[2:-2], "")
    if isinstance(value, list):
        return [_resolve_input(item, params) for item in value]
    if isinstance(value, dict):
        return {key: _resolve_input(item, params) for key, item in value.items()}
    return value


async def load_user_workflow_skills(session: AsyncSession, user_id: str) -> list[DeclarativeUserWorkflowSkill]:
    try:
        uid = _uid(user_id)
    except (TypeError, ValueError):
        return []
    records = (
        await session.scalars(
            select(UserWorkflowSkill).where(
                UserWorkflowSkill.user_id == uid,
                UserWorkflowSkill.status == "enabled",
            )
        )
    ).all()
    return [DeclarativeUserWorkflowSkill(record) for record in records]


async def get_visible_workflow_skills(user_id: str) -> list[WorkflowSkill]:
    """返回一个用户可规划的公共与私有 Workflow Skill。

    私有定义以数据库为事实来源，避免 API worker、Temporal worker 的内存
    注册表不一致；同名私有定义覆盖公共定义，仅在该用户的可见列表中生效。
    """
    public = [
        item
        for item in SkillRegistry.list_visible(user_id)
        if item.source == "developer" and item.status != "disabled"
    ]
    try:
        from app.core.database import async_session_factory

        async with async_session_factory() as session:
            private = await load_user_workflow_skills(session, user_id)
    except Exception:
        private = []
    visible = {item.name: item for item in public}
    visible.update({item.name: item for item in private})
    return list(visible.values())


async def load_user_workflow_skill(
    session: AsyncSession,
    user_id: str,
    name: str,
) -> DeclarativeUserWorkflowSkill | None:
    """按所有者读取一个启用的私有 Skill。

    不能只依赖进程内 ``SkillRegistry``：API 多 worker、Temporal worker
    和重启后的任务不会共享内存。这里的按需读取确保私有 Skill 始终以
    数据库中的所有者约束为准，且不会把其他用户的定义装入当前任务。
    """
    try:
        uid = _uid(user_id)
    except (TypeError, ValueError):
        return None
    record = await session.scalar(
        select(UserWorkflowSkill).where(
            UserWorkflowSkill.user_id == uid,
            UserWorkflowSkill.name == str(name),
            UserWorkflowSkill.status == "enabled",
        )
    )
    return DeclarativeUserWorkflowSkill(record) if record is not None else None


async def refresh_user_workflow_registry(session: AsyncSession, user_id: str) -> None:
    """刷新单个用户的私有 Skill；名称在注册表中按 user_id 隔离保存。"""
    for skill in list(SkillRegistry.list_visible(user_id)):
        if skill.source == "user" and skill.owner_user_id == str(user_id):
            SkillRegistry.unregister(skill.name, user_id=user_id)
    for skill in await load_user_workflow_skills(session, user_id):
        SkillRegistry.register(skill, source="user")
