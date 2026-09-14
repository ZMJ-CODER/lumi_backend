"""任务提交链路的应用侧拆分。

原先根目录一个 `job_submission_service.py` 同时承担：请求上下文构建、意图与能力预检、
计划选择、Job 物化、运行后端选择、提交后的索引与快照。现在按模块收拢：

| 关注点 | 位置 |
| --- | --- |
| 请求上下文（画像/办公文档/LLM 生效配置） | :mod:`~app.agents.orchestration.submission.submission_context_service` |
| 提交前置门禁（准入/去重/形状） | :mod:`~app.agents.orchestration.submission.submission_guard` |
| 计划选择（办公场景策略） | :mod:`~app.agents.orchestration.planning.office_plan_selection_service` |
| Job 物化 | :mod:`~app.agents.orchestration.execution.job_materialization_service` |
| 提交服务本体（编排上述步骤并按执行偏好派发） | :mod:`~app.agents.orchestration.submission.service` |

本 `__init__` 只做**同包聚合再导出**；真实来源永远是具体子模块。
"""

from app.agents.orchestration.submission.service import JobSubmissionService

__all__ = ["JobSubmissionService"]
