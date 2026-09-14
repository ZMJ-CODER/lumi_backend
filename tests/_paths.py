"""测试共享路径基准（P7 测试目录镜像）。

测试文件分域下沉后，**不要**再写 ``Path(__file__).resolve().parent.parent``
这类"数几层到仓库根"的写法——层数会随目录调整而变，且出错时表现为
"文件找不到"这种与本次改动无关的报错。统一从这里取：

    fixture = REPO_ROOT / "tests" / "fixtures" / "x.json"

``tests/`` 在 ``pyproject.toml`` 的 ``pythonpath`` 里，所以裸名导入 ``_paths`` 在任何深度都成立。
"""

from __future__ import annotations

from pathlib import Path

#: 仓库根：以 ``pyproject.toml`` 为锚点向上找，避免依赖目录深度。
REPO_ROOT = next(
    parent for parent in Path(__file__).resolve().parents if (parent / "pyproject.toml").exists()
)
