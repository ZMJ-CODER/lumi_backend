"""P2（能力域内部纵切）的落地回归：**只改目录，不改行为**。

P2 把 ``app/agents/capabilities/`` 从 21 个平铺模块整理成 7 个子包。这一阶段最容易
出两类错，测试就盯这两类：

1. **漏改引用**：旧扁平路径（``app.agents.capabilities.catalog`` 等）还在某处被 import。
   注意 ``from app.agents.capabilities import <子模块>`` 这种形状**正则扫不到**——
   第一轮搬家就漏了 8 个测试文件，所以这里用 AST 解析（``ImportFrom`` 的 level/module/names
   都能看），并且只允许"包级公开名"。
2. **悄悄改了包级公开面**：``app.agents.capabilities.__all__`` 是旧一代的稳定入口，
   外部 100+ 处引用它，P2 不许动它——这里把 66 个名字逐字钉住。

再加一条"四分类标注"的进度断言：静态词表已拆到 ``catalog.tool_tables``，
且 ``tool_registry`` 对其**原样再导出**（拆段不许改调用面）。
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT

CAP = "app.agents.capabilities"

#: P2 的子包 → 子包内的模块（旧基名保留；catalog 两个文件改名以陈述"两代并存"）。
LAYOUT: dict[str, tuple[str, ...]] = {
    "contracts": ("context",),
    "catalog": ("legacy", "resource", "tool_registry", "tool_tables"),
    "registry": ("registry", "builtin", "resolver"),
    "policy": (
        "gate", "policy_guard", "policy_packs", "approvals", "routing",
        "resource_window", "resource_workflow",
    ),
    "broker": ("broker", "dispatch", "resource_dispatch"),
    "audit": ("audit",),
    "views": ("views", "snapshots", "resource_surface", "tool_shadow"),
}

#: 搬家前的扁平模块名。**分两类**：
#: * 名字被搬走、原路径彻底消失（16 个）；
#: * 名字恰好与**新子包同名**（catalog / registry / broker / audit / views，5 个）——
#:   这些路径仍然 import 得到，但含义从"模块"变成了"子包"，里面不再有同名子模块。
GONE_FLAT_MODULES: tuple[str, ...] = (
    "context", "resource_catalog", "tool_registry", "builtin",
    "resolver", "gate", "policy_guard", "policy_packs", "approvals", "routing",
    "resource_window", "resource_workflow", "dispatch", "resource_dispatch",
    "snapshots", "resource_surface",
)

#: 旧模块名与新子包同名的五个：路径还在，但要断言"模块 → 子包"的语义变化。
RENAMED_TO_SUBPACKAGE: tuple[str, ...] = ("catalog", "registry", "broker", "audit", "views")

#: 包级公开面（P2 之前 = P2 之后，逐字一致）。改这个列表等于改对外契约，必须是有意为之。
PACKAGE_PUBLIC_API: tuple[str, ...] = (
    "ABSTRACT_CAPABILITY_MAP", "ApprovalVerdict", "BUILTIN_POLICY_IDS",
    "CAPABILITY_ARTIFACT_CREATE", "CAPABILITY_CODE_EXECUTE", "CAPABILITY_CODE_SCAN",
    "CAPABILITY_GIT_OPERATIONS", "CAPABILITY_TOOL_MAP", "CAPABILITY_WORKSPACE_DELETE",
    "CAPABILITY_WORKSPACE_EDIT", "CAPABILITY_WORKSPACE_MOVE", "CAPABILITY_WORKSPACE_READ",
    "CAPABILITY_WORKSPACE_WRITE", "WORKSPACE_OPERATION_CAPABILITIES", "DEFAULT_POLICY_ID",
    "IMPLEMENTATION_MAP", "MODE_ACTIVE", "MODE_OFF", "MODE_READ_ONLY", "MODE_SHADOW",
    "NEVER_FALLBACK_CAPABILITIES", "POLICY_COST_SAVER", "POLICY_ENTERPRISE_AUDIT",
    "POLICY_HIGH_PRECISION", "POLICY_MANUAL_COMMIT", "PROVIDER_CLIENT_CODE",
    "PROVIDER_CLIENT_GIT", "PROVIDER_CLIENT_WORKSPACE", "PROVIDER_SERVER_ARTIFACT",
    "PolicyPack", "PolicyPackRegistry", "PolicyVerdict", "PluginPolicyGuard",
    "RoutingDecision", "SERVER_EXECUTABLE_CAPABILITIES", "SERVER_INLINE_CAPABILITIES",
    "AgentExecutionContext", "CapabilityCatalog", "CapabilityDispatchAdapter",
    "CapabilityProvider", "CapabilityRegistry", "CapabilityResolution", "CapabilityResolver",
    "DispatchOutcome", "ProviderRegistration", "RequiredCapabilitiesReport",
    "adapt_to_mcp_tool", "builtin_providers", "capability_catalog", "capability_fingerprint",
    "capability_for_mcp_tool", "capability_registry", "capability_requires_client",
    "concrete_capabilities", "descriptor_allows_deployment", "issue_approval_token",
    "maybe_route_capability", "mcp_tool_for_capability", "normalize_mode", "policy_guard",
    "policy_packs", "register_builtin_providers", "routing_mode", "select_policy_id",
    "should_route", "validate_approval",
)


def _gate():
    path = REPO_ROOT / "tools" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("_arch_gate_p2", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_arch_gate_p2"] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()


@pytest.mark.parametrize("subpackage,modules", sorted(LAYOUT.items()))
def test_subpackage_layout_is_importable(subpackage, modules):
    for name in modules:
        importlib.import_module(f"{CAP}.{subpackage}.{name}")


@pytest.mark.parametrize("name", GONE_FLAT_MODULES)
def test_old_flat_paths_are_gone(name):
    """扁平路径必须彻底消失：留着就会变成"两套路径"，P3 抽包时无法判断谁是权威。"""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"{CAP}.{name}")


@pytest.mark.parametrize("name", RENAMED_TO_SUBPACKAGE)
def test_same_named_old_modules_became_subpackages(name):
    """``catalog`` 等五个名字现在是**子包**（模块 → 子包），旧"平铺模块"语义不复存在。"""
    package = importlib.import_module(f"{CAP}.{name}")
    assert hasattr(package, "__path__"), f"{CAP}.{name} 应该是子包"


def test_catalog_module_was_renamed_to_legacy():
    """唯一改名的模块：``catalog.py`` → ``catalog/legacy.py``（名字本身陈述"两代并存"）。"""
    importlib.import_module(f"{CAP}.catalog.legacy")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"{CAP}.catalog.catalog")


def test_package_public_api_is_unchanged():
    package = importlib.import_module(CAP)
    assert tuple(sorted(package.__all__)) == tuple(sorted(PACKAGE_PUBLIC_API))
    for name in PACKAGE_PUBLIC_API:
        assert hasattr(package, name), f"包级入口少了 {name}"


def test_no_module_imports_old_flat_paths_or_submodules_as_package_members():
    """AST 扫描（不是正则）：既查旧扁平路径，也查 "from 包 import 子模块" 这种形状。

    允许的两种写法：
      * ``from app.agents.capabilities import <__all__ 里的公开名>``（包级入口）；
      * ``from app.agents.capabilities.<子包>.<模块> import <名字>``（新路径）。
    """
    me = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    package_public = set(PACKAGE_PUBLIC_API)
    subpackages = set(LAYOUT)
    offenders: list[str] = []
    for path in gate.iter_python_files(["app", "packages", "scripts", "tests", "plugins", "celery_app", "tools"]):
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
        if rel == me:
            continue  # 本文件必须写下旧名字才能断言它不存在
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module == CAP:
                for alias in node.names:
                    if alias.name not in package_public and alias.name != "*":
                        offenders.append(f"{rel}:{node.lineno} from {CAP} import {alias.name}")
            elif module.startswith(CAP + "."):
                tail = module[len(CAP) + 1:]
                head = tail.split(".", 1)[0]
                if head not in subpackages and head != "adapters":
                    # 只有 ``<子包>.<模块>`` 或子包本身是合法的新路径
                    if "." not in tail:
                        offenders.append(f"{rel}:{node.lineno} import {module}（旧扁平路径）")
    assert not offenders, "还有引用旧扁平路径的地方：\n  " + "\n  ".join(sorted(offenders))


def test_static_tables_are_extracted_and_reexported():
    """四分类落地：配置加载段已拆出，且**原样再导出**（拆段不许改调用面）。"""
    registry = importlib.import_module(f"{CAP}.catalog.tool_registry")
    tables = importlib.import_module(f"{CAP}.catalog.tool_tables")
    for name in ("SIDE_EFFECT_TIER", "CAPABILITY_TIER", "TOOL_TIER_OVERRIDES", "LEGACY_TIER_TOOLS"):
        assert getattr(registry, name) is getattr(tables, name), f"{name} 不是同一个对象（说明被复制了）"
    # 拆分后模块必须还认得这些常量（既有调用点写的是 tool_registry.SIDE_EFFECT_TIER）
    assert registry.SIDE_EFFECT_TIER["external"] == "critical"


def test_big_files_carry_four_category_annotations():
    """大文件必须留下四分类段标（方案硬要求），否则下一刀无从下手。"""
    for rel in (
        "app/agents/capabilities/catalog/tool_registry.py",
        "app/agents/capabilities/catalog/tool_tables.py",
        "app/agents/capabilities/catalog/legacy.py",
        "app/agents/capabilities/catalog/resource.py",
    ):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "[纯决策]" in text or "[配置加载]" in text, f"{rel} 缺少四分类段标"
        assert "── [" in text, rel


def test_shadow_projection_moved_out_and_verdict_unchanged():
    """诊断投影段已拆到 views/tool_shadow：对拍结论必须与拆分前逐字一致。"""
    shadow = importlib.import_module(f"{CAP}.views.tool_shadow")
    registry = importlib.import_module(f"{CAP}.catalog.tool_registry")
    # 旧路径不再提供影子名字（干净迁移，不留转发壳）
    assert not hasattr(registry, "shadow_compare")
    assert not hasattr(registry, "shadow_parity_totals")
    dims = shadow.SHADOW_PARITY_DIMENSIONS
    assert dims == ("tool→capability", "capability→mcp_target", "intent→tool_window", "tool→risk_tier")
    diffs = shadow.shadow_compare()
    totals = shadow.shadow_parity_totals(diffs)
    assert set(dims) <= set(diffs), "判定维度必须全部算出（缺维度会被判不安全）"
    assert totals["missing_dimensions"] == []
    assert totals["parity_total"] == 0, diffs
    assert totals["switch_safe"] is True


def test_provider_candidate_lookup_still_reports_declared_providers():
    """P2 只搬目录：Provider 候选解析的结果必须逐字不变（含已知缺口）。"""
    resource = importlib.import_module(f"{CAP}.catalog.resource")
    from app.agents.capabilities.registry.builtin import register_builtin_providers

    register_builtin_providers()
    assert resource.providers_for("resource.write", "workspace"), "workspace 写能力必须有 Provider 候选"
    assert resource.providers_for("resource.write", "memory"), "memory 写能力仍按声明给出候选"
