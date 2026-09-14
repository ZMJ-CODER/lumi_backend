"""P1（兼容层整理）的落地回归：**纯搬家不许改行为**；P7 之后追加"壳已删除"。

这一阶段做了三件事，每一件都必须在测试里留下"搬完还在、且只有一条路"的证据：

1. 编排薄壳集中到 ``app/agents/orchestration/adapters/``（**P7 已整批删除**：
   零引用就不该留着），两层资源租约壳合并成一层（``app/agents/resource_coordination``）；
2. 插件控制面 ``app/services/plugins/`` → ``app/plugins/``；
3. 一次性数据迁移脚本 ``scripts/migrate_*.py`` 等 → ``scripts/migrations/``。

另加两条**启动/热重载回归**（方案 P1 明确要求）：

* ``app.main`` 能构建出 FastAPI 应用（模块被搬走后最容易坏的就是这里，而且原来的
  测试集里**没有任何**启动测试）；
* ``importlib.reload`` 包入口后公开 API 仍然完整（重载不产生新的模块对象）。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT

#: 搬走之后**必须**不存在的旧路径（import 得到、或被别人 import 到，都算回归）。
#: 后五项是 P1 搬进 ``adapters/`` 的薄壳——它们连同旧路径一起退休，
#: **测试也不许再依赖它们**（否则 P7 删壳时会连带炸掉测试）。
RETIRED_MODULES = (
    "app.services.plugins",
    "app.services.plugins.manager",
    "app.services.plugins.quota",
    "app.agents.orchestration.resources",
    "app.agents.orchestration.execution_mode",
    "app.agents.orchestration.execution_policy",
    "app.agents.orchestration.job_run_view",
    "app.agents.orchestration.step_resume",
    "app.agents.orchestration.step_sequence",
)

#: 迁移脚本新家（P1 从 scripts/ 移入 scripts/migrations/）。
MIGRATION_SCRIPTS = (
    "migrate_bge_m3",
    "migrate_memory_schema",
    "migrate_projects",
    "migrate_prompt_schema",
    "migrate_token_stats",
    "migrate_users",
    "migrate_user_prompts",
    "reembed_vectors",
    "rotate_memory_key",
)

#: 编排薄壳 → 真实实现（搬进 adapters/ 后仍然只是搬运）。
ADAPTER_PAIRS = {
    "app.agents.orchestration.adapters.execution_mode": "lumi_orch.execution_mode",
    "app.agents.orchestration.adapters.execution_policy": "lumi_orch.execution_policy",
    "app.agents.orchestration.adapters.job_run_view": "lumi_orch.run_view",
    "app.agents.orchestration.adapters.step_resume": "lumi_execution.step_resume",
    "app.agents.orchestration.adapters.step_sequence": "lumi_orch.step_sequence",
}


def _gate():
    """复用架构门禁的 AST import 解析（别在测试里再写一个正则版）。"""
    path = REPO_ROOT / "tools" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("_arch_gate_p1", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_arch_gate_p1"] = module
    spec.loader.exec_module(module)
    return module


gate = _gate()


# ── 1. 插件控制面搬到 app/plugins ────────────────────────────


def test_plugin_control_plane_is_importable_from_new_path():
    package = importlib.import_module("app.plugins")
    for name in ("PluginManager", "PluginRegistry", "quota_spec_for", "DependencyResolver"):
        assert hasattr(package, name), f"app.plugins 少了公开名 {name}"
    importlib.import_module("app.plugins.manager")
    importlib.import_module("app.plugins.quota")
    importlib.import_module("app.api.v1.plugins")
    # 控制面内部的一条真实私有路径（原来由 app.services.plugins.dependencies 提供）
    from app.plugins.dependencies import parse_requirement

    assert callable(parse_requirement)


@pytest.mark.parametrize("module", RETIRED_MODULES)
def test_retired_paths_are_gone(module):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def test_plugin_package_reload_keeps_public_api():
    """热重载回归：重载包入口后公开 API 还在，且**不产生新的模块对象**。"""
    package = importlib.import_module("app.plugins")
    manager_cls = package.PluginManager
    reloaded = importlib.reload(package)
    assert reloaded is package
    assert reloaded.PluginManager is manager_cls


def test_no_module_imports_retired_paths():
    """全仓库 AST 扫描：搬迁不留悬空引用（相对导入也能被还原）。

    本文件自己被排除：它是**对抗性测试**，必须写下"旧路径"这个名字才能断言它 import 不到
    （与 ``tests/test_unsafe_call_gate.py`` 在 ``ALLOWED_MODULES`` 里的理由相同）。
    """
    me = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    offenders: list[str] = []
    for path in gate.iter_python_files(["app", "packages", "scripts", "tests", "plugins", "celery_app", "tools"]):
        rel = path.resolve().relative_to(REPO_ROOT).as_posix()
        if rel == me:
            continue
        source = path.read_text(encoding="utf-8-sig")
        for ref in gate.imports_of(source, module=gate.module_for_rel(rel), is_package=path.name == "__init__.py"):
            if any(ref.module == retired or ref.module.startswith(retired + ".") for retired in RETIRED_MODULES):
                offenders.append(f"{rel}:{ref.lineno} → {ref.module}")
    assert not offenders, "还有模块引用已搬迁的旧路径：\n  " + "\n  ".join(sorted(offenders))


# ── 2. 编排薄壳（P7 已删除）+ 资源壳合并 ─────────────────────


@pytest.mark.parametrize("adapter_name,kernel_name", sorted(ADAPTER_PAIRS.items()))
def test_orchestration_adapters_are_gone_and_kernel_is_direct(adapter_name, kernel_name):
    """P7 之后壳已经删掉：真实内核仍然可用，而旧壳路径必须彻底消失。

    P1 把它们集中到 ``adapters/`` 是为了让"零引用"这件事看得见；
    P7 把它们删掉是因为**没人引用就不该留着**——留着只会让下一个读代码的人
    以为还有一层适配逻辑要维护。
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(adapter_name)
    kernel = importlib.import_module(kernel_name)
    assert getattr(kernel, "__all__", None) or dir(kernel), kernel_name


