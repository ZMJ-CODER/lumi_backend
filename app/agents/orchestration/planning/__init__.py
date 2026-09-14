"""计划与画像：把请求编译成可执行的计划（规划领域的应用层边界）。

规划器、计划编译器、办公预规划
策略与计划选择、逻辑计划（滚动续跑）、任务画像与形状判定、复杂度评估（TCA）、路由与
置信度校准、成功案例库（Few-Shot 参考）都在这里；与本包原有的
`compilation` / `contracts` / `normalizer` / `prompting` / `context` 一起构成
"从请求到计划"的全部步骤。

本包只负责把办公领域请求转换为可编译的任务树；DAG 执行、持久化、重试与资源治理仍分别
由 ``lumi_orch`` 与 ``lumi_execution`` 提供。规划相关实现均从本包直接导入，
不提供旧模块路径的转发入口。
"""

from app.agents.orchestration.planning.compilation import PlanCompilationService
from app.agents.orchestration.planning.context import PlanRequestContext
from app.agents.orchestration.planning.contracts import Planner, PlannerModelError, TaskTree

__all__ = [
    "PlanRequestContext",
    "PlanCompilationService",
    "Planner",
    "PlannerModelError",
    "TaskTree",
]
