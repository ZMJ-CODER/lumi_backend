"""沙箱注册中心 —— 按类型获取沙箱实例.

类型通过配置 AGENT_SANDBOX_TYPE 选择，后期扩展：
  - local: 本地子进程 + 资源限制（当前为占位，未启用）
  - docker: 容器隔离
  - wasm: WASM 运行时
"""

from loguru import logger

from app.agents.sandbox.base import Sandbox
from app.core.config import settings

SANDBOXES: dict[str, type[Sandbox]] = {}
_instances: dict[str, Sandbox] = {}


def register_sandbox(name: str, cls: type[Sandbox]) -> None:
    """注册一种沙箱实现."""
    SANDBOXES[name] = cls
    logger.info(f"沙箱已注册: {name} ({cls.__name__})")


def _ensure_builtins() -> None:
    """懒加载内置沙箱（避免循环导入）."""
    if not SANDBOXES:
        from app.agents.sandbox.local import LocalSandbox

        SANDBOXES["local"] = LocalSandbox


def get_sandbox(name: str | None = None) -> Sandbox:
    """获取沙箱实例（懒加载单例）."""
    _ensure_builtins()
    name = name or settings.AGENT_SANDBOX_TYPE
    cls = SANDBOXES.get(name)
    if not cls:
        raise ValueError(f"未知沙箱类型: {name}，可选: {list(SANDBOXES)}")
    if name not in _instances:
        _instances[name] = cls()
    return _instances[name]


def available_sandboxes() -> list[str]:
    """列出已注册的沙箱类型."""
    _ensure_builtins()
    return list(SANDBOXES)
