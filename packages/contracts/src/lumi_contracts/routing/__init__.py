"""路由契约（TaskProfile / RouteDecision / ExecutionRequest）。

三者物理上各自独立成模块，这里统一再导出——既有导入路径
``from lumi_contracts.routing import RouteDecision`` 与
``from lumi_contracts.routing.task_profile import TaskProfile`` 都继续可用。
"""

from __future__ import annotations

from lumi_contracts.routing.execution_request import ExecutionRequest
from lumi_contracts.routing.route_decision import RouteDecision, RouteMode
from lumi_contracts.routing.task_profile import (
    Complexity,
    ExecutionTarget,
    InfoSource,
    TaskProfile,
)

__all__ = [
    "Complexity",
    "ExecutionRequest",
    "ExecutionTarget",
    "InfoSource",
    "RouteDecision",
    "RouteMode",
    "TaskProfile",
]
