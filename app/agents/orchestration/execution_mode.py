"""薄适配层：执行模式/规范状态机语义位于 ``lumi_orch.execution_mode``。

业务层只保留导入路径兼容（API、Job 适配器、测试等继续引用本模块），
真正的判定/映射逻辑归编排内核包所有。
"""

from __future__ import annotations

from lumi_orch.execution_mode import *  # noqa: F401,F403
