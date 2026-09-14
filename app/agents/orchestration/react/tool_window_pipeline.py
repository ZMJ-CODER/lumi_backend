"""ReAct 的**候选窗流水线**：每轮把"模型能看见哪些工具"算出来。

从 `react_runner.py` 的 ``agent`` 节点里抽出。顺序是固定的，
每一步都只做一件事，且都能单独测试：

```text
selection.capabilities
  → expand_domain_window     模型申请过领域 → 收窄到该域（写权限另判）
  → add_bootstrap_primitives 自主模式 → 保证探索原语（Read/Glob/Grep/沙箱）在窗内
  → merge_discovered         已发现工具在后续轮次保持可见（L2 会话缓存）
  → ensure_document_discovery 多文档任务把 inspect_document_set 列为强制项
  → （调用方）workspace stage window / 模型可见面收敛
```

每一步都返回新的候选列表（不改原列表），并且**只增不减或按声明收窄**——
"当前阶段明确要求的工具"一律走 ``extra_mandatory``，不会被 8 个名额裁掉。
"""

from __future__ import annotations

from typing import Any

from app.agents.skills.mandatory_tools import apply_tool_window


async def expand_domain_window(runner: Any, capabilities: list[Any]) -> list[Any]:
    """模型申请过领域 ⇒ 下一轮窗口收窄到该域（只读域不含写工具）。"""
    if not runner._requested_domains:
        return capabilities
    from app.agents.skills.executor import get_capabilities_for_scene

    legal_domain = [
        item for item in await get_capabilities_for_scene("office", runner.user_role, runner.user_id)
        if str(item.domain or item.category or "").casefold() == runner._active_domain
    ]
    domain_capabilities = [
        item for item in capabilities
        if str(item.domain or item.category or "").casefold() == runner._active_domain
    ]
    loaded_domain = [
        item for item in runner.discovery_session.loaded_tools.values()
        if str(item.domain or item.category or "").casefold() == runner._active_domain
    ]
    if not (domain_capabilities or loaded_domain or legal_domain):
        return capabilities
    by_name = {item.name: item for item in [*loaded_domain, *legal_domain, *domain_capabilities]}
    if runner._domain_mode != "write":
        by_name = {
            name: item for name, item in by_name.items()
            if not item.write_op and not item.requires_confirmation
        }
    window, _ = apply_tool_window(
        list(by_name.values()),
        limit=8,
        scene="office",
        extra_mandatory=(),
        layer="react.domain_expand",
    )
    return window


async def add_bootstrap_primitives(runner: Any, capabilities: list[Any]) -> list[Any]:
    """自主模式：把探索原语放进窗内（否则纯词法召回可能一个都不给）。"""
    if not (runner.autonomous_mode and not (runner.domain_first and not runner._requested_domains)):
        return capabilities
    from app.agents.skills.executor import get_tool_capability

    bootstrap_names = ("Read", "Glob", "Grep", "run_in_sandbox", "run_static_check")
    # Put exploration primitives first. The previous append
    # approach was ineffective when lexical recall had
    # already filled all eight available slots.
    bootstrap: list[Any] = []
    for tool_name in bootstrap_names:
        candidate = next((item for item in capabilities if item.name == tool_name), None)
        if candidate is None:
            candidate = await get_tool_capability(
                tool_name, "office", runner.user_role, runner.user_id
            )
        if candidate is not None:
            bootstrap.append(candidate)
    found = {item.name for item in bootstrap}
    # 探索原语是"当前阶段明确要求"的工具：同样是强制项，
    # 不能因为名额被其它候选占满而消失（旧写法靠 [:8] 截断）。
    window, _ = apply_tool_window(
        [*bootstrap, *(item for item in capabilities if item.name not in found)],
        limit=8 + len(found),
        scene="office",
        extra_mandatory=tuple(found),
        mandatory_reason="exploration_primitives",
        layer="react.bootstrap",
    )
    return window


async def merge_discovered(runner: Any, capabilities: list[Any]) -> list[Any]:
    """L2 会话缓存：已发现工具在后续轮次保持可见，避免重复检索。"""
    discovered = list(runner.discovery_session.loaded_tools.values())
    if runner._active_domain:
        discovered = [
            item for item in discovered
            if str(item.domain or item.category or "").casefold() == runner._active_domain
        ]
    if not discovered:
        return capabilities
    by_name = {item.name: item for item in [*discovered, *capabilities]}
    if runner._domain_mode != "write":
        by_name = {
            name: item for name, item in by_name.items()
            if not item.write_op and not item.requires_confirmation
        }
    window, _ = apply_tool_window(
        list(by_name.values()),
        limit=8,
        scene="office",
        layer="react.cache_merge",
    )
    return window


async def ensure_document_discovery(
    runner: Any,
    capabilities: list[Any],
    selection: Any,
    internal_docs: list[dict],
) -> tuple[list[Any], Any]:
    """多文档任务：把 ``inspect_document_set`` 作为**操作前提**强制放进窗内。

    返回 ``(capabilities, selection)``——注入会在候选契约里留痕（``selection`` 重建），
    这样"为什么这一轮多了个盘点工具"在审计里看得见。
    """
    if len(internal_docs) < 2:
        return capabilities, selection
    from app.agents.skills.executor import get_tool_capability

    discovery = await get_tool_capability(
        "inspect_document_set", "office", runner.user_role, runner.user_id
    )
    if discovery is None or discovery.name in {item.name for item in capabilities}:
        return capabilities, selection
    # 文档发现是操作前提，与核心工具同级强制：不能被名额裁掉。
    window, _ = apply_tool_window(
        [discovery, *capabilities],
        limit=8 + 1,
        scene="office",
        extra_mandatory=(str(discovery.name),),
        mandatory_reason="document_discovery_prerequisite",
        layer="react.document_discovery",
    )
    # Discovery was injected as a mandatory prerequisite;
    # make that visible in the auditable candidate trace.
    traced = type(selection)(
        capabilities=window,
        candidates=[
            {"name": item.name, "version": item.version, "score": 0.0, "bootstrap": False, "availability_hint": "available"}
            for item in window
        ],
        scene=selection.scene,
        top_score=selection.top_score,
        low_confidence=selection.low_confidence,
        reason="document_discovery_prerequisite",
    )
    return window, traced


__all__ = [
    "add_bootstrap_primitives",
    "ensure_document_discovery",
    "expand_domain_window",
    "merge_discovered",
]
