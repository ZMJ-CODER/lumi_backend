"""办公 ReAct 执行器（应用层拆分后的**包入口**）。

原 ``react_runner.py`` 是一个 949 行的单体；按职责拆到本包的子模块后，
这里只做两件事：**聚合并导出公开面**，让既有调用点继续按
``from app.agents.orchestration.react_runner import OfficeReactRunner`` 使用。

职责分布见 :mod:`app.agents.orchestration.react.runner` 的模块说明。
"""

from app.agents.orchestration.react.runner import OfficeReactRunner
from app.agents.orchestration.react.state import ReactRunResult, ReactState

__all__ = ["OfficeReactRunner", "ReactRunResult", "ReactState"]
