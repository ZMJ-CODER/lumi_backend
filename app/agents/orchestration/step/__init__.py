"""Step 运行的应用侧拆分。

`step_run_service.py` 原先同时负责状态适配、执行、checkpoint 持久化、过程日志与
事件投影、终态收尾。**不搬去 `lumi_execution`**：它仍然带着应用层的 Job、事件与
持久化依赖。本包的模块划分：

| 关注点 | 模块 |
| --- | --- |
| Step 状态适配（Job/routing ↔ ``StepRunState``） | :mod:`~app.agents.orchestration.step.state_adapter` |
| 过程日志与事件投影（与刷新快照同源的文案） | :mod:`~app.agents.orchestration.step.presentation` |
| checkpoint 与结果引用持久化 | :mod:`~app.agents.orchestration.step.persistence` |

``StepRunService`` 本体仍在 `step_run_service.py`（对外入口不变），它从这里
导入上述实现——既有调用点与测试不需要改路径。
"""

from app.agents.orchestration.step.persistence import (
    _persist_node_result_ref,
    _record_step_checkpoints,
)
from app.agents.orchestration.step.presentation import (
    _display_text,
    _live_presentation_fields,
    _presentation_node,
    _result_summary,
    _text_or,
)
from app.agents.orchestration.step.state_adapter import (
    _current_step_id,
    _dependencies_done,
    _effect_type_for_node,
    _state_from_job,
    locate_current_step,
)

__all__ = [
    "_current_step_id",
    "_dependencies_done",
    "_display_text",
    "_effect_type_for_node",
    "_live_presentation_fields",
    "_persist_node_result_ref",
    "_presentation_node",
    "_record_step_checkpoints",
    "_result_summary",
    "_state_from_job",
    "_text_or",
    "locate_current_step",
]
