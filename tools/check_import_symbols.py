#!/usr/bin/env python
"""导入**符号**门禁：``from X import Y`` 里的 Y 必须真在 X 里存在（含函数体内的懒加载导入）。

## 为什么还需要它（``tools/check_imports.py`` 抓不到的那一半）

``check_imports.py`` 逐个 ``import`` 模块，能抓到"模块路径搬家了"。但它抓不到
**函数体内的导入**——那些行只有真正执行到才会报错：

```
app/office/context.py:65:
    from app.office.api import ensure_rag_index, ensure_session, extract_full_text
```

``ensure_rag_index`` 早就不在 ``app/office/api.py`` 了（公开面只 re-export 三个写入接口），
于是模块 import 完全正常、全量测试全绿，**直到用户发一条带办公文档的消息**才在线上炸：

```
流式聊天失败: cannot import name 'ensure_rag_index' from 'app.office.api'
```

本脚本不看执行路径，直接读 AST：对全仓每个 ``from X import a, b`` 都验证 ``a``/``b``
能在 ``X`` 里解析（模块属性或子模块）。这样"懒加载路径错"从"线上第一次跑到才炸"
变成"提交前就红"。

## 用法

```bash
python tools/check_import_symbols.py            # 失败即非零退出（CI 用）
python tools/check_import_symbols.py --verbose  # 打印每个被检查的导入
```

**注意**：它会 import 每个被引用的模块（以解析符号），因此和 ``check_imports.py`` 一样
不要在 pytest 进程里调用。
"""

from __future__ import annotations

import argparse
import ast
import importlib
import pathlib

#: 扫描根：与被 import 的模块同名的基础设施包也要一起看（``app`` 是主线）。
DEFAULT_ROOTS: tuple[str, ...] = ("app", "celery_app", "plugins")
SKIP_DIRS: frozenset[str] = frozenset({"__pycache__", ".venv", "node_modules", ".ptmp", "tests"})

#: 这些名字允许"看起来不存在"（import 机制/类型检查专用，不是模块属性）。
_ALLOWED_MISSING: frozenset[str] = frozenset({"annotations", "TYPE_CHECKING", "mypy_extensions"})

#: 白名单标记：确实要写"旧版本兜底导入"时，在那一行（或其上方一行）加这个注释。
#: 例如 ``try: ... except ImportError: from pkg import language  # import-symbols: allow-missing``
_ALLOW_MARKER = "import-symbols: allow-missing"


def iter_files(roots: list[str]) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in roots:
        base = pathlib.Path(root)
        if not base.exists():
            continue
        if base.is_file() and base.suffix == ".py":
            files.append(base)
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIRS & set(path.parts):
                continue
            files.append(path)
    return files


def collect_imports(path: pathlib.Path) -> list[tuple[int, str, list[str]]]:
    """返回 ``[(行号, 模块名, [符号...]), ...]``；相对导入已按包名折算成绝对模块名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = ".".join(path.parts[:-1])
    if path.name == "__init__.py":
        package = ".".join(path.parts[:-1])
    found: list[tuple[int, str, list[str]]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if any(alias.name == "*" for alias in node.names):
            continue  # 星号导入交给运行时/其它门禁
        if node.level:
            parts = package.split(".") if package else []
            base = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
            module = ".".join([*base, node.module]) if node.module else ".".join(base)
        else:
            module = node.module or ""
        if not module:
            continue
        names = [alias.name for alias in node.names]
        found.append((node.lineno, module, names))
    return found


def resolve(module: str, name: str) -> bool:
    """``name`` 是否能在 ``module`` 里解析（模块属性或子模块）。"""
    try:
        imported = importlib.import_module(module)
    except Exception:  # noqa: BLE001 - 模块本身 import 失败由 check_imports.py 负责报
        return True
    if hasattr(imported, name):
        return True
    try:
        importlib.import_module(f"{module}.{name}")
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入符号门禁（验证 from X import Y 的 Y 存在）")
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS), help="扫描根")
    parser.add_argument("--verbose", action="store_true", help="打印每个被检查的导入")
    args = parser.parse_args(argv)

    files = iter_files(list(args.roots))
    checked = 0
    failures: list[str] = []
    imported_ok: set[str] = set()

    for path in files:
        try:
            source_lines = path.read_text(encoding="utf-8").splitlines()
            imports = collect_imports(path)
        except SyntaxError as exc:  # pragma: no cover - 语法错误另有门禁
            failures.append(f"{path}:{exc.lineno} 语法错误: {exc.msg}")
            continue
        for lineno, module, names in imports:
            # 白名单标记：允许"旧版本兜底"这类刻意写不存在的导入。
            window = "\n".join(source_lines[max(0, lineno - 2): lineno + 1])
            if _ALLOW_MARKER in window:
                continue
            # 只检查仓库内模块与顶层依赖：标准库/第三方交给真实 import 报错。
            for name in names:
                if name in _ALLOWED_MISSING:
                    continue
                checked += 1
                if args.verbose:
                    print(f"  {path}:{lineno} from {module} import {name}")
                if resolve(module, name):
                    imported_ok.add(f"{module}.{name}")
                    continue
                failures.append(f"{path}:{lineno}  from {module} import {name}")

    print(f"导入符号门禁：扫描 {len(files)} 个文件，检查 {checked} 条 from-import，失败 {len(failures)} 条。")
    for item in failures:
        print(f"  ✗ {item}")
    if failures:
        print(
            "\n这些符号在目标模块里不存在（多半是搬家后只改了模块级导入、漏了函数体内的懒加载）。\n"
            "修法：把导入指向符号现在的归属模块，或在同包公开面里显式 re-export。"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
