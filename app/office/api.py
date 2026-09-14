"""Office 域 · **对外公开接口**（P4 收尾）。

跨域依赖只能走这里（门禁规则 2/3 + ``DOMAIN_PUBLIC["office"] = ("app.office.api",)``）。

本域公开的东西分两类：

1. **编排/执行层需要的写入接口**：``push_delta`` / ``read_deltas``（办公输出的短生命周期
   文本流）、``ensure_session``（会话登记）——执行节点与步骤服务在跑办公任务时要写它们；
2. **能力层需要的 Provider 落点**：``render_document``（确定性渲染）、``generic_outputs_dir``
   （产物目录）、``resolve_generic_output``（按名字解析产物路径）——Provider 适配器把
   能力调用接到这些实现上；
3. **共享的 LLM 调用封装与路由哨兵**：``office_llm`` / ``SENTINEL`` / ``SENSITIVE_WORDS``。

**不公开**：``app.office.docs`` 这个 1500 行的编辑引擎本体——办公 Worker 角色用它，
那条依赖作为**已知存量**留在基线上（写明了理由），而不是把它整个变成公开面。

本模块是**同包聚合**（不是跨包转发壳）：只 re-export 本包内的名字，因此不受规则 4 约束。
"""

from __future__ import annotations

from app.office.docs import (
    ensure_session,
    generic_outputs_dir,
    resolve_generic_output,
)
from app.office.render import render_document
from app.office.skill_utils import (
    ROUTE_SENTINEL_PREFIX,
    SENSITIVE_WORDS,
    office_llm,
)
from app.office.stream import push_delta, read_deltas

__all__ = [
    "ROUTE_SENTINEL_PREFIX",
    "SENSITIVE_WORDS",
    "ensure_session",
    "generic_outputs_dir",
    "office_llm",
    "push_delta",
    "read_deltas",
    "render_document",
    "resolve_generic_output",
]
