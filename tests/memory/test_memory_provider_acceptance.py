"""资源能力层验收对象：``memory_provider``（**当前是"只有声明"的状态**）。

方案《资源能力层》Phase 6 的验收条件是原文照抄：

> 增加一个真正的新 Provider 作为验收对象，例如 ``memory_provider``，只提供
> ``resource.read`` / ``resource.write``、``resource_type = memory``；验证它是否可以在
> **不修改 Router / Preflight / ChatGraph / ReActRunner / Broker 静态映射**的情况下
> 被发现、注入和执行。

**2026-09 裁决（两代对照缺口 2a）**：验收对象一度按"已注册"呈现，但
``register_builtin_providers()`` 里从来没有 ``lumi.server.memory`` 这个实现——
"声明说有、运行时没有"会同时污染 Broker 候选、管理端展示、能力发现与测试判断。
补真实现要新增能力名/权限/事件/数据安全边界，属于**独立功能**，不在两代收口的尾巴里做。
因此本文件的口径改为"**只有声明**"：

* ``registered=False`` 且 ``provider_id=""``（与 ``knowledge_provider`` 同形）；
* 声明本身保留（目录、管理端、默认 Provider 映射都在），设计意图记在 ``note`` 里；
* **任何地方都不得把它当成可用**：候选集合为空、Adapter 为 None、Workflow 不注入工具行、
  过程条目/事件里不出现 ``provider_id``——这是本文件最重要的反向断言。

"不改静态映射"那部分仍然成立：验收对象 ``task_memory``（任务内工作记忆）只在工具类上
声明了 ``capability`` / ``resource_type``，资源目录里只多了一条 Provider 声明。
本文件依旧做**反向断言**：``Router / Preflight / ChatGraph / ReActRunner / Broker``
的源码里**不得出现** ``memory_provider`` / ``task_memory`` / ``resource_type = memory``
之类的专属分支——否则"不改静态映射"就只是口号。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.capabilities.catalog import resource as rc

from _paths import REPO_ROOT
_REPO_ROOT = REPO_ROOT


def _read(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.fixture(scope="module", autouse=True)
def _memory_plugin_registered():
    """只加载**验收对象**（``task_memory``）——不调 ``load_skill_plugins()``。

    为什么不用全量插件加载：那会把 ``python_exec`` 等工具也注册进来，而某些既有测试
    依赖"插件尚未加载"的启动边界（例如办公脚本 Agent 在 ``python_exec`` 未注册时
    跳过沙箱预检，直接走 ``run_skill``）。验收测试不该改变别人的前置条件，
    因此这里按文件名精确加载一个插件，并在结束时撤销。
    """
    import importlib.util

    from app.agents.skills.registry import ToolRegistry

    path = Path("plugins/tools/office/task_memory.py").resolve()
    spec = importlib.util.spec_from_file_location("acceptance_task_memory", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    instance = module.TaskMemorySkill()
    ToolRegistry.register(instance, source="plugin")
    try:
        # 把**模块对象**交给用例：插件类的 globals 就是这个模块，打补丁要打在它上面。
        yield module
    finally:
        ToolRegistry.unregister("task_memory")


@pytest.fixture()
def registered_memory_reader():
    """注册一个**只靠声明**接入的记忆读取工具（读侧的验收对象）。

    它不改任何静态映射：``capability="resource.read"`` + ``resource_type="memory"``
    两条类属性就是全部接入成本。
    """
    from app.agents.skills.base import Tool, ToolOutput
    from app.agents.skills.registry import ToolRegistry

    class _MemoryRecall(Tool):
        name = "memory_recall"
        description = "回顾任务内工作记忆"
        category = "office"
        environment = "server"
        scenes = ["office"]
        capability = "resource.read"
        resource_type = "memory"
        parameters_schema = {"type": "object", "properties": {}}

        async def execute(self, params, context=None):
            return ToolOutput(success=True, output="ok", data={})

    ToolRegistry.register(_MemoryRecall(), source="test")
    try:
        yield "memory_recall"
    finally:
        ToolRegistry.unregister("memory_recall")


# ── 1. 发现：声明一次即进入注册表 ───────────────────────────


def test_memory_is_a_declared_resource_type_with_a_declaration_only_provider():
    """声明在，实现不在：这就是当前的**真实状态**，两边必须一致。"""
    assert rc.RESOURCE_MEMORY in rc.RESOURCE_TYPES
    provider = rc.PROVIDERS_BY_NAME["memory_provider"]
    assert provider.resource_types == ("memory",)
    assert set(provider.capabilities) == {rc.UNIFIED_RESOURCE_READ, rc.UNIFIED_RESOURCE_WRITE}
    # "只有声明"的两个标志（与 knowledge_provider 同形）
    assert provider.registered is False
    assert provider.provider_id == ""
    # 设计意图不能丢：计划中的 id 写在 note 里，而不是写在一个不存在的字段上
    assert "lumi.server.memory" in provider.note
    # 默认 Provider 映射仍然指向它（声明层的排序事实，不代表可用）
    assert rc.DEFAULT_PROVIDER_BY_RESOURCE["memory"] == "memory_provider"


def test_declaration_only_provider_is_never_claimed_available():
    """**最重要的一条**：只有声明时，任何"可用性"入口都必须给空。

    收窄集合、Adapter、候选清单是三条不同的入口，漏掉任何一条就会重新出现
    "声明说有、运行时没有"（污染 Broker 选择、管理端展示、能力发现与测试判断）。
    """
    from app.agents.capabilities.broker.resource_dispatch import (
        adapter_for,
        adapter_tool_for,
        provider_ids_for,
        resolve_dispatch,
    )

    assert provider_ids_for("resource.read", "memory") == frozenset()
    assert provider_ids_for("resource.write", "memory") == frozenset()
    assert adapter_for("resource.write", "memory") is None
    assert adapter_tool_for("resource.write", "memory") == "", "没有实现就不编工具名"
    target = resolve_dispatch("task_memory")
    assert target.known is True, "统一层仍然认识这条路线（声明在）"
    assert target.provider_name == "memory_provider"
    assert target.provider_id == "", "不得给出一个不存在的 provider_id"
    # 候选清单仍列出**声明**（目录要能回答"这条路线的设计是什么"）
    assert [spec.name for spec in rc.providers_for("resource.write", "memory")] == [
        "memory_provider"
    ]


def test_task_memory_binding_comes_from_its_own_declaration():
    """绑定来自工具自己的声明（不是静态映射表）。"""
    binding = rc.binding_for_tool("task_memory")
    assert binding.known is True
    assert binding.capability == rc.UNIFIED_RESOURCE_WRITE
    assert binding.resource_type == rc.RESOURCE_MEMORY
    assert binding.provider == "memory_provider"
    # 逻辑 Provider 名是声明，物理 provider_id 是"有没有实现"——后者当前为空。
    assert binding.provider_id == ""
    # 它**不在**任何静态映射表里——这正是"声明一次"的意义
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP

    assert "task_memory" not in TOOL_CAPABILITY_MAP


def test_registry_entry_exposes_memory_metadata():
    from app.agents.capabilities.catalog.tool_registry import entries_by_name

    entry = entries_by_name().get("task_memory")
    assert entry is not None, "插件工具必须进入统一注册表"
    assert entry.unified_capability == rc.UNIFIED_RESOURCE_WRITE
    assert entry.resource_type == rc.RESOURCE_MEMORY
    assert entry.resource_provider == "memory_provider"
    assert entry.provider_candidates == (), "只有声明 ⇒ 没有候选（不能被当成可用）"
    payload = entry.as_dict()
    assert payload["resource_type"] == "memory"


def test_task_memory_is_no_longer_unbound():
    from app.agents.capabilities.catalog.resource import unbound_tools

    assert "task_memory" not in unbound_tools(["task_memory", "AskUserQuestion"])


def test_declaration_only_provider_never_reaches_the_event_labels():
    """缺口 2a：只有声明 ⇒ 过程条目/事件里**不能**出现 ``provider_id``。

    这是"声明说有、运行时没有"最容易被忽视的出口：``provider_id`` 会进过程条目与
    事件流，前端与历史回放都按它做展示。逻辑 Provider 名（声明）可以发，
    物理 provider_id 必须等实现注册之后才有值（工具已由模块级 fixture 注册）。
    """
    from app.agents.capabilities.broker.resource_dispatch import dispatch_labels

    labels = dispatch_labels("task_memory")
    assert (labels["capability"], labels["resource_type"]) == ("resource.write", "memory")
    assert labels["provider_name"] == "memory_provider"
    assert "provider_id" not in labels, labels


def test_memory_provider_capabilities_are_reachable():
    assert [spec.name for spec in rc.providers_for("resource.read", "memory")] == ["memory_provider"]
    assert [spec.name for spec in rc.providers_for("resource.write", "memory")] == ["memory_provider"]
    # 其它资源类型不受影响（新 Provider 不能抢别人的候选）
    assert "memory_provider" not in [
        spec.name for spec in rc.providers_for("resource.write", "workspace")
    ]


# ── 2. 注入：预检与工具窗口按资源类型选出它 ──────────────────


def test_preflight_window_includes_memory_tool(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WINDOW", True)
    from app.agents.orchestration.preflight.capability_preflight import tool_window_for_actions

    # CREATE 对应资源写入：task_memory 自己声明的是 resource.write（它能改记忆）
    window = tool_window_for_actions(["CREATE"], resource_types=["memory"])
    assert "task_memory" in window, window
    # 工作区资源不受影响（同一个意图）
    workspace_window = tool_window_for_actions(["CREATE"], resource_types=["workspace"])
    assert "task_memory" not in workspace_window


def test_resource_window_derives_memory_tools(monkeypatch):
    """工具窗口按**资源类型 + 工具自己的声明**推导工具——与 Provider 是否注册无关。

    这是"哪些工具能对 memory 资源做写操作"（工具层问题），不是"这个能力现在可用吗"
    （Provider 层问题，见 ``test_declaration_only_provider_is_never_claimed_available``）。
    ``task_memory`` 是真工具、也真的声明了绑定，因此它在窗口里；而 Provider 候选为空。
    """
    from app.agents.capabilities.policy import resource_window as rw

    plan = rw.plan_for_actions(["CREATE"], resource_types=["memory"])
    assert plan.capabilities == ("resource.write",)
    assert plan.tools_by_capability == (("CREATE", "resource.write", "task_memory"),)
    providers = {row[0]: row[2] for row in plan.providers_by_capability}
    # 逻辑 Provider 名（声明）仍然出现在窗口诊断里……
    assert providers["resource.write"] == ("memory_provider",)
    # ……但物理候选为空：声明不等于实现（缺口 2a）。
    from app.agents.capabilities.broker.resource_dispatch import provider_ids_for

    assert provider_ids_for("resource.write", "memory") == frozenset()


def test_read_side_of_the_same_provider_is_reachable(registered_memory_reader, monkeypatch):
    """读侧：同一个 Provider 的 ``resource.read`` 也能按资源类型选出工具。

    现有 ``task_memory`` 用 ``action`` 同时承担读/写而**只能声明一个能力**，因此读侧
    用一个最小替身（它同样只声明 ``capability``/``resource_type``）——这恰好证明
    "新资源 + 新工具 = 声明一次"，而不是"改静态表"。
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WINDOW", True)
    from app.agents.orchestration.preflight.capability_preflight import tool_window_for_actions

    window = tool_window_for_actions(["READ"], resource_types=["memory"])
    assert "memory_recall" in window, window
    assert "task_memory" not in window, "写工具不该出现在只读窗口里"


