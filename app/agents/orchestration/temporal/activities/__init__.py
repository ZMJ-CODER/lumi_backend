"""编排 Activity（按运行族分子模块）。

原本一个 `activities.py` 混着五类活动；现在按运行族分开，`__init__` 只做**聚合再导出**——
`from app.agents.orchestration.temporal.activities import execute_node_activity` 与
Worker 的注册列表都保持原样。

| 族 | 模块 |
| --- | --- |
| 静态 DAG 节点执行 | :mod:`~app.agents.orchestration.temporal.activities.static_dag` |
| 结果引用持久化 | :mod:`~app.agents.orchestration.temporal.activities.persistence` |
| 静态任务重规划 | :mod:`~app.agents.orchestration.temporal.activities.replan` |
| 终答合成 | :mod:`~app.agents.orchestration.temporal.activities.synthesis` |
| 生命周期与清理 | :mod:`~app.agents.orchestration.temporal.activities.lifecycle` |

**为什么叫同名包而不是新名字**：Activity 由 Workflow 按名字调用，改导入路径会同时
动 Worker 注册、Workflow 调用与测试；同名包把"拆分"限制在包内部。
"""

from app.agents.orchestration.temporal.activities.static_dag import (
    _install_node_deadline,
    _json_safe,
    _execute_node_activity_inner,
    execute_node_activity,
)
from app.agents.orchestration.temporal.activities.persistence import (
    persist_node_result_ref_activity,
)
from app.agents.orchestration.temporal.activities.replan import (
    replan_static_job_activity,
)
from app.agents.orchestration.temporal.activities.synthesis import (
    synthesize_final_answer_activity,
)
from app.agents.orchestration.temporal.activities.lifecycle import (
    cleanup_job_secrets_activity,
)

__all__ = ['_execute_node_activity_inner', '_install_node_deadline', '_json_safe', 'cleanup_job_secrets_activity', 'execute_node_activity', 'persist_node_result_ref_activity', 'replan_static_job_activity', 'synthesize_final_answer_activity']
