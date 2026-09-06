"""统一资源访问边界。

模型提供的路径、工作目录和命令都是不可信输入。该模块只做边界判定，
不负责决定业务是否需要读取资源；授权项目仍由执行器通过 project_id 注入。
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class ResourcePolicyError(ValueError):
    """资源不在当前任务允许的边界内。"""


def _backend_root() -> Path:
    # app/core/resource_policy.py -> repo root
    return Path(__file__).resolve().parents[2]


def _normal(value: str) -> str:
    return str(value or "").strip().replace("\\", "/")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_client_path(value: str, *, field: str = "file_path") -> str:
    """拒绝路径穿越、符号链接越权和 Lumi 后端自身目录。"""
    raw = _normal(value)
    if not raw:
        raise ResourcePolicyError(f"缺少 {field}")
    if "\x00" in raw:
        raise ResourcePolicyError(f"{field} 包含非法字符")
    candidate = Path(raw).expanduser()
    # 客户端项目工具常传项目内相对路径；它们由客户端在已授权工作区内
    # 解析，不能相对后端进程 cwd 展开，否则所有 ``src/foo.py`` 都会被
    # 错误判定为服务端路径。
    if not candidate.is_absolute():
        parts = [part.casefold() for part in candidate.parts]
        if ".." in parts or ".git" in parts or "__pycache__" in parts:
            raise ResourcePolicyError("路径不能越过已授权工作区")
        if candidate.name.casefold().startswith(".env"):
            raise ResourcePolicyError("禁止访问凭据或环境配置文件")
        return raw
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise ResourcePolicyError(f"无法解析 {field}") from exc
    backend = _backend_root()
    if _inside(resolved, backend):
        raise ResourcePolicyError("禁止访问 Lumi 服务端源码、配置、测试和运行数据")
    # 即使目标尚不存在，也阻断明显的敏感文件名；这避免通过新建路径读取
    # 环境凭据或把服务端内部数据当作普通文件交给模型。
    parts = {part.casefold() for part in resolved.parts}
    name = resolved.name.casefold()
    if name.startswith(".env") or name in {"credentials", "secrets", "id_rsa"}:
        raise ResourcePolicyError("禁止访问凭据或环境配置文件")
    if parts & {".git", "__pycache__"}:
        raise ResourcePolicyError("禁止访问版本控制或运行时缓存目录")
    return str(resolved)


def validate_command(value: str, *, cwd: str = "") -> None:
    """阻断命令把客户端执行桥变成后端源码读取器。"""
    command = str(value or "").strip()
    if not command:
        raise ResourcePolicyError("缺少 command")
    backend = _backend_root()
    backend_text = _normal(str(backend)).casefold().rstrip("/")
    folded = _normal(command).casefold()
    if backend_text and backend_text in folded:
        raise ResourcePolicyError("禁止通过命令访问 Lumi 服务端目录")
    if re.search(r"(?i)(?:^|[\s'\"])(?:app|config|tests|logs|data|artifacts)(?:[/\\]|\s|$)", command):
        raise ResourcePolicyError("禁止通过命令访问 Lumi 服务端源码、配置、测试或运行数据")
    if re.search(r"(?i)(?:^|[\s'\"])(?:\.env(?:\b|[./\\])|credentials|id_rsa)(?:$|[\s'\"])", command):
        raise ResourcePolicyError("禁止通过命令读取凭据或环境配置")
    if cwd:
        validate_client_path(cwd, field="cwd")