def test_workflow_can_require_memory_without_code_changes(registered_memory_reader):
    """Workflow 只声明能力+资源类型即可被能力层理解——但**没有实现的 Provider 不注入工具行**。

    两条路径回答的是两个不同问题，本用例把它们分开断言，避免"工具可见"被误读成"能力可用"：

    * **声明层**（``effective_providers``）：仍然列出 ``memory_provider``——目录要能回答
      "这条路线的设计是什么"；
    * **依赖注入层**（``capability_tool_rows``）：Provider 未注册 ⇒ 派生不出任何工具行，
      Skill 不会以为自己拿到了 memory 能力（这正是缺口 2a 要消除的 false positive）。
    """
    from app.agents.capabilities.catalog.tool_registry import resolve_tool
    from app.agents.skills.base import WorkflowSkill

    class _MemorySkill(WorkflowSkill):
        name = "memory_helper"
        version = "1.0.0"
        required_capabilities = ["resource.read"]
        resource_types = ["memory"]

    skill = _MemorySkill()
    assert skill.effective_capabilities() == ["resource.read"]
    assert skill.effective_resource_types() == ["memory"]
    assert "memory_provider" in skill.effective_providers()
    assert skill.capability_tool_rows() == [], "只有声明 ⇒ 不得注入工具行"
    # 但"工具自己声明了绑定"仍然有效：这是"声明一次"的价值所在（与 Provider 是否注册无关）。
    entry = resolve_tool("memory_recall")
    assert entry is not None
    assert entry.unified_capability == rc.UNIFIED_RESOURCE_READ
    assert entry.resource_type == rc.RESOURCE_MEMORY


