"""内置技能包：导入即注册到 SkillRegistry."""

from app.agents.skills.registry import SkillRegistry
from app.agents.skills.tools.get_datetime_skill import GetDatetimeSkill
from app.agents.skills.tools.python_exec_skill import PythonExecSkill
from app.agents.skills.tools.query_knowledge_skill import QueryKnowledgeSkill
from app.agents.skills.tools.web_search_skill import WebSearchSkill

SkillRegistry.register(GetDatetimeSkill())
SkillRegistry.register(PythonExecSkill())
SkillRegistry.register(QueryKnowledgeSkill())
SkillRegistry.register(WebSearchSkill())
