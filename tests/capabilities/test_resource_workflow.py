"""统一资源能力层 Phase 4（Workflow Skill 依赖迁移）回归。

Phase 4 把 Workflow Skill 的依赖从"14 个底层 MCP 名字"迁移到"能力声明"：

```text
required_capabilities = [resource.read, resource.write, resource.edit,
                         resource.move, resource.delete, code.execute]
resource_types        = [workspace]
providers             = [workspace_provider]
```

四条不变量：

1. **只补不替**：能力派生的依赖行与旧 ``allowed_tools`` 行**并存**。替换会让
   ``resolve_dependencies`` 看到的依赖变少——那是"依赖检查变松"，是回归；
2. **迁移期不出圈**：能力解析出来的工具必须是该 Skill 本来就会声明的工具
   （底部有断言），否则"迁移"会悄悄扩大 Skill 的工具面；
3. **歧义不猜**：只声明 ``resource.write`` 而不给资源类型 → 不解析工具；
4. **关闭开关逐字不变**：旧 Skill/用户自建 Skill 只声明 ``allowed_tools`` 时，
   行为与改造前一致（能力行只在显式声明能力时出现）。
"""

from __future__ import annotations

import json

import pytest

from app.agents.capabilities.policy import resource_workflow as rwf
from app.agents.skills.base import WorkflowSkill


class _Declared(WorkflowSkill):
    name = "declared_skill"
    version = "1.0.0"
    allowed_tools = ["mcp__lumi_client__workspace_write"]
    required_capabilities = ["resource.write", "sandbox.run", "nonsense", "resource.write"]
    resource_types = ["workspace"]


class _LegacyOnly(WorkflowSkill):
    name = "legacy_skill"
    version = "1.0.0"
    allowed_tools = [
        "mcp__lumi_client__workspace_navigator",
        "mcp__lumi_client__workspace_write",
        "mcp__lumi_client__web_search",
    ]


class _Ambiguous(WorkflowSkill):
    name = "ambiguous_skill"
    version = "1.0.0"
    required_capabilities = ["resource.write"]


