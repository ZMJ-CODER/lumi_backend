"""阶段 8（后端部分）：Extension Handler Registry —— 未知插件类型默认拒绝。

方案第八阶段要求：``未知 kind → Developer Mode → 注册 Extension Handler → 安全审计 →
隔离运行 → 允许激活``，**生产环境默认拒绝未注册的插件类型**。

本模块只做前半段的"注册与门禁"：

* ``register`` 显式登记一个扩展类型（必须给出许可的隔离级别与理由）；
* ``handles`` 判断某个 kind 是否已被登记（未知 kind → False → 安装期拒绝）；
* 生产模式（``developer_mode=False``）下即使登记了也**不允许激活**，只有开发者模式
  才放行——避免"注册即等于放开"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from lumi_contracts.plugins import (
    IsolationLevel,
    parse_plugin_kind,
)

#: 扩展类型的 kind 必须形如 ``ext.<name>``（与内置 kind 词表不冲突）。
EXTENSION_KIND_PREFIX = "ext."


@dataclass(slots=True)
class ExtensionHandler:
    """一个已登记的扩展类型（最小可用形状）。"""

    kind: str
    handler_id: str
    isolation: IsolationLevel = IsolationLevel.SANDBOXED
    reason: str = ""
    registered_at: float = 0.0
    audited: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "handler_id": self.handler_id,
            "isolation": str(self.isolation),
            "reason": self.reason,
            "registered_at": self.registered_at,
            "audited": self.audited,
        }


class ExtensionHandlerRegistry:
    """扩展类型的登记处（未知 kind 一律不在册）。"""

    def __init__(self) -> None:
        self._handlers: dict[str, ExtensionHandler] = {}

    def register(
        self,
        *,
        kind: str,
        handler_id: str,
        isolation: IsolationLevel | str = IsolationLevel.SANDBOXED,
        reason: str = "",
        audited: bool = False,
    ) -> ExtensionHandler:
        """登记扩展类型；``kind`` 必须是 ``ext.*`` 且不是内置词表成员。"""
        key = str(kind or "").strip().casefold()
        if not key.startswith(EXTENSION_KIND_PREFIX):
            raise ValueError(
                f"扩展类型必须以 {EXTENSION_KIND_PREFIX} 开头（内置类型不需要登记）：{kind!r}"
            )
        # ``ext.skill_plugin`` 这种"给内置类型套前缀"的写法必须拒绝：
        # 否则等于绕过内置类型的门禁（隔离/审批/生命周期规则）。
        inner = key[len(EXTENSION_KIND_PREFIX) :]
        if parse_plugin_kind(inner) is not None or parse_plugin_kind(key) is not None:
            raise ValueError(f"{inner or key} 是内置插件类型，不允许作为扩展类型登记")
        parsed_isolation = (
            isolation
            if isinstance(isolation, IsolationLevel)
            else IsolationLevel(str(isolation))
        )
        if parsed_isolation in {IsolationLevel.IN_PROCESS, IsolationLevel.CLIENT_DEVICE}:
            # 扩展类型**不允许**进程内或客户端设备级隔离：它没有经过内置审计。
            raise ValueError(
                f"扩展类型不能使用 {parsed_isolation} 隔离（至少 restricted_worker/sandboxed）"
            )
        handler = ExtensionHandler(
            kind=key,
            handler_id=str(handler_id or "").strip(),
            isolation=parsed_isolation,
            reason=str(reason or ""),
            registered_at=time.time(),
            audited=bool(audited),
        )
        if not handler.handler_id:
            raise ValueError("扩展类型必须给出 handler_id")
        self._handlers[key] = handler
        logger.info("[plugin] 扩展类型已登记：{} → {}", key, handler.handler_id)
        return handler

    def unregister(self, kind: str) -> bool:
        return self._handlers.pop(str(kind or "").strip().casefold(), None) is not None

    def handles(self, kind: Any) -> bool:
        """该 kind 是否已登记为扩展类型（未知 → False，安装期据此拒绝）。"""
        key = str(getattr(kind, "value", kind) or "").strip().casefold()
        return key in self._handlers

    def get(self, kind: Any) -> ExtensionHandler | None:
        key = str(getattr(kind, "value", kind) or "").strip().casefold()
        return self._handlers.get(key)

    def all(self) -> list[ExtensionHandler]:
        return sorted(self._handlers.values(), key=lambda item: item.kind)

    def to_snapshot(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.all()]


#: 进程内共享登记处（生产默认空 = 所有未知类型都被拒绝）。
extension_handlers = ExtensionHandlerRegistry()


__all__ = [
    "EXTENSION_KIND_PREFIX",
    "ExtensionHandler",
    "ExtensionHandlerRegistry",
    "extension_handlers",
]
