"""工具与工作流 Skill 注册中心。

ToolRegistry 保存可执行的原子工具；SkillRegistry 只保存组合工作流。
"""

from __future__ import annotations

from loguru import logger

from app.agents.skills.base import Tool, WorkflowSkill


class ToolRegistry:
    """原子工具注册表。

    ``_tools`` 是模型可见的规范工具集合；旧领域工具和实现细节放入
    ``_internal_tools``，仍可被已编译的 Workflow/Worker 使用，但不会进入
    Function Calling 候选池。这样迁移期间不需要让旧工具继续污染模型命名空间。
    """

    _tools: dict[str, Tool] = {}
    _internal_tools: dict[str, Tool] = {}
    _sources: dict[str, str] = {}

    @classmethod
    def register(cls, tool: Tool, source: str = "builtin", *, public: bool = True) -> None:
        tool.validate_lifecycle()
        # L0 准入只针对可执行原子工具；组合 Skill 永远不会进入此表。
        from app.agents.skills.executor import _skill_capability
        from app.agents.skills.discovery import validate_tool_entry
        errors = validate_tool_entry(_skill_capability(tool))
        if errors:
            raise ValueError(f"Tool {tool.name} 未通过 L0 准入: {'；'.join(errors)}")
        target = cls._tools if public else cls._internal_tools
        other = cls._internal_tools if public else cls._tools
        other.pop(tool.name, None)
        if tool.name in target:
            logger.warning("工具 '{}' 已存在，将被覆盖（来源: {}）", tool.name, source)
        target[tool.name] = tool
        cls._sources[tool.name] = source

    @classmethod
    def get(cls, name: str) -> Tool | None:
        return cls._tools.get(name) or cls._internal_tools.get(name)

    @classmethod
    def unregister(cls, name: str) -> Tool | None:
        cls._sources.pop(name, None)
        return cls._tools.pop(name, None) or cls._internal_tools.pop(name, None)

    @classmethod
    def get_source(cls, name: str) -> str:
        return cls._sources.get(name, "builtin")

    @classmethod
    def list(cls, *, include_internal: bool = False) -> list[Tool]:
        """列出工具；默认只返回 18 个规范公共工具。"""
        values = list(cls._tools.values())
        if include_internal:
            values.extend(cls._internal_tools.values())
        return values

    @classmethod
    def internal_list(cls) -> list[Tool]:
        return list(cls._internal_tools.values())

    @classmethod
    def clear(cls) -> None:
        cls._tools.clear()
        cls._internal_tools.clear()
        cls._sources.clear()


class SkillRegistry:
    """组合工作流注册表（单例），不用于 Function Calling。"""

    _skills: dict[tuple[str, str], WorkflowSkill] = {}
    _sources: dict[tuple[str, str], str] = {}

    @staticmethod
    def _key(skill: WorkflowSkill) -> tuple[str, str]:
        return (str(skill.owner_user_id or "__public__"), skill.name)

    @classmethod
    def register(cls, skill: WorkflowSkill, source: str = "developer") -> None:
        """注册一个组合工作流。"""
        if isinstance(skill, Tool):
            raise TypeError("Tool 必须注册到 ToolRegistry，不能注册为 WorkflowSkill")
        skill.validate_lifecycle()
        key = cls._key(skill)
        if key in cls._skills:
            logger.warning(f"技能 '{skill.name}' 已存在，将被覆盖（来源: {source}）")
        cls._skills[key] = skill
        cls._sources[key] = source
        try:
            from app.agents.skills.routing import invalidate_skill_semantic_index

            invalidate_skill_semantic_index()
        except Exception:
            # 注册表不能被可选的检索加速层阻断。
            pass
        logger.debug("组合 Skill 已注册: {}", skill.name)

    @classmethod
    def get(cls, name: str) -> WorkflowSkill | None:
        """获取开发者公共 Workflow Skill；不回退到 Tool。"""
        return cls._skills.get(("__public__", name))

    @classmethod
    def get_workflow(cls, name: str) -> WorkflowSkill | None:
        """只查询真正的工作流；新运行时不得通过兼容回退获取 Tool。"""
        return cls._skills.get(("__public__", name))

    @classmethod
    def get_visible_workflow(cls, name: str, user_id: str = "") -> WorkflowSkill | None:
        """只返回公共开发者 Skill 或当前用户私有 Skill。"""
        if user_id:
            private = cls._skills.get((str(user_id), name))
            if private is not None:
                return private
        return cls._skills.get(("__public__", name))

    @classmethod
    def list_visible(cls, user_id: str = "") -> list[WorkflowSkill]:
        """用户只能看到公共开发者 Skill 与自己创建的私有 Skill。"""
        return [
            skill for skill in cls._skills.values()
            if (skill.visibility == "public" and skill.source == "developer")
            or (user_id and skill.owner_user_id == str(user_id))
        ]

    @classmethod
    def unregister(cls, name: str, *, user_id: str | None = None) -> WorkflowSkill | None:
        """卸载技能（插件热更新用）；返回被移除的技能."""
        key = (str(user_id) if user_id else "__public__", name)
        removed = cls._skills.pop(key, None)
        cls._sources.pop(key, None)
        try:
            from app.agents.skills.routing import invalidate_skill_semantic_index

            invalidate_skill_semantic_index()
        except Exception:
            pass
        return removed

    @classmethod
    def get_source(cls, name: str) -> str:
        """技能来源：builtin / plugin."""
        return cls._sources.get(("__public__", name), ToolRegistry.get_source(name))

    @classmethod
    def list(cls) -> list[WorkflowSkill]:
        """列出所有已注册技能."""
        return list(cls._skills.values())

    @classmethod
    def clear(cls) -> None:
        """仅清空 Workflow Skill 注册表（主要用于测试）。"""
        cls._skills.clear()
        cls._sources.clear()


def init_skills() -> None:
    """初始化：分别加载 Tool 与开发者公共 Workflow Skill 插件。"""
    from app.agents.skills.loader import load_skill_plugins

    load_skill_plugins()