@pytest.fixture()
def workflow_on(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WORKFLOW", True)
    return settings


@pytest.fixture()
def workflow_off(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "RESOURCE_CAPABILITY_WORKFLOW", False)
    return settings


# ── 1. 声明解析 ─────────────────────────────────────────────


def test_flag_defaults_to_off():
    from app.core.config import settings

    assert settings.RESOURCE_CAPABILITY_WORKFLOW is False
    assert rwf.workflow_enabled() is False


def test_declared_capabilities_are_normalized_and_filtered():
    skill = _Declared()
    # 别名归一（sandbox.run → code.execute）、去重、非法值丢弃
    assert skill.declared_capabilities() == ["resource.write", "code.execute"]
    assert skill.effective_capabilities() == ["resource.write", "code.execute"]
    assert skill.effective_resource_types() == ["workspace"]


def test_legacy_skill_capabilities_are_derived_for_compat():
    """老 Skill 不改一行：能力从 ``allowed_tools`` 推导（兼容解析）。"""
    skill = _LegacyOnly()
    assert skill.declared_capabilities() == []
    assert skill.effective_capabilities() == ["resource.read", "resource.write"]
    assert skill.effective_resource_types() == ["workspace"]
    assert skill.capability_dependencies()["declared"] is False


def test_ambiguous_capability_without_resource_type_is_not_guessed():
    """``resource.write`` 跨多种资源：没给资源类型就不解析（也不猜一个）。"""
    skill = _Ambiguous()
    assert skill.effective_resource_types() == []
    assert rwf.tools_for_capabilities(skill.effective_capabilities(), []) == []


def test_effective_providers_is_declared_plus_derived():
    skill = _Declared()
    providers = skill.effective_providers()
    assert "workspace_provider" in providers  # 声明
    # 由能力/资源推导出来的候选也在（去重保序）
    assert len(providers) == len(set(providers))
    assert set(providers) <= set(__import__("app.agents.capabilities.catalog.resource", fromlist=["x"]).PROVIDERS_BY_NAME)


def test_capability_dependencies_is_json_safe():
    payload = _Declared().capability_dependencies()
    json.dumps(payload, ensure_ascii=False)
    assert set(payload) == {"capabilities", "resource_types", "providers", "declared"}


# ── 2. 依赖清单：只补不替 ───────────────────────────────────


def test_effective_dependencies_keeps_legacy_rows_and_adds_capability_rows():
    skill = _Declared()
    manifest = skill.effective_dependencies()
    rows = manifest["tools"]
    legacy = [row for row in rows if row.get("via") != "capability"]
    capability = [row for row in rows if row.get("via") == "capability"]
    assert legacy, "旧的 allowed_tools 依赖行必须保留（迁移期兼容）"
    assert {row["name"] for row in legacy} == {"mcp__lumi_client__workspace_write"}
    assert capability, "能力声明必须派生出依赖行"
    # 能力派生的行是**补充**：不设为必需，避免"声明了能力就把旧依赖判死"
    assert all(row["required"] is False for row in capability)
    assert manifest["capabilities"] == ["resource.write", "code.execute"]
    assert manifest["resource_types"] == ["workspace"]
    assert "workspace_provider" in manifest["providers"]


def test_legacy_only_skill_manifest_has_no_capability_rows():
    """没有能力声明的 Skill：依赖清单逐字不变（关闭开关时的等价性）。"""
    skill = _LegacyOnly()
    manifest = skill.effective_dependencies()
    assert all(row.get("via") != "capability" for row in manifest["tools"])
    assert len(manifest["tools"]) == len(skill.allowed_tools)
    # 但它仍然能被能力层"读懂"（兼容解析），供 Phase 5 收敛时使用
    assert manifest["capabilities"] == ["resource.read", "resource.write"]


def test_capability_rows_do_not_break_dependency_resolution():
    """能力行参与依赖解析时不能让一个本来可用的 Skill 变成不可用。"""
    from app.agents.skills.dependencies import resolve_dependencies

    skill = _Declared()
    manifest = skill.effective_dependencies()
    available = {
        row["name"]: {"version": "1.0.0", "provider": "desktop_mcp", "environment": "client"}
        for row in manifest["tools"]
    }
    report = resolve_dependencies(manifest, available)
    assert report.required_issues == [], [issue.message for issue in report.required_issues]


# ── 3. 能力 ↔ 工具解析 ──────────────────────────────────────


def test_legacy_tools_to_capabilities():
    assert rwf.legacy_tools_to_capabilities([
        "mcp__lumi_client__workspace_write",
        "mcp__lumi_client__workspace_navigator",
        "mcp__lumi_client__sandbox_run",
    ]) == ["resource.write", "resource.read", "code.execute"]
    assert rwf.legacy_tools_to_capabilities(["totally_unknown"]) == []
    assert rwf.legacy_tools_to_capabilities([]) == []


def test_tools_for_capabilities_uses_provider_adapter():
    """能力 → 工具走 Provider Adapter（Phase 3），给出**客户端规范的**工具名。

    ``code.execute`` 解析成 ``sandbox_run``（客户端 MCP 规范名，``CAPABILITY_TOOL_MAP``
    的登记值），而不是服务端实现名 ``run_in_sandbox``——依赖检查要与桌面端广告的
    能力名对齐，因此这里必须用前者。
    """
    tools = rwf.tools_for_capabilities(
        ["resource.read", "resource.write", "code.execute"], ["workspace"]
    )
    assert tools == ["workspace_navigator", "workspace_write", "sandbox_run"]
    # 资源类型为空 → 不解析（歧义）
    assert rwf.tools_for_capabilities(["resource.write"], []) == []
    # 非法能力名直接跳过
    assert rwf.tools_for_capabilities(["nonsense"], ["workspace"]) == []


def test_select_capabilities_by_declaration():
    from app.agents.skills.capability import ToolCapability

    pool = [
        ToolCapability(name="workspace_navigator"),
        ToolCapability(name="mcp__lumi_client__workspace_write"),
        ToolCapability(name="mcp__lumi_client__sandbox_run"),
        ToolCapability(name="mcp__lumi_client__office_doc_edit"),
        ToolCapability(name="totally_unknown"),
    ]
    selected = rwf.select_capabilities(
        pool, capabilities=["resource.write"], resource_types=["workspace"]
    )
    names = [item.name for item in selected]
    assert names == ["mcp__lumi_client__workspace_write"], names
    # 旧名字白名单兜底（迁移期只补不替）
    legacy = rwf.select_capabilities(
        pool, capabilities=["resource.write"], resource_types=["workspace"],
        legacy_names=["mcp__lumi_client__sandbox_run"],
    )
    assert {item.name for item in legacy} == {
        "mcp__lumi_client__workspace_write",
        "mcp__lumi_client__sandbox_run",
    }
    # 什么都不声明 → 不选（绝不"全选"）
    assert rwf.select_capabilities(pool) == []


def test_declared_declarations_shape():
    view = rwf.declared_declarations(_Declared())
    json.dumps(view, ensure_ascii=False)
    assert view["capabilities"] == ["resource.write", "code.execute"]
    assert view["tools"] == ["mcp__lumi_client__workspace_write"]
    assert view["declared"] is True
    legacy_view = rwf.declared_declarations(_LegacyOnly())
    assert legacy_view["declared"] is False
    assert legacy_view["capabilities"] == ["resource.read", "resource.write"]
    assert rwf.workflow_capability_dependencies(_Declared())["resource_types"] == ["workspace"]


# ── 4. 真实 Skill：迁移期不出圈 ─────────────────────────────


@pytest.mark.parametrize("module_name", ["workspace_code_change", "workspace_operation"])
def test_real_workspace_skills_declare_capabilities(module_name):
    from importlib import import_module

    module = import_module(f"plugins.workflows.developer.{module_name}")
    skill = next(
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, WorkflowSkill) and obj is not WorkflowSkill
    )()
    assert skill.declared_capabilities() == [
        "resource.read", "resource.write", "resource.edit",
        "resource.move", "resource.delete", "code.execute",
    ]
    assert skill.effective_resource_types() == ["workspace"]
    assert "workspace_provider" in skill.effective_providers()
    # 底层名字仍是兼容层（Phase 5 之前不删）
    assert skill.allowed_tools


