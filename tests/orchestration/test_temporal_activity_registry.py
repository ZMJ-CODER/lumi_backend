"""Temporal **活动名清单**守卫。

## 为什么需要它

Activity 由 Workflow 按**字符串名**调用（`workflow.execute_activity("execute_node_activity", …)`），
Worker 侧按函数注册。于是"把 Activity 拆进不同模块"这件事有两种失败模式：

* 少注册一个 → 工作流跑到那一步**静默挂住**（不是导入失败，也不是显式报错）；
* 名字被改掉 → 同样只在运行时暴露。

本文件把三份清单钉在一起，任何一处漂移都会在 CI 变红：

1. **实现**：带 ``@activity.defn`` 的函数（按模块扫描 AST，不 import 以避免拉起 Temporal）；
2. **注册**：``temporal/worker.py`` 里各 Worker 的 ``activities=[…]``；
3. **调用**：Workflow 里 ``workflow.execute_activity("名字", …)`` 的字面量。

顺带守一条工程性质：Activity 实现的模块清单是**冻结**的（新增/拆分类别时同步这里），
避免"拆完发现少了一个活动"这种事后再查。
"""

from __future__ import annotations

import ast

from _paths import REPO_ROOT

TEMPORAL = REPO_ROOT / "app" / "agents" / "orchestration" / "temporal"
WORKER = TEMPORAL / "worker.py"
WORKFLOWS = (
    REPO_ROOT / "app" / "agents" / "temporal_workflows.py",
    REPO_ROOT / "app" / "agents" / "temporal_logical_read_workflows.py",
    REPO_ROOT / "app" / "agents" / "temporal_node_workflows.py",
)

#: 冻结的"Activity 实现包"清单（按运行族拆分后的形状）。
_ACTIVITY_PACKAGES = ("activities", "logical_read_activities")

#: 冻结的活动名清单（= Worker 注册的功能面；改名/删除必须同步这里与前端/运维文档）。
_EXPECTED_ACTIVITIES = frozenset(
    {
        "execute_node_activity",
        "persist_node_result_ref_activity",
        "replan_static_job_activity",
        "synthesize_final_answer_activity",
        "cleanup_job_secrets_activity",
        "run_logical_read_frontier_activity",
        "run_logical_effects_frontier_activity",
        "expire_logical_effects_approval_activity",
        "cancel_logical_effects_job_activity",
        "replan_logical_read_activity",
        "fail_logical_read_job_activity",
    }
)


def _activity_definitions() -> dict[str, str]:
    """``{活动名: 相对模块路径}``（只扫 AST，不 import）。"""
    found: dict[str, str] = {}
    for package in _ACTIVITY_PACKAGES:
        for path in sorted((TEMPORAL / package).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorated = any(
                    "activity.defn" in ast.unparse(deco) for deco in node.decorator_list
                )
                if decorated:
                    found[node.name] = str(path.relative_to(REPO_ROOT))
    return found


def _registered_activities() -> set[str]:
    """``worker.py`` 里所有 ``activities=[…]`` 名单里出现的名字。"""
    tree = ast.parse(WORKER.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "activities" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
                continue
            for item in keyword.value.elts:
                if isinstance(item, ast.Name):
                    names.add(item.id)
    return names


def _workflow_activity_names() -> set[str]:
    """Workflow 里 ``execute_activity("名字", …)`` 的字面量。"""
    names: set[str] = set()
    for path in WORKFLOWS:
        if not path.exists():  # pragma: no cover - 可选的工作流模块
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = ast.unparse(node.func)
            if not func.endswith("execute_activity"):
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    return names


def test_activity_definitions_match_the_frozen_list():
    """实现清单必须与冻结清单逐字相等（拆分/改名必须同步更新）。"""
    defined = _activity_definitions()
    assert set(defined) == set(_EXPECTED_ACTIVITIES), {
        "多出来": sorted(set(defined) - _EXPECTED_ACTIVITIES),
        "少了": sorted(_EXPECTED_ACTIVITIES - set(defined)),
    }


def test_every_activity_is_registered_by_a_worker():
    """注册清单必须覆盖全部实现（漏注册 = 工作流静默挂住）。"""
    defined = set(_activity_definitions())
    registered = _registered_activities()
    assert registered, "worker.py 里没有解析到任何 activities=[…] 名单"
    missing = sorted(defined - registered)
    assert missing == [], f"这些 Activity 没有被任何 Worker 注册：{missing}"


def test_every_workflow_call_targets_a_known_activity():
    """Workflow 调用的名字必须都在实现清单里（拼写漂移在这里变红）。"""
    called = _workflow_activity_names()
    assert called, "没有解析到任何 execute_activity(\"…\") 调用，守卫会空转"
    unknown = sorted(called - set(_activity_definitions()))
    assert unknown == [], f"Workflow 调用了未实现/未注册的 Activity：{unknown}"


def test_activity_modules_are_grouped_by_running_family():
    """拆分后的形状：每个包内按运行族分文件，且不再有同名 .py 模块并存。"""
    expected_families = {
        "activities": {"static_dag", "persistence", "replan", "synthesis", "lifecycle"},
        "logical_read_activities": {
            "_shared", "helpers", "frontier_read", "frontier_effects",
            "replan", "approval", "lifecycle",
        },
    }
    for package, families in expected_families.items():
        pkg_dir = TEMPORAL / package
        assert pkg_dir.is_dir(), f"{package} 应是一个包"
        assert not (TEMPORAL / f"{package}.py").exists(), f"{package}.py 与包 {package}/ 不应并存"
        modules = {path.stem for path in pkg_dir.glob("*.py")}
        assert {"__init__"} | families == modules, (package, sorted(modules))
        init = (pkg_dir / "__init__.py").read_text(encoding="utf-8")
        assert "__all__" in init, f"{package}/__init__.py 必须显式声明导出面"
        # 包入口聚合再导出：活动名都能从包属性拿到（调用点因此一行不用改）
        for name in sorted(_EXPECTED_ACTIVITIES):
            if _activity_definitions().get(name, "").startswith(f"app/agents/orchestration/temporal/{package}/"):
                assert name in init, (package, name)