# ── 3. 执行：工具真的能跑，但派发层不编 Provider ──────────────


def test_dispatch_keeps_the_declaration_but_invents_nothing():
    from app.agents.capabilities.broker.resource_dispatch import resolve_dispatch

    target = resolve_dispatch("task_memory")
    assert target.known is True, "统一层认识它（判据是统一能力+资源类型）"
    assert target.unified_capability == "resource.write"
    assert target.resource_type == "memory"
    # 逻辑 Provider 名是声明；物理 provider_id 只有在实现注册后才会有值。
    assert target.provider_name == "memory_provider"
    assert target.provider_id == ""
    # 纯声明式工具在**旧**能力目录里没有条目 → 旧能力名为空，走服务端内联执行路径
    assert target.capability == ""


@pytest.mark.asyncio
async def test_memory_tool_executes_through_the_standard_path(_memory_plugin_registered, monkeypatch):
    """端到端：`execute_tool_call` 走既有注册表路径把工具跑起来（租约/审批链不变）。

    ``allow_internal=True``：``task_memory`` 目前是**内部执行实现**（未登记进
    ``base_tools.yaml`` 公共池，办公 Agent 按名字显式调用）。把它放进公共模型池是
    方案 §六"白名单只描述安全策略"那一步的事，不属于本阶段——本阶段要证明的是
    "统一资源能力层能把它解析并执行"，而不是"它已经对模型公开"。
    """
    from app.agents.skills.executor import execute_tool_call

    module = _memory_plugin_registered
    captured: list[str] = []

    async def _recall(job_id):
        captured.append(job_id)
        return {"已读文件": "README.md"}

    monkeypatch.setattr(module, "recall", _recall)
    result = await execute_tool_call(
        {"function": {"name": "task_memory", "arguments": '{"action": "recall"}'}},
        user_id="u1",
        scene="office",
        conversation_id="job-1",
        allow_internal=True,
    )
    assert result.success is True, result.error
    assert captured == ["job-1"], "工具确实被执行了（拿到了任务上下文）"
    assert "README.md" in str(result.output)


