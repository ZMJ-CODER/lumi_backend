"""智能体工具调用循环（预留沙箱槽位）.

后期技能调用流程（二期实现）：
  1. LLM 以结构化方式输出技能调用请求（function calling）
  2. SkillRegistry 校验技能与参数
  3. 经 Sandbox 隔离执行（get_sandbox()）
  4. 结果回填对话 → 继续循环，直到 LLM 给出最终答复

当前状态：
  - AGENT_SKILLS_ENABLED=false 或未注册技能 → 返回 None，
    保持原有"单轮直接回复"行为，不改变现有链路。
"""

from loguru import logger

from app.agents.skills.registry import SkillRegistry
from app.core.config import settings


async def maybe_run_skills(
    conversation_id: str,
    scene: str,
    messages: list[dict],
    user_id: str = "",
) -> list[dict] | None:
    """技能调用入口（预留）：返回技能执行结果列表；未启用/无技能时返回 None."""
    if not settings.AGENT_SKILLS_ENABLED:
        return None
    skills = SkillRegistry.list()
    if not skills:
        return None
    logger.info(
        "⚙️ [技能调用] 技能已启用（{} 个），执行循环待二期实现: conv={} scene={}",
        len(skills), conversation_id, scene,
    )
    # TODO(二期): 让 LLM 在 function-calling 模式下决定调用哪些技能，
    # 经沙箱执行后返回 [{skill, params, result}]，由调用方回填给 LLM 继续对话。
    return []
