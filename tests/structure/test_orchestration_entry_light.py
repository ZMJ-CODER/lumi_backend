"""编排包入口必须保持"轻"（入口纪律的回归守卫）。

## 为什么要专门守这个

实测（改动前）：只导入一个叶子模块——`app.agents.orchestration.models` /
`effects` / `timeout_ladder` / `plan_compiler` / `admission`——都要 **~13 秒**，
并且顺带把 `orchestrator` / `redis` / `sqlalchemy` / `langgraph` / `app.repositories`
全部拉进进程。原因不在那些模块自己，而在父包 `__init__.py` 急切导入了 orchestrator 单例。
它同时构成循环的最后一环：

    app.repositories → orchestration.models → orchestration.__init__
        → orchestrator → app.repositories

改动后：`import app.agents.orchestration` **0.36s**、叶子模块 0.3–0.8s，且不再拖入重依赖。

## 本文件守四件事

1. **入口不得 eager import 重符号**（AST 静态断言，不依赖进程状态）；
2. **懒加载机制必须在**：`AgentOrchestrator` / `orchestrator` 仍可按需解析到真实来源；
3. **仓库里不得再用包级重符号写法**（`from app.agents.orchestration import orchestrator`）：
   它与同名子模块共享名字，语义取决于导入顺序——调用点一律指向真实模块；
4. **循环的两半都不成立**：入口不导入 orchestrator、`models` 不导入 `app.repositories`。
"""

from __future__ import annotations

import ast

import pytest

from _paths import REPO_ROOT

PACKAGE_DIR = REPO_ROOT / "app" / "agents" / "orchestration"
ENTRY = PACKAGE_DIR / "__init__.py"
MODELS = PACKAGE_DIR / "models.py"

#: 入口绝不允许急切导入的东西（导入它们=拉起整个编排系统）。
_HEAVY_MODULES = (
    "app.agents.orchestration.orchestrator",
    "redis",
    "sqlalchemy",
    "temporalio",
    "langgraph",
    "app.repositories",
)

#: 受管源码根（**不要**用 rglob 全仓扫描：那会遍历 .venv，耗时上百秒）。
_SCAN_ROOTS = ("app", "packages", "plugins", "scripts", "tools", "tests")
#: 扫描时的跳过目录。
_SKIP_DIRS = frozenset(
    {"node_modules", "__pycache__", ".ptmp", ".pytest_cache", ".ruff_cache", "dist", "build"}
)


def _iter_python_files():
    for root in _SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():  # pragma: no cover - 目录不存在时跳过
            continue
        for path in sorted(base.rglob("*.py")):
            if _SKIP_DIRS & set(path.parts):
                continue
            yield path

#: 重符号的**包级**写法（歧义写法）。命中即失败——用真实的子模块路径。
_HEAVY_NAMES = ("orchestrator", "AgentOrchestrator")


def _module_level_imports(path) -> list[str]:
    """文件里**模块级** import 的目标（函数内 import 不算 eager）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            prefix = "." * int(node.level or 0)
            targets.append(f"{prefix}{base}")
    return targets


def test_package_entry_has_no_eager_heavy_imports():
    """入口只许急切导入公共模型；重符号必须走懒加载。"""
    targets = _module_level_imports(ENTRY)
    for target in targets:
        assert not any(heavy in target for heavy in _HEAVY_MODULES), (
            f"{ENTRY.name} 不得急切导入 {target}"
        )
    # 唯一的编排内部依赖是公共模型（叶子、纯 pydantic 模型）
    assert targets == ["app.agents.orchestration.models"], targets


def test_package_entry_exposes_the_public_surface_and_lazy_loader():
    source = ENTRY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
    }
    assert "__all__" in names
    assert "_LAZY_ATTRS" in names, "懒加载名单必须在（否则 __getattr__ 无从判断）"
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "__getattr__" for node in tree.body
    ), "PEP 562 的模块级 __getattr__ 必须存在"
    assert {"Job", "JobStatus", "TaskNode", "TaskStatus"} <= set(
        ast.literal_eval(
            next(
                node.value
                for node in tree.body
                if isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "__all__"
            )
        )
    )


def test_lazy_symbols_resolve_to_their_real_source():
    """懒加载给出的对象必须与真实来源**同一个对象**（不是副本、不是模块）。"""
    import importlib

    package = importlib.import_module("app.agents.orchestration")
    real = importlib.import_module("app.agents.orchestration.orchestrator")
    assert package.__getattr__("AgentOrchestrator") is real.AgentOrchestrator
    assert package.__getattr__("orchestrator") is real.orchestrator
    # 未登记的属性仍然按标准语义报错
    with pytest.raises(AttributeError):
        package.__getattr__("not_a_public_name")


def test_repo_never_uses_the_ambiguous_package_form():
    """仓库里不得再用 ``from app.agents.orchestration import orchestrator``。

    这种写法与同名子模块共享一个名字：谁先被导入决定它是单例还是模块对象。
    调用点必须直接指向真实模块（``...orchestration.orchestrator import orchestrator``）。

    按 **AST 的 import 语句**扫描（不是文本匹配）：文档字符串里的示例代码不算违规。
    """
    offenders: list[str] = []
    for path in _iter_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - 仓库里的文件都应当能解析
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "app.agents.orchestration":
                continue
            if any(alias.name in _HEAVY_NAMES for alias in node.names):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == [], "这些地方请改成 from app.agents.orchestration.orchestrator import …"


def test_cycle_between_repositories_and_the_entry_is_broken():
    """循环的两半都不成立：入口不导入 orchestrator，公共模型不导入仓储层。"""
    entry_targets = _module_level_imports(ENTRY)
    assert not any("orchestrator" in target for target in entry_targets)
    model_targets = _module_level_imports(MODELS)
    assert not any("app.repositories" in target for target in model_targets), model_targets
    # 公共模型必须是"纯"的：只有标准库、编排内核与 pydantic
    allowed_prefixes = ("time", "enum", "lumi_orch", "pydantic", "__future__")
    for target in model_targets:
        assert target.startswith(allowed_prefixes), f"models.py 引入了不该有的依赖：{target}"
