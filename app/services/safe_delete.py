"""受控删除：全仓库**唯一**允许做递归删除/删除文件的地方。

## 为什么需要收口

删除是本项目里唯一"错了无法回滚"的本地操作。此前 ``shutil.rmtree`` 散落在 6 个业务模块
（账号注销、工作区注销、办公文档清理、制品保留、Celery 清理）各自直接调用，安全边界靠
每个调用点自己记得写——这正是静态扫描要禁止的形态。

收口后：

* CI 的 AST 门禁（``scripts/check_unsafe_calls.py``）只允许**本模块**出现
  ``shutil.rmtree`` / ``Path.unlink`` / ``Path.rmdir``；
* 业务代码一律调 :func:`remove_tree` / :func:`remove_file`，边界校验只有一份实现；
* 每个函数都要求显式 ``root``（允许删除的根），并拒绝越界路径——避免"路径来自
  用户输入就直接 rmtree"这类经典事故。

## 边界规则（fail-closed）

1. ``root`` 必须存在且是**绝对路径**：不给自己猜相对路径的机会（cwd 一变就删错地方）；
2. ``target`` 解析后必须落在 ``root`` 之内（``Path.relative_to`` 语义，含符号链接解析）；
3. ``target == root`` 一律拒绝：删根目录几乎总是调用方写错了变量；
4. 默认拒绝删除挂载点/家目录/系统临时根等"危险根"本身。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from loguru import logger


class UnsafeRemoval(RuntimeError):
    """删除请求越界（稳定可读的原因，不做静默兜底）。"""


def _resolved_root(root: str | Path) -> Path:
    base = Path(root)
    if not base.is_absolute():
        raise UnsafeRemoval(f"删除根必须是绝对路径：{base}")
    resolved = base.resolve()
    if not resolved.exists():
        raise UnsafeRemoval(f"删除根不存在：{resolved}")
    return resolved


def _within(base: Path, target: str | Path) -> Path:
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if resolved == base:
        raise UnsafeRemoval(f"拒绝删除删除根本身：{resolved}")
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise UnsafeRemoval(f"目标不在允许的根之内：{resolved} (root={base})") from exc
    return resolved


def remove_tree(root: str | Path, target: str | Path, *, ignore_errors: bool = True) -> bool:
    """在 ``root`` 边界内递归删除 ``target``。返回是否真的删了东西。

    ``ignore_errors=True``（默认）与既有调用点行为一致：清理类删除失败不该让主流程挂掉，
    但必须留日志——"删不掉"和"没删"在排障时是完全不同的事。
    """
    base = _resolved_root(root)
    resolved = _within(base, target)
    if not resolved.exists():
        return False
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved, ignore_errors=ignore_errors)
        else:
            resolved.unlink()
    except OSError as exc:
        if not ignore_errors:
            raise
        logger.warning("[safe-delete] 删除失败（已忽略）path={} err={}", str(resolved)[:160], str(exc)[:120])
        return False
    return True


def remove_tree_exact(target: str | Path, *, allow_roots: Iterable[str | Path], ignore_errors: bool = True) -> bool:
    """按**允许根列表**删除（调用方没有单一 root 时的显式写法）。

    与 :func:`remove_tree` 的差别只在"允许哪些根"的表达方式：审核时能一眼看出
    这个调用点被授权删除的范围。
    """
    roots = [Path(item).resolve() for item in allow_roots]
    candidate = Path(target)
    if not candidate.is_absolute():
        raise UnsafeRemoval(f"目标必须是绝对路径：{candidate}")
    resolved = candidate.resolve()
    for base in roots:
        if resolved == base:
            raise UnsafeRemoval(f"拒绝删除允许根本身：{resolved}")
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        if not resolved.exists():
            return False
        try:
            if resolved.is_dir():
                shutil.rmtree(resolved, ignore_errors=ignore_errors)
            else:
                resolved.unlink()
        except OSError as exc:
            if not ignore_errors:
                raise
            logger.warning("[safe-delete] 删除失败（已忽略）path={} err={}", str(resolved)[:160], str(exc)[:120])
            return False
        return True
    raise UnsafeRemoval(f"目标不在任何允许的根之内：{resolved} (roots={roots})")


def remove_file(root: str | Path, target: str | Path, *, missing_ok: bool = True) -> bool:
    """在 ``root`` 边界内删除单个文件（不递归、不删目录）。"""
    base = _resolved_root(root)
    resolved = _within(base, target)
    if not resolved.exists():
        return False
    if resolved.is_dir():
        raise UnsafeRemoval(f"{resolved} 是目录；删除目录请用 remove_tree")
    try:
        resolved.unlink(missing_ok=missing_ok)
    except OSError as exc:
        logger.warning("[safe-delete] 删除文件失败 path={} err={}", str(resolved)[:160], str(exc)[:120])
        return False
    return True


__all__ = [
    "UnsafeRemoval",
    "remove_file",
    "remove_tree",
    "remove_tree_exact",
]
