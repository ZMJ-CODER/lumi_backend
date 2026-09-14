"""导入**符号**门禁的自身回归：``from X import Y`` 里 Y 不存在时必须报出来。

## 背景（一次真实事故）

办公上下文里的懒加载导入没跟上搬家：

```
app/office/context.py:65:  from app.office.api import ensure_rag_index, ...
流式聊天失败: cannot import name 'ensure_rag_index' from 'app.office.api'
```

模块级导入完好，所以 ``tools/check_imports.py``（逐个 import 模块）与全量测试**全绿**；
只有真正执行到那一行才炸 —— 也就是"用户发一条带办公文档的消息"。这一关把检查前移到
AST 层：不看执行路径，直接验证每条 from-import 的目标符号是否存在。

本文件只测门禁**本身的判定逻辑**（临时目录 + 合成模块），不扫全仓（那样太慢，全仓扫描
由 CI job ``import-symbols`` 与本地 `python tools/check_import_symbols.py` 负责）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TOOL_PATH = pathlib.Path(__file__).resolve().parents[2] / "tools" / "check_import_symbols.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("check_import_symbols_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load_tool()


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把临时包挂到 sys.path 并切到该目录（门禁按相对路径扫描）。"""
    package = tmp_path / "gatefixture"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "provider.py").write_text("def present():\n    return 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delitem(sys.modules, "gatefixture", raising=False)
    try:
        yield package
    finally:
        for name in list(sys.modules):
            if name == "gatefixture" or name.startswith("gatefixture."):
                sys.modules.pop(name, None)


def _write(package: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = package / name
    path.write_text(body, encoding="utf-8")
    return path


def test_existing_symbol_passes(tool, sandbox, capsys):
    _write(sandbox, "good.py", "from gatefixture.provider import present\n")
    assert tool.main(["gatefixture"]) == 0
    assert "失败 0 条" in capsys.readouterr().out


def test_missing_symbol_is_reported_even_inside_a_function(tool, sandbox, capsys):
    """事故形状：模块级导入正常，坏掉的导入藏在函数体里。"""
    _write(
        sandbox,
        "lazy.py",
        "def run():\n    from gatefixture.provider import missing_symbol\n    return missing_symbol\n",
    )
    assert tool.main(["gatefixture"]) == 1
    out = capsys.readouterr().out
    assert "missing_symbol" in out and "lazy.py" in out


def test_missing_symbol_inside_try_except_fallback_is_reported(tool, sandbox):
    """``try/except ImportError`` 的兜底分支同样要检查（那里最容易被漏改）。"""
    _write(
        sandbox,
        "fallback.py",
        "try:\n"
        "    from gatefixture.provider import new_name\n"
        "except ImportError:\n"
        "    from gatefixture.provider import present\n",
    )
    assert tool.main(["gatefixture"]) == 1


def test_allow_missing_pragma_exempts_an_intentional_fallback(tool, sandbox, capsys):
    _write(
        sandbox,
        "allowed.py",
        "try:\n"
        "    from gatefixture.provider import present\n"
        "except ImportError:\n"
        "    # import-symbols: allow-missing\n"
        "    from gatefixture.provider import legacy_name\n",
    )
    assert tool.main(["gatefixture"]) == 0
    assert "失败 0 条" in capsys.readouterr().out


def test_submodule_import_counts_as_resolvable(tool, sandbox):
    _write(sandbox, "child.py", "VALUE = 1\n")
    _write(sandbox, "via_submodule.py", "from gatefixture import child\n")
    assert tool.main(["gatefixture"]) == 0


def test_star_import_is_skipped(tool, sandbox):
    _write(sandbox, "star.py", "from gatefixture.provider import *  # noqa: F403\n")
    assert tool.main(["gatefixture"]) == 0


def test_relative_import_is_resolved_against_package(tool, sandbox):
    _write(sandbox, "relative.py", "from .provider import present\n")
    assert tool.main(["gatefixture"]) == 0
    _write(sandbox, "relative_bad.py", "from .provider import nope\n")
    assert tool.main(["gatefixture"]) == 1
