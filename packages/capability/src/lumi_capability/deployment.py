"""部署位置判定：描述符是否允许在**当前部署位置**执行（纯决策）。

从 ``app/agents/capabilities/registry/registry.py`` 抽出（结构重构 P3 第一批）。
这里刻意**不接收** ``ProviderRegistration``（那是应用侧的注册项形状，绑着能力目录），
只接收"描述符序列 + 部署位置"两样纯数据——判定逻辑本身与"谁注册的、目录里有什么"无关。
"""

from __future__ import annotations

from typing import Any, Iterable


def descriptor_allows_deployment(descriptors: Iterable[Any], deployment: Any) -> bool:
    """每个描述符都允许在当前部署位置执行才算通过。

    **一个不允许就整体不允许**：注册项是"这组能力的整体承诺"，
    部分可执行会让 Broker 在同一个 Provider 上得到互相矛盾的结论。
    """
    for descriptor in descriptors:
        if not descriptor.allows_deployment(deployment):
            return False
    return True


__all__ = ["descriptor_allows_deployment"]
