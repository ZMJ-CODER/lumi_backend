#!/usr/bin/env python
"""导入冒烟门禁：把 ``app/``（以及 ``celery_app`` / ``plugins``）下的**每个模块**都 import 一遍。

## 为什么需要它

大范围搬家（本轮把编排层拆成 17 个子包、46 个模块换位置）之后，**测试只覆盖它们 import
到的模块**：一个只被 Celery 任务、Temporal Worker 或某个懒加载分支引用的模块，如果路径漏改，
测试全绿、线上第一次跑到才炸。这个脚本把"整棵导入图"变成一次可 CI 化的检查：

* 与测试不同，它不依赖任何用例的手工 import，因此能抓到"没人测到的模块"；
* 与 AST 门禁不同，它走真实的 import 机制（含包 ``__init__``、动态 import、相对导入）。

## 用法

```bash
python tools/check_imports.py            # 失败即非零退出（CI 用）
python tools/check_imports.py --list     # 只列出会导入的模块
```

**注意**：本脚本会 import 模块，因此**不要**在 pytest 进程里调用它（会污染 ``sys.modules``，
让依赖"尚未导入"的用例变得顺序敏感）。它是一次性的独立进程，这也是它放在 ``tools/``
而不是 ``tests/`` 的原因。
"""

from __future__ import annotations

import argparse
import importlib
import pathlib
import sys
import traceback

#: 扫描根：``app`` 是主线；另外两个是"只在 worker/插件加载时才 import"的入口区。
DEFAULT_ROOTS: tuple[str, ...] = ("app", "celery_app", "plugins")
SKIP_DIRS: frozenset[str] = frozenset({"__pycache__", ".venv", "node_modules", ".ptmp"})


def module_name(root: str, path: pathlib.Path) -> str:
    """文件路径 → 模块名（``__init__.py`` 归到包本身）。"""
    rel = path.relative_to(root)
    parts = list(rel.parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = rel.stem
    return ".".join([root, *parts]) if parts else root


def iter_modules(roots: list[str]) -> list[str]:
    names: list[str] = []
    for root in roots:
        base = pathlib.Path(root)
        if not base.is_dir():  # pragma: no cover - 仓库里都存在
            continue
        for path in sorted(base.rglob("*.py")):
            if SKIP_DIRS & set(path.parts):
                continue
            names.append(module_name(root, path))
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入冒烟门禁（逐个 import 应用模块）")
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS), help="扫描根目录")
    parser.add_argument("--list", action="store_true", help="只列出模块名，不实际导入")
    args = parser.parse_args(argv)

    modules = iter_modules(list(args.roots))
    if args.list:
        for name in modules:
            print(name)
        return 0

    failures: list[tuple[str, str]] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - 这里就是要收集一切导入失败
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            if "--verbose" in (argv or sys.argv):
                traceback.print_exc()

    print(f"导入冒烟：扫描 {len(modules)} 个模块，失败 {len(failures)} 个。")
    for name, error in failures:
        print(f"  ✗ {name} -> {error}")
    if failures:
        print("\n这些模块没有人 import 到（测试覆盖不到），但运行时会走到——先修路径再提交。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
