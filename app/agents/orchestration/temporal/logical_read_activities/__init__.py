"""逻辑计划 Activity（按运行族分子模块）。

原本一个 `logical_read_activities.py`（716 行）混着纯读前沿、副作用前沿、审批过期、
取消、重规划与失败收尾；现在按族分开，`__init__` 只做**聚合再导出**，Worker 注册与
测试引用保持不变。

| 族 | 模块 |
| --- | --- |
| 纯读前沿 | :mod:`~app.agents.orchestration.temporal.logical_read_activities.frontier_read` |
| 副作用前沿（审批后写） | :mod:`~app.agents.orchestration.temporal.logical_read_activities.frontier_effects` |
| 重规划 | :mod:`~app.agents.orchestration.temporal.logical_read_activities.replan` |
| 审批过期 | :mod:`~app.agents.orchestration.temporal.logical_read_activities.approval` |
| 取消/失败收尾 | :mod:`~app.agents.orchestration.temporal.logical_read_activities.lifecycle` |
| 共享判定与心跳 | `_shared.py` / `helpers.py`（包内部） |
"""

from app.agents.orchestration.temporal.logical_read_activities.helpers import (
    _finalize_logical_answer,
    _wait_for_ready_expansion,
)
from app.agents.orchestration.temporal.logical_read_activities.frontier_read import (
    run_logical_read_frontier_activity,
)
from app.agents.orchestration.temporal.logical_read_activities.frontier_effects import (
    run_logical_effects_frontier_activity,
)
from app.agents.orchestration.temporal.logical_read_activities.replan import (
    _try_replan_pure_read_tail,
    replan_logical_read_activity,
)
from app.agents.orchestration.temporal.logical_read_activities.approval import (
    expire_logical_effects_approval_activity,
)
from app.agents.orchestration.temporal.logical_read_activities.lifecycle import (
    cancel_logical_effects_job_activity,
    fail_logical_read_job_activity,
)

__all__ = ['_TERMINAL', '_finalize_logical_answer', '_heartbeat', '_try_replan_pure_read_tail', '_wait_for_ready_expansion', 'cancel_logical_effects_job_activity', 'expire_logical_effects_approval_activity', 'fail_logical_read_job_activity', 'replan_logical_read_activity', 'run_logical_effects_frontier_activity', 'run_logical_read_frontier_activity']
