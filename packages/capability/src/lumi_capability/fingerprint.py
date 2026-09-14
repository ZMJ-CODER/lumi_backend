"""能力调用的**参数指纹**（纯函数，审批绑定与审计定位的唯一对象）。

从 ``app/agents/capabilities/policy/policy_guard.py`` 抽出（结构重构 P3 第二批）。

规则（与既有工具审批指纹同一约定）：指纹 = ``sha256(能力名 + 作用域 + 排序后的参数)``，
忽略执行期保留键（``_lumi_*``）。三条不可动摇的语义：

* **能力基名参与、版本号不参与**：审批针对"这个能力 + 这些参数"，
  Provider 升版不该让已批准的调用失效；
* **参数排序后编码**：同一语义调用必须得到同一指纹（否则审批永远对不上）；
* **绝不含未归一化的原始字符串**：编码用 ``sort_keys`` + 紧凑分隔符，
  避免"同样的字典因为插入顺序不同得到不同指纹"。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: 执行期保留键前缀：这些键是运行时透传的（追踪/重试标记），不参与指纹。
RESERVED_ARGUMENT_PREFIX = "_lumi_"


def capability_fingerprint(
    capability: str,
    arguments: dict[str, Any] | None = None,
    *,
    scope: dict[str, Any] | None = None,
) -> str:
    """能力调用的参数指纹（审批绑定的对象）。"""
    payload = {
        key: value
        for key, value in dict(arguments or {}).items()
        if not str(key).startswith(RESERVED_ARGUMENT_PREFIX)
    }
    encoded = json.dumps(
        {
            "capability": str(capability or "").split("@", 1)[0],
            "args": payload,
            "scope": dict(scope or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["RESERVED_ARGUMENT_PREFIX", "capability_fingerprint"]
