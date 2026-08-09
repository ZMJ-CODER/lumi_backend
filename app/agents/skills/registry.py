"""技能注册中心 —— 管理所有可被智能体调用的技能."""

from loguru import logger

from app.agents.skills.base import Skill


class SkillRegistry:
    """技能注册表（单例）."""

    _skills: dict[str, Skill] = {}

    @classmethod
    def register(cls, skill: Skill) -> None:
        """注册一个技能."""
        if skill.name in cls._skills:
            logger.warning(f"技能 '{skill.name}' 已存在，将被覆盖")
        cls._skills[skill.name] = skill
        logger.info(f"技能已注册: {skill.name} | 需要沙箱: {skill.requires_sandbox}")

    @classmethod
    def get(cls, name: str) -> Skill | None:
        """按名称获取技能."""
        return cls._skills.get(name)

    @classmethod
    def list(cls) -> list[Skill]:
        """列出所有已注册技能."""
        return list(cls._skills.values())

    @classmethod
    def clear(cls) -> None:
        """清空注册表（主要用于测试）."""
        cls._skills.clear()
