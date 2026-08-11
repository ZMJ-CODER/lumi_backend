"""技能注册中心 —— 管理所有可被智能体调用的技能."""

from loguru import logger

from app.agents.skills.base import Skill


class SkillRegistry:
    """技能注册表（单例）."""

    _skills: dict[str, Skill] = {}
    _sources: dict[str, str] = {}  # skill name -> builtin / plugin

    @classmethod
    def register(cls, skill: Skill, source: str = "builtin") -> None:
        """注册一个技能（source: builtin / plugin）."""
        if skill.name in cls._skills:
            logger.warning(f"技能 '{skill.name}' 已存在，将被覆盖（来源: {source}）")
        cls._skills[skill.name] = skill
        cls._sources[skill.name] = source
        logger.debug(f"技能已注册: {skill.name} | 需要沙箱: {skill.requires_sandbox}")

    @classmethod
    def get(cls, name: str) -> Skill | None:
        """按名称获取技能."""
        return cls._skills.get(name)

    @classmethod
    def unregister(cls, name: str) -> Skill | None:
        """卸载技能（插件热更新用）；返回被移除的技能."""
        removed = cls._skills.pop(name, None)
        cls._sources.pop(name, None)
        return removed

    @classmethod
    def get_source(cls, name: str) -> str:
        """技能来源：builtin / plugin."""
        return cls._sources.get(name, "builtin")

    @classmethod
    def list(cls) -> list[Skill]:
        """列出所有已注册技能."""
        return list(cls._skills.values())

    @classmethod
    def clear(cls) -> None:
        """清空注册表（主要用于测试）."""
        cls._skills.clear()
        cls._sources.clear()


def init_skills() -> None:
    """初始化：加载插件目录（plugins/skills）的全部技能."""
    from app.agents.skills.loader import load_skill_plugins

    load_skill_plugins()