# ── 4. 反向断言：没有为它加任何专属分支 ─────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "app/agents/orchestration/preflight/capability_preflight.py",
        "app/agents/orchestration/preflight/capability_preflight_service.py",
        "app/agents/langchain/chat_graph.py",
        # ReAct 执行器已拆成 react/ 子包：反向断言要覆盖**整包**，
        # 否则代码一搬家这条保护就名存实亡。
        "app/agents/orchestration/react_runner.py",
        "app/agents/orchestration/react/runner.py",
        "app/agents/orchestration/react/tool_selection.py",
        "app/agents/orchestration/react/tool_execution.py",
        "app/agents/orchestration/react/workspace_window.py",
        "app/agents/capabilities/broker/broker.py",
        "app/agents/capabilities/broker/dispatch.py",
        "app/agents/capabilities/registry/builtin.py",
        "app/agents/capabilities/catalog/legacy.py",
    ],
)
def test_no_memory_specific_branch_in_routing_modules(rel):
    """Router/Preflight/ChatGraph/ReActRunner/Broker **不得**出现记忆专属分支。

    这是"新增 Provider 不改静态映射"的**反向证据**：只要有人在这些模块里写
    ``if resource_type == "memory"`` 或点名 ``task_memory``，这条断言就会失败。
    """
    source = _read(rel)
    for needle in ("memory_provider", "task_memory", '"memory"', "'memory'"):
        assert needle not in source, f"{rel} 出现了记忆专属分支：{needle}"


def test_static_mapping_tables_have_no_memory_entries():
    """三张静态映射表里**没有**任何记忆条目：它是纯声明式接入的。"""
    from app.agents.capabilities.registry.builtin import TOOL_CAPABILITY_MAP
    from app.agents.capabilities.catalog.legacy import IMPLEMENTATION_MAP
    from app.agents.capabilities.broker.dispatch import CAPABILITY_TOOL_MAP
    from app.agents.orchestration.preflight.capability_preflight import ACTION_TOOL_WINDOW

    for table in (TOOL_CAPABILITY_MAP, IMPLEMENTATION_MAP, CAPABILITY_TOOL_MAP):
        assert not any("memory" in str(key) or "memory" in str(value) for key, value in table.items())
    assert not any("memory" in str(item) for tools in ACTION_TOOL_WINDOW.values() for item in tools)


def test_adding_a_provider_keeps_shadow_parity_intact():
    """新增 Provider 不得破坏"切真相源前零差异"的前提。"""
    from app.agents.capabilities.views.tool_shadow import (
        SHADOW_PARITY_DIMENSIONS,
        shadow_compare,
        shadow_parity_totals,
    )

    diffs = shadow_compare()
    for dimension in SHADOW_PARITY_DIMENSIONS:
        assert diffs.get(dimension) == [], (dimension, diffs.get(dimension))
    assert shadow_parity_totals(diffs)["switch_safe"] is True


def test_memory_tool_is_not_converged_into_the_model_surface():
    """Phase 5 的收敛面对新资源**默认保守**：只收敛工作区资源，其余保留原名。

    （这条规则正是本阶段验收暴露出来的：统一能力跨多种资源时，模型只叫 ``Write``
    无法判断该落哪个 Provider，因此 memory / office_document 一律不改名。）
    """
    from app.agents.capabilities.views.resource_surface import (
        CONVERGED_RESOURCE_TYPES,
        display_name_for,
        hidden_tools,
    )

    assert CONVERGED_RESOURCE_TYPES == frozenset({"workspace"})
    assert display_name_for("task_memory") == "task_memory"
    assert "task_memory" not in hidden_tools(["task_memory"])
