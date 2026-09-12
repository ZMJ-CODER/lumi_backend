"""持久化外部副作用日志的纯状态转换。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


EffectStatus = Literal["intent", "pending", "confirmed", "uncertain"]


class EffectType(StrEnum):
    """副作用类型（方案 §4.2）：决定"怎么核对"（查文件 / 查消息 ID）。

    ``UNKNOWN`` 是保守兜底：类型判不出来时不猜，恢复链路按不可判定处理（交人工）。
    """

    FILE_CREATE = "file_create"
    FILE_MODIFY = "file_modify"
    FILE_DELETE = "file_delete"
    FILE_MOVE = "file_move"
    SEND = "send"
    PUBLISH = "publish"
    EXEC = "exec"
    UNKNOWN = "unknown"

    @property
    def is_file_effect(self) -> bool:
        return self in {
            EffectType.FILE_CREATE,
            EffectType.FILE_MODIFY,
            EffectType.FILE_DELETE,
            EffectType.FILE_MOVE,
        }

    @property
    def verifiable(self) -> bool:
        """是否可核对实际状态（文件类可查目标路径；未知类型不可查）。"""
        return self is not EffectType.UNKNOWN


#: 类型判定词表（按**动作词**判定；只用于给出"怎么核对"，不改变任何执行决策）。
_EFFECT_TYPE_HINTS: tuple[tuple[EffectType, tuple[str, ...]], ...] = (
    (EffectType.FILE_DELETE, ("delete", "remove", "unlink", "trash", "删除", "移除")),
    (EffectType.FILE_MOVE, ("move", "rename", "移动", "重命名")),
    (EffectType.FILE_CREATE, ("create", "write", "new_file", "mkdir", "新建", "写入", "创建")),
    (EffectType.FILE_MODIFY, ("modify", "edit", "patch", "update", "append", "修改", "编辑")),
    (EffectType.SEND, ("send", "email", "message", "notify", "发送", "通知")),
    (EffectType.PUBLISH, ("publish", "deploy", "release", "push", "submit", "commit", "发布", "部署", "提交")),
    (EffectType.EXEC, ("exec", "run", "shell", "command", "script", "执行", "运行")),
)


def effect_type_for(*material: object) -> EffectType:
    """从工具名/动作/参数文本判定副作用类型（判不出返回 ``UNKNOWN``，不猜）。"""
    text = " ".join(str(item or "").strip().lower() for item in material if str(item or "").strip())
    if not text:
        return EffectType.UNKNOWN
    for effect_type, hints in _EFFECT_TYPE_HINTS:
        if any(hint in text for hint in hints):
            return effect_type
    return EffectType.UNKNOWN


def effect_key_for(intent: Mapping[str, Any] | None = None, *, fallback: str = "") -> str:
    """幂等键：优先显式声明，其次工具 + 参数摘要（与 intent 指纹同源）。

    写文件：目标路径 + 内容 hash；删除：目标路径 + 版本号；外发：消息 ID —— 都由调用方
    放进 ``intent``（键名 ``effect_key`` / ``target`` / ``message_id``），这里只做收敛。
    """
    payload = dict(intent or {})
    for name in ("effect_key", "idempotency_key", "target", "message_id", "path"):
        value = str(payload.get(name) or "").strip()
        if value:
            digest = payload.get("params_sha256") if name == "effect_key" else ""
            return (f"{value}:{digest}" if digest else value)[:160]
    digest = str(payload.get("params_sha256") or "").strip()
    tool = str(payload.get("tool") or "").strip()
    if digest:
        return f"{tool}:{digest}"[:160] if tool else digest[:160]
    return str(fallback or "")[:160]


class EffectRecord(BaseModel):
    """A body-free audit record for one externally visible operation."""

    status: EffectStatus
    intent: dict[str, Any] = Field(default_factory=dict)
    intent_at: float | None = None
    confirmed_at: float | None = None
    uncertain_at: float | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None
    updated_at: float
    # ── 方案 §4.2：类型与幂等键（旧记录缺失时按 UNKNOWN / 空处理）──
    effect_type: str = ""
    effect_key: str = ""
    step_id: str = ""
    attempt: int = 1
    result_ref: dict[str, Any] | None = None


def effect_intent_for_node(*, job_id: str, node: Any, tool: str = "") -> dict[str, str]:
    """Produce the stable, body-free intent fingerprint for a task node."""
    params = getattr(node, "params", {}) or {}
    encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "job_id": str(job_id or ""),
        "node_id": str(getattr(node, "id", "") or ""),
        "agent": str(getattr(node, "agent", "") or ""),
        "tool": str(tool or params.get("preferred_tool") or getattr(node, "agent", ""))[:160],
        "params_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def intent_record(intent: Mapping[str, Any] | None = None, *, now: float | None = None) -> EffectRecord:
    timestamp = time.time() if now is None else now
    payload = dict(intent or {})
    return EffectRecord(
        status="intent",
        intent=payload,
        intent_at=timestamp,
        updated_at=timestamp,
        # 类型/幂等键在**预留时**就落库：恢复时不必再解析当时的参数才能判断怎么核对。
        effect_type=str(payload.get("effect_type") or effect_type_for(
            payload.get("tool"), payload.get("action"), payload.get("operation"), payload.get("node_id"),
        ).value),
        effect_key=effect_key_for(payload),
        step_id=str(payload.get("node_id") or payload.get("step_id") or "")[:128],
        attempt=int(payload.get("attempt") or 1),
    )


def confirm_record(
    previous: Mapping[str, Any] | EffectRecord | None,
    result: dict[str, Any] | None = None,
    *,
    now: float | None = None,
    result_ref: Mapping[str, Any] | None = None,
) -> EffectRecord:
    timestamp = time.time() if now is None else now
    prior = _coerce_record(previous)
    return EffectRecord(
        status="confirmed",
        intent=dict(prior.intent),
        intent_at=prior.intent_at,
        confirmed_at=timestamp,
        result=result,
        updated_at=timestamp,
        effect_type=prior.effect_type,
        effect_key=prior.effect_key,
        step_id=prior.step_id,
        attempt=prior.attempt,
        # 确认时把结果引用一并记下：恢复/fork 只看 Journal 就能拿到引用，不必查 Job。
        result_ref=dict(result_ref) if result_ref else prior.result_ref,
    )


def uncertain_record(
    previous: Mapping[str, Any] | EffectRecord | None,
    reason: str = "execution_interrupted",
    *,
    now: float | None = None,
) -> EffectRecord:
    timestamp = time.time() if now is None else now
    prior = _coerce_record(previous)
    return EffectRecord(
        status="uncertain",
        intent=dict(prior.intent),
        intent_at=prior.intent_at,
        uncertain_at=timestamp,
        reason=str(reason or "execution_interrupted")[:160],
        updated_at=timestamp,
        effect_type=prior.effect_type,
        effect_key=prior.effect_key,
        step_id=prior.step_id,
        attempt=prior.attempt,
        result_ref=prior.result_ref,
    )


def _coerce_record(previous: Mapping[str, Any] | EffectRecord | None) -> EffectRecord:
    if isinstance(previous, EffectRecord):
        return previous
    if previous:
        return EffectRecord.model_validate(previous)
    return EffectRecord(status="intent", updated_at=0)
