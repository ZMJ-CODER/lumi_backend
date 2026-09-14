"""受控办公 ReAct 执行器（**包入口门面**）。

实现已按职责拆到 :mod:`app.agents.orchestration.react` 子包：

```text
react/state.py            数据形状（ReactState / ReactRunResult）
react/tool_selection.py   工具发现、名字判定、领域申请
react/workspace_window.py 工作区阶段工具窗口
react/tool_execution.py   执行护栏、去重、失败排除
react/progress.py         进度与结果投影
react/prompt.py           系统提示词组装
react/graph.py            状态机装配（节点/边/路由）
react/runner.py           OfficeReactRunner（运行流程）
```

本文件只做**同包聚合再导出**：既有调用点（``app/agents/roles/react.py`` 与测试）
继续按 ``react_runner`` 这个名字使用，不必跟着搬家。新代码请直接从
``app.agents.orchestration.react`` 或更具体的子模块导入。
"""

from app.agents.orchestration.react import ReactRunResult, ReactState
from app.agents.orchestration.react.runner import OfficeReactRunner

__all__ = ["OfficeReactRunner", "ReactRunResult", "ReactState"]
