"""薄适配层：模型输出统一协议位于 ``lumi_orch.protocol``。

业务层保留本模块仅为兼容既有导入路径；协议解析、DSML/XML 工具形态剥离
等纯逻辑由编排内核包提供。
"""

from __future__ import annotations

from lumi_orch.protocol import *  # noqa: F401,F403