def test_resource_lease_adapter_is_single_layer():
    adapter = importlib.import_module("app.agents.resource_coordination")
    assert hasattr(adapter, "resource_coordinator")
    assert hasattr(adapter, "ResourceClaim")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.agents.orchestration.resources")


def test_orchestration_execution_uses_the_single_adapter():
    """真实调用点必须指向唯一那一层（否则删壳就等于把功能删了）。"""
    for rel in ("app/agents/orchestration/execution/node.py", "app/agents/orchestration/temporal/activities/static_dag.py"):
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "app.agents.resource_coordination import" in source, rel
        assert "orchestration.resources import" not in source, rel


# ── 3. 迁移脚本新家 ─────────────────────────────────────────


def test_migration_scripts_moved_out_of_scripts_root():
    assert not list((REPO_ROOT / "scripts").glob("migrate_*.py")), "scripts/ 根目录不该再有迁移脚本"
    for name in MIGRATION_SCRIPTS:
        assert (REPO_ROOT / "scripts" / "migrations" / f"{name}.py").exists(), name


def test_moved_scripts_still_resolve_the_repo_root():
    """深了一层之后 sys.path 基准必须同步（否则脚本一跑就 ImportError）。"""
    for name in MIGRATION_SCRIPTS:
        path = (REPO_ROOT / "scripts" / "migrations" / f"{name}.py").resolve()
        assert path.parents[2] == REPO_ROOT, f"{name} 的 parents[2] 不是仓库根"
    source = (REPO_ROOT / "scripts" / "migrations" / "reembed_vectors.py").read_text(encoding="utf-8")
    assert "parents[2]" in source, "reembed_vectors 直接 import app.*，必须显式把仓库根加进 sys.path"


# ── 4. 启动回归 ─────────────────────────────────────────────


def test_application_builds_and_exposes_plugin_routes():
    """``app.main`` 在导入时构建应用：这是"搬完还能起来"的最强证据。

    路由断言走 OpenAPI schema 而不是 ``app.routes``：FastAPI 0.141 把
    ``include_router`` 记成嵌套的 ``_IncludedRouter``（不是扁平列表），
    schema 才是"所有路径都真的挂上了"的权威视图。
    """
    from fastapi import FastAPI

    main = importlib.import_module("app.main")
    assert isinstance(main.app, FastAPI)
    paths = set((main.app.openapi() or {}).get("paths", {}))
    assert "/api/v1/plugins" in paths, "插件控制面路由没有挂上（很可能是搬包时漏了注册）"
    assert "/api/v1/agents/jobs/{job_id}/tool-window" in paths, "编排/工具窗口路由没有挂上"
    assert "/api/v1/admin/policies/tools" in paths
