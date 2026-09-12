"""事件载荷 Schema 注册表（方案 §1.3）与未知事件策略。

规则：

* 注册维度是 **(message_type, schema_version)**，不是只按 ``type``：
  ``SCHEMA_REGISTRY[(type, version)] -> 载荷模型``；
* 同一个 ``message_type`` 内**只允许加字段**（additive）：新版本必须是旧版本的
  超集，校验由 :func:`schema_diff` / :func:`assert_additive_only` 提供，
  测试里对每条注册链强制执行（等价于 CI 校验）；
* 破坏性变更 = **新 message_type**，新旧并存过渡，不原地改字段含义；
* 未知事件类型/未知载荷结构：**小数据不解析（降级为空载荷）**，大对象只留
  ``data_ref``（内容哈希引用），绝不把看不懂的正文塞进公开事件。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.events.envelope import (
    ALLOWED_PAYLOAD_KEYS,
    PAYLOAD_MODELS,
    PAYLOAD_SCHEMA_VERSIONS,
    EventPayload,
)

#: 未知载荷"小数据"上限：序列化后不超过这个字节数就不解析（直接降级）。
UNKNOWN_PAYLOAD_MAX_BYTES = 4_096

#: 未知事件降级动作。
UNKNOWN_ACTION_EMPTY = "empty"
UNKNOWN_ACTION_REF = "ref"


def build_registry() -> dict[tuple[str, int], type[EventPayload]]:
    """从既有载荷模型表构建 ``(type, version) -> model`` 注册表。"""
    return {
        (event_type, int(PAYLOAD_SCHEMA_VERSIONS.get(event_type, 1))): model
        for event_type, model in PAYLOAD_MODELS.items()
    }


#: 唯一注册表（模块级只读使用；测试可 monkeypatch 后重建）。
SCHEMA_REGISTRY: dict[tuple[str, int], type[EventPayload]] = build_registry()


def registered_types() -> frozenset[str]:
    return frozenset(event_type for event_type, _ in SCHEMA_REGISTRY)


def schema_model(event_type: str, version: int | None = None) -> type[EventPayload] | None:
    """取载荷模型；(type, version) 未注册返回 ``None``（调用方走未知事件策略）。"""
    name = str(event_type or "").strip()
    if not name:
        return None
    if version is None:
        version = PAYLOAD_SCHEMA_VERSIONS.get(name, 1)
    return SCHEMA_REGISTRY.get((name, int(version)))


def current_schema_version(event_type: str) -> int:
    return int(PAYLOAD_SCHEMA_VERSIONS.get(str(event_type or "").strip(), 1))


def is_registered(event_type: str, version: int | None = None) -> bool:
    return schema_model(event_type, version) is not None


@dataclass(frozen=True, slots=True)
class SchemaDiff:
    """两个版本的字段差异（用于 additive 校验与排障）。"""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    retyped: tuple[str, ...] = ()

    @property
    def additive(self) -> bool:
        """只有新增字段（无删除、无改类型）才算兼容。"""
        return not self.removed and not self.retyped

    def describe(self) -> str:
        return (
            f"added={list(self.added)} removed={list(self.removed)} retyped={list(self.retyped)}"
        )


def _field_types(model: type[EventPayload]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, info in getattr(model, "model_fields", {}).items():
        annotation = getattr(info, "annotation", None)
        out[name] = str(annotation)
    return out


def schema_diff(old: type[EventPayload], new: type[EventPayload]) -> SchemaDiff:
    """``old`` → ``new`` 的字段差异（新增/删除/改类型）。"""
    before, after = _field_types(old), _field_types(new)
    added = tuple(sorted(set(after) - set(before)))
    removed = tuple(sorted(set(before) - set(after)))
    retyped = tuple(sorted(name for name in set(before) & set(after) if before[name] != after[name]))
    return SchemaDiff(added=added, removed=removed, retyped=retyped)


def assert_additive_only(event_type: str, versions: list[tuple[int, type[EventPayload]]]) -> None:
    """校验同一 ``message_type`` 的版本链只做了加法（否则抛 ``ValueError``）。"""
    ordered = sorted(versions, key=lambda item: item[0])
    for (prev_version, prev_model), (next_version, next_model) in zip(ordered, ordered[1:], strict=False):
        diff = schema_diff(prev_model, next_model)
        if not diff.additive:
            raise ValueError(
                f"{event_type} v{prev_version}→v{next_version} 不是加法兼容变更：{diff.describe()}"
            )


@dataclass(frozen=True, slots=True)
class UnknownEventDecision:
    """未知事件的处理决定（投影层唯一入口 :func:`decide_unknown_event`）。"""

    event_type: str
    action: str
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


def _payload_size(payload: Any) -> int:
    try:
        return len(json.dumps(payload or {}, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _opaque_ref(event_type: str, payload: Any, size: int) -> str:
    """大对象只留内容哈希引用：不解析正文，也不搬运正文。"""
    try:
        blob = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(size)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"opaque:{event_type}:{digest[:24]}:{size}"


def decide_unknown_event(
    event_type: str,
    payload: Any = None,
    *,
    known_types: frozenset[str] | None = None,
) -> UnknownEventDecision:
    """未知事件类型/未知载荷结构的统一策略。

    * 已注册类型 → ``pass``（正常投影）；
    * 未注册类型但载荷键都在安全白名单内（既有透传帧）→ ``pass``；
    * 未注册类型 + 未知结构：小数据 → ``empty``（降级为空载荷，前端记录"客户端
      版本不支持该事件"），大对象 → ``ref``（只留 ``data_ref`` 哈希引用）。
    """
    name = str(event_type or "").strip()
    values = payload if isinstance(payload, dict) else {}
    known = known_types if known_types is not None else registered_types()
    if name in known:
        return UnknownEventDecision(name, "pass")
    if values and set(map(str, values)) <= set(ALLOWED_PAYLOAD_KEYS):
        # 既有透传帧（capability_* / operation_* / plan_ready …）：结构在安全白名单内。
        return UnknownEventDecision(name, "pass")
    size = _payload_size(values)
    if size <= UNKNOWN_PAYLOAD_MAX_BYTES:
        return UnknownEventDecision(
            name,
            UNKNOWN_ACTION_EMPTY,
            {"unsupported": True, "schema_version": 0},
            reason="unknown_event_small_payload_dropped",
        )
    return UnknownEventDecision(
        name,
        UNKNOWN_ACTION_REF,
        {"unsupported": True, "schema_version": 0, "data_ref": _opaque_ref(name, values, size), "size_bytes": size},
        reason="unknown_event_large_payload_referenced",
    )


__all__ = [
    "SCHEMA_REGISTRY",
    "UNKNOWN_ACTION_EMPTY",
    "UNKNOWN_ACTION_REF",
    "UNKNOWN_PAYLOAD_MAX_BYTES",
    "SchemaDiff",
    "UnknownEventDecision",
    "assert_additive_only",
    "build_registry",
    "current_schema_version",
    "decide_unknown_event",
    "is_registered",
    "registered_types",
    "schema_diff",
    "schema_model",
]
