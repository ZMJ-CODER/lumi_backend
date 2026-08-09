"""技能层（预留）—— 智能体可调用的能力单元.

新增技能步骤（后期）：
  1. 继承 app.agents.skills.base.Skill
  2. 实现 name / description / parameters_schema / execute
  3. SkillRegistry.register(...) 注册
  4. 需要执行代码/命令的技能，通过沙箱接口运行（见 app.agents.sandbox）
"""

from app.agents.skills.base import Skill, SkillResult
from app.agents.skills.registry import SkillRegistry

__all__ = ["Skill", "SkillResult", "SkillRegistry"]
