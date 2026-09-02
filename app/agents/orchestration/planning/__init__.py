"""规划领域的应用层边界。

此包只负责将办公领域请求转换为可编译的任务树；DAG 执行、持久化、
重试和资源治理仍分别由 ``lumi_orch`` 与 ``lumi_execution`` 提供。
规划相关实现均从本包直接导入，不再提供旧模块路径的转发入口。
"""

from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import Planner, PlannerModelError, TaskTree
from app.agents.orchestration.planning.office_compound import CompoundOfficePlan, build_text_then_todo_plan
from app.agents.orchestration.planning.read_only_dag import build_explicit_read_only_dag
from app.agents.orchestration.planning.compilation import PlanCompilationService
from app.agents.orchestration.planning.strategies import PlannerStrategies
from app.agents.orchestration.planning.patterns import build_pattern, pattern_catalog_text
from app.agents.orchestration.planning.templates import get_template, template_catalog_text

__all__ = [
    "CompoundOfficePlan",
    "PlanRequestContext",
    "PlanCompilationService",
    "PlannerStrategies",
    "Planner",
    "PlannerModelError",
    "TaskTree",
    "build_explicit_read_only_dag",
    "build_text_then_todo_plan",
    "build_pattern",
    "pattern_catalog_text",
    "get_template",
    "template_catalog_text",
]