@pytest.mark.parametrize("module_name", ["workspace_code_change", "workspace_operation"])
def test_capability_resolution_stays_within_declared_tools(module_name):
    """**迁移期不出圈**：能力解析出来的工具必须本来就在该 Skill 的旧声明里。

    否则"迁移到能力声明"会悄悄扩大 Skill 的工具面——那是安全边界变化，
    必须由人显式决定，而不是 PR 里顺手发生。
    """
    from importlib import import_module

    module = import_module(f"plugins.workflows.developer.{module_name}")
    skill = next(
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, WorkflowSkill) and obj is not WorkflowSkill
    )()
    declared = {str(name).split("__")[-1] for name in skill.allowed_tools}
    resolved = rwf.tools_for_capabilities(
        skill.effective_capabilities(), skill.effective_resource_types()
    )
    assert resolved, "能力声明必须能解析出工具"
    assert set(resolved) <= declared, set(resolved) - declared


def test_select_capabilities_falls_back_to_legacy_names(workflow_on):
    """开关打开但统一层不认识这些名字时，旧白名单仍然选中（只补不替）。"""
    from app.agents.skills.capability import ToolCapability

    pool = [ToolCapability(name="mcp__lumi_client__workspace_write")]
    selected = rwf.select_capabilities(
        pool,
        capabilities=["resource.write"],
        resource_types=["workspace"],
        legacy_names=["mcp__lumi_client__workspace_write"],
    )
    assert [item.name for item in selected] == ["mcp__lumi_client__workspace_write"]


def test_workflow_skill_without_tools_or_capabilities_is_safe():
    class _Empty(WorkflowSkill):
        name = "empty_skill"
        version = "1.0.0"

    skill = _Empty()
    assert skill.effective_capabilities() == []
    assert skill.effective_resource_types() == []
    assert rwf.declared_declarations(skill)["capabilities"] == []
    assert skill.capability_tool_rows() == []
