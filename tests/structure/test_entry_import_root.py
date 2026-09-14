"""入口导入根守卫：**不要让 ``app/`` 遮蔽标准库**。

## 这次踩到的真问题

`python app/main.py`（IDE/脚本方式启动）时，Python 把脚本所在目录 ``app/`` 放到
``sys.path[0]``。于是第三方库里的 ``import platform`` 命中了 ``app/platform/`` ——
进 sqlalchemy 直接炸：

```
AttributeError: module 'platform' has no attribute 'python_implementation'
```

``platform`` 是 ``app/`` 下**唯一**与标准库重名的顶层名字（基础设施层从 ``app/core``
整体搬到 ``app/platform`` 之后才出现的）。``uvicorn app.main:app`` 不受影响，因为
模块方式下 ``sys.path[0]`` 是仓库根。

## 本文件守四件事

1. 入口 `app/main.py` 会在**任何第三方 import 之前**调用 `ensure_import_root()`；
2. 该函数真的把 ``app/`` 从 ``sys.path`` 摘掉、并把仓库根放到最前（幂等）；
3. ``app/`` 顶层与标准库重名的名字只有 `platform`（新增重名必须显式登记理由）；
4. 进程里的标准库 `platform` 没有被 `app/platform` 顶掉。
"""

from __future__ import annotations

import ast
import importlib
import platform
import sys
from pathlib import Path

import pytest

from _paths import REPO_ROOT

ENTRY = REPO_ROOT / "app" / "main.py"
APP_DIR = REPO_ROOT / "app"

#: 允许与标准库同名的 `app/` 顶层名字（**只减不增**；新增必须写明理由）。
_SHADOWING_ALLOWED = {
    "platform": "平台设施层（模型/安全/运行时/网络）；入口会把 app/ 从 sys.path 摘掉",
}


def _entry_ast() -> ast.Module:
    return ast.parse(ENTRY.read_text(encoding="utf-8"))


def test_entry_calls_the_import_root_guard_before_third_party_imports():
    """守卫必须在**任何第三方/app import** 之前跑（否则 sqlalchemy 已经先炸了）。

    守卫自身需要的标准库 import（``sys`` / ``pathlib`` …）可以排在它前面——
    它们不可能遮蔽 ``app/platform``。
    """
    tree = _entry_ast()
    allowed_preamble = {"sys", "os", "pathlib", "__future__"}
    guard_index = None
    first_heavy: tuple[int, str] | None = None
    for index, node in enumerate(tree.body):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "ensure_import_root"
        ):
            guard_index = index
            continue
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        root_name = (
            node.names[0].name.split(".")[0]
            if isinstance(node, ast.Import)
            else (node.module or "").split(".")[0]
        )
        if root_name in allowed_preamble:
            continue
        if first_heavy is None:
            first_heavy = (index, ast.unparse(node))
    assert guard_index is not None, "入口必须调用 ensure_import_root()"
    assert first_heavy is not None, "入口应当有第三方/app import（守卫比较的基线）"
    assert guard_index < first_heavy[0], (
        f"ensure_import_root() 必须排在 {first_heavy[1]} 之前（guard@{guard_index}）"
    )


def test_guard_removes_the_app_dir_and_keeps_the_repo_root(monkeypatch):
    """核心行为：摘掉 ``app/``、补上仓库根，且可重复调用。"""
    from app.main import ensure_import_root

    app_dir = str(APP_DIR)
    root = str(REPO_ROOT)
    monkeypatch.setattr(sys, "path", [app_dir, "/tmp/other", root])
    ensure_import_root()
    resolved = [str(Path(item).resolve()) for item in sys.path if item]
    assert str(APP_DIR.resolve()) not in resolved, sys.path
    assert sys.path[0] == root, sys.path
    # 幂等：再调一次不改变结果
    before = list(sys.path)
    ensure_import_root()
    assert sys.path == before


def test_guard_tolerates_a_missing_app_dir_entry(monkeypatch):
    """正常模块方式启动（sys.path 里没有 app/）时是空操作 + 补根。"""
    from app.main import ensure_import_root

    root = str(REPO_ROOT)
    monkeypatch.setattr(sys, "path", ["/tmp/other"])
    ensure_import_root()
    assert sys.path == [root, "/tmp/other"], sys.path


@pytest.mark.parametrize("name", sorted(_SHADOWING_ALLOWED))
def test_declared_stdlib_shadowing_still_exists(name):
    """登记的重名必须真的存在（否则名单过期，要及时删）。"""
    assert (APP_DIR / f"{name}.py").exists() or (APP_DIR / name).is_dir(), name
    assert _SHADOWING_ALLOWED[name], "必须写明为什么允许它与标准库同名"


def test_no_undeclared_stdlib_shadowing_under_app():
    """`app/` 顶层不得出现未登记的标准库同名名字（``types.py`` 这种最危险）。"""
    stdlib = set(sys.stdlib_module_names)
    found = set()
    for path in APP_DIR.iterdir():
        if path.name.startswith("__"):
            continue
        name = path.stem if path.is_file() and path.suffix == ".py" else path.name
        if name in stdlib:
            found.add(name)
    unexpected = sorted(found - set(_SHADOWING_ALLOWED))
    assert unexpected == [], f"app/ 下出现未登记的标准库同名名字：{unexpected}"


def test_stdlib_platform_is_not_shadowed_in_this_process():
    """进程里的 ``platform`` 必须是标准库，而不是 ``app/platform``。"""
    assert importlib.import_module("platform") is platform
    module_file = str(getattr(platform, "__file__", "") or "")
    assert "app" not in Path(module_file).parts, module_file
    assert hasattr(platform, "python_implementation")
    # app/platform 仍然是一个正常的**子包**（名字空间没有冲突）
    from app import platform as app_platform

    assert app_platform.__name__ == "app.platform"
