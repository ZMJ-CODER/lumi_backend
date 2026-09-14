"""迁移可执行性回归：两个让"代码到 0015、库停在 0011"真实卡住的坑。

本地库就是这样卡住的（评审 P0 第 8 条），而且**线上只要有历史数据就同样升不上去**：

1. ``alembic_version.version_num`` 是 Alembic 建的 ``VARCHAR(32)``，而本仓库的 revision
   标识可以是 33 字符（``0015_result_checkpoint_projection``）→ 所有 DDL 都跑完、
   写版本号时才报 ``StringDataRightTruncationError``，现场看起来像"某条 DDL 出错"；
2. 给**已有数据的表**加 ``NOT NULL`` 列而不给 ``server_default`` → ``NotNullViolationError``
   （``column "fallback_used" contains null values``）。

这两条都不该靠"记得小心"来避免，所以在这里做成静态断言 + 一次性修复的存在性检查。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from _paths import REPO_ROOT
REPO_ROOT = REPO_ROOT
VERSIONS_DIR = REPO_ROOT / "alembic" / "versions"

#: ``alembic_version.version_num`` 在 Alembic 里是 VARCHAR(32)；0015 把它放宽到 64。
#: 新 revision 标识不得超过 64（否则又会在写版本号时炸）。
MAX_REVISION_LENGTH = 64


def _version_files() -> list[Path]:
    return sorted(path for path in VERSIONS_DIR.glob("*.py") if path.name != "__init__.py")


def _module_assignments(tree: ast.Module) -> dict[str, ast.AST]:
    """模块级 ``name = value`` 与 ``name: T = value`` 两种写法都要认。

    （早期迁移用的是带注解的写法 ``revision: str = "..."``；只处理 ``ast.Assign``
    会让这些文件被"看不见"，从而误报"down_revision 指向不存在的迁移"。）
    """
    found: dict[str, ast.AST] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = value
    return found


def test_revision_ids_fit_the_version_column():
    """revision 标识必须放得进 ``alembic_version.version_num``。"""
    too_long: list[str] = []
    for path in _version_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        value = _module_assignments(tree).get("revision")
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        if len(value.value) > MAX_REVISION_LENGTH:
            too_long.append(f"{path.name}: revision={value.value!r}（{len(value.value)} 字符）")
    assert too_long == [], (
        "revision 标识过长，写版本号时会失败：\n" + "\n".join(too_long)
    )


def test_head_migration_widens_the_version_column():
    """必须有迁移把 ``alembic_version.version_num`` 放宽（否则 33 字符的 revision 必炸）。"""
    widened = [
        path.name
        for path in _version_files()
        if "ALTER TABLE alembic_version" in path.read_text(encoding="utf-8")
        and "VARCHAR(64)" in path.read_text(encoding="utf-8")
    ]
    assert widened, "没有任何迁移放宽 alembic_version.version_num（长 revision 会写不进去）"


def _add_column_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "add_column":
                calls.append(node)
    return calls


def test_not_null_columns_added_to_existing_tables_carry_server_default():
    """给已有表加 ``NOT NULL`` 列必须带 ``server_default``，否则有历史数据的库升不上去。

    数据驱动的写法（``_COLUMNS`` 里带默认值再循环 ``add_column``）无法静态看出默认值，
    因此这里按"文件里是否出现过 ``server_default``"做粗粒度但有效的守卫：
    一个文件只要有 ``add_column`` 且出现 ``nullable=False`` 的显式关键字，就必须同时
    出现 ``server_default``。误报时人工确认一次即可，比"上线才发现升不上去"划算。
    """
    offenders: list[str] = []
    for path in _version_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = _add_column_calls(tree)
        if not calls:
            continue
        has_not_null_keyword = any(
            any(kw.arg == "nullable" and getattr(kw.value, "value", None) is False for kw in call.keywords)
            for call in calls
        )
        if not has_not_null_keyword:
            continue
        if "server_default" not in source:
            offenders.append(path.name)
    assert offenders == [], (
        "这些迁移给已有表加了 NOT NULL 列却没有 server_default，有数据的库会升级失败：\n"
        + "\n".join(offenders)
    )


def test_migration_graph_is_linear_and_reachable():
    """迁移链必须是单链（没有意外分叉/孤儿），否则 ``upgrade head`` 会有歧义。"""
    revisions: dict[str, str | None] = {}
    for path in _version_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = _module_assignments(tree)
        rev = getattr(values.get("revision"), "value", None)
        down = getattr(values.get("down_revision"), "value", None)
        if isinstance(rev, str):
            revisions[rev] = down if isinstance(down, str) else None
    assert revisions, "没有解析到任何迁移"
    heads = set(revisions) - {down for down in revisions.values() if down}
    assert len(heads) == 1, f"迁移链存在多个 head：{sorted(heads)}（upgrade head 会有歧义）"
    # 每个 down_revision 都必须指向一个存在的 revision（除基线外）。
    missing = [down for down in revisions.values() if down and down not in revisions]
    assert missing == [], f"down_revision 指向不存在的迁移：{missing}"


@pytest.mark.parametrize("filename", ["0014_llm_usage_model_roles.py", "0015_result_checkpoint_projection.py"])
def test_known_hard_migrations_still_present(filename: str):
    """这两条是"卡住升级"的现场，删掉它们等于把坑重新埋回去。"""
    assert (VERSIONS_DIR / filename).is_file()
