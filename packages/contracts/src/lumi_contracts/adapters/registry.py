"""工具/契约注册表：按 ``name`` 与 ``namespace`` 管理 ``ToolSpec``。

治理要求（方案第七节）：

* 注册即自检（``ToolSpec.assert_valid``）——治理声明不完整直接拒绝；
* 命名空间 + 版本可查询，第三方插件按命名空间隔离；
* **注册成功不等于获得权限**：写入/删除/执行权限仍由执行期策略与授权快照决定，
  本注册表只负责"声明是否完备、是否可被发现"。
"""

from __future__ import annotations

from typing import Any, Callable

from lumi_contracts.common.errors import ContractError, ContractErrorCode
from lumi_contracts.execution.tool import ToolSpec

# 受信任的命名空间（第三方注册需显式加入白名单）。
DEFAULT_TRUSTED_NAMESPACES = ("lumi", "lumi_client", "lumi_skill")


class ToolRegistry:
    """``ToolSpec`` 注册表（进程内；持久化留给上层）。"""

    def __init__(self, *, trusted_namespaces: tuple[str, ...] = DEFAULT_TRUSTED_NAMESPACES) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._trusted = tuple(trusted_namespaces)

    @staticmethod
    def _key(name: str, namespace: str = "") -> str:
        return f"{namespace}.{name}" if namespace else str(name)

    def register(self, spec: ToolSpec, *, replace: bool = False) -> None:
        """注册一个工具；声明不完整或命名空间不受信则拒绝。"""
        if not isinstance(spec, ToolSpec):
            raise ContractError(ContractErrorCode.INVALID_CONTRACT, "register 需要 ToolSpec")
        problems = spec.validate_declaration()
        if problems:
            raise ContractError(
                ContractErrorCode.INVALID_CONTRACT,
                f"工具 {spec.qualified_name} 治理声明不完整：{'；'.join(problems)}",
                details={"problems": problems},
            )
        if spec.namespace and spec.namespace not in self._trusted:
            raise ContractError(
                ContractErrorCode.PERMISSION_DENIED,
                f"命名空间 {spec.namespace} 不在受信白名单内",
                details={"trusted": list(self._trusted)},
            )
        if spec.qualified_name in self._specs and not replace:
            raise ContractError(
                ContractErrorCode.INVALID_CONTRACT,
                f"工具 {spec.qualified_name} 已注册（如需覆盖请显式 replace=True）",
            )
        self._specs[spec.qualified_name] = spec

    def get(self, name: str, namespace: str = "") -> ToolSpec | None:
        return self._specs.get(self._key(name, namespace)) or self._specs.get(str(name))

    def require(self, name: str, namespace: str = "") -> ToolSpec:
        spec = self.get(name, namespace)
        if spec is None:
            raise ContractError(
                ContractErrorCode.NOT_IMPLEMENTED,
                f"未注册的工具：{self._key(name, namespace)}",
            )
        return spec

    def specs(self) -> list[ToolSpec]:
        return sorted(self._specs.values(), key=lambda item: item.qualified_name)

    def names(self) -> list[str]:
        return [item.qualified_name for item in self.specs()]

    def unregister(self, name: str, namespace: str = "") -> ToolSpec | None:
        return self._specs.pop(self._key(name, namespace), None)

    def clear(self) -> None:
        self._specs.clear()

    def export_contracts(self) -> dict[str, Any]:
        """导出契约清单（供文档 / 前端类型生成使用）。"""
        return {
            spec.qualified_name: {
                "version": spec.version,
                "input_schema": spec.input_schema,
                "output_schema": (
                    f"{spec.output_schema_name}@{spec.output_schema_version}"
                    if spec.output_schema_name else ""
                ),
                "side_effect": spec.side_effect.value,
                "risk_level": spec.risk_level.value,
                "data_sensitivity": spec.data_sensitivity.value,
                "requires_approval": spec.requires_approval,
                "streaming_support": spec.streaming_support,
            }
            for spec in self.specs()
        }


# 进程内默认注册表（业务侧可注入自己的实例）。
_DEFAULT: ToolRegistry | None = None


def default_tool_registry() -> ToolRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = ToolRegistry()
    return _DEFAULT


def register_tool(
    *,
    name: str,
    namespace: str = "lumi",
    version: str = "1.0.0",
    description: str = "",
    input_schema: dict[str, Any] | None = None,
    executor: Callable[..., Any] | None = None,
    **declaration: Any,
) -> ToolSpec:
    """便捷注册：构造 ``ToolSpec`` → 校验 → 入默认注册表。"""
    spec = ToolSpec(
        name=name,
        namespace=namespace,
        version=version,
        description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        executor=executor,
        **declaration,
    )
    default_tool_registry().register(spec)
    return spec


__all__ = [
    "DEFAULT_TRUSTED_NAMESPACES",
    "ToolRegistry",
    "default_tool_registry",
    "register_tool",
]
