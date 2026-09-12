"""《事件协议整合版》接入的灰度开关（方案阶段 6）。

规则：

* **默认全关**：关掉时必须回到改造前的旧路径（新逻辑不得成为隐式默认）；
* 开关只决定"新行为是否生效"，**不改契约**（公开事件、错误码、接口形状不随开关变化）；
* 未知开关名一律返回 ``False``（fail-safe：拼错名字不会意外打开新逻辑）；
* 影子模式（``INTEGRATION_SHADOW_MODE``）只记录"新逻辑会做什么"的差异，不改变实际返回。
"""

from __future__ import annotations

from typing import Any

#: 全部接入开关（与 ``app.core.config.Settings`` 字段一一对应）。
FEATURE_FLAGS: tuple[str, ...] = (
    "CAPABILITY_PREFLIGHT_V2",
    "MODEL_CAPABILITY_ROUTER_V2",
    "EFFECT_JOURNAL_RECOVERY_V2",
    "TASK_PROFILE_CANONICAL",
    "PLUGIN_QUOTA_ENFORCEMENT",
    "ARCHIVE_CONTENT_V2",
    # ── 结果存储 / 检查点与恢复（方案《结果存储、检查点与恢复》）──
    # 开启后结果统一进 ResultStore（分层 + schema_version + 过期 + 校验），
    # Job/快照只留摘要与引用；关闭时保持既有 {id, sha256} 引用路径。
    "RESULT_STORE_V2",
    # 开启后每一步都写步骤检查点（planned→started→running→…→uncertain），
    # 并保证"完成事件在检查点落盘之后"；关闭时不写检查点，行为与改造前一致。
    "STEP_CHECKPOINT_V2",
    # 开启后副作用日志同时记录 effect_type / effect_key，并支持 pending 在途核对。
    "EFFECT_JOURNAL_TYPED_V2",
)

#: 影子模式开关（不属于上面六个：它控制"是否只观察"）。
SHADOW_FLAG = "INTEGRATION_SHADOW_MODE"
ALL_FLAGS: tuple[str, ...] = (*FEATURE_FLAGS, SHADOW_FLAG)


def _settings() -> Any:
    from app.core.config import settings

    return settings


def feature_enabled(name: str, *, settings: Any = None) -> bool:
    """读取开关；未知名字返回 ``False``（拼错不会误开新逻辑）。"""
    flag = str(name or "").strip().upper()
    if flag not in ALL_FLAGS:
        return False
    source = settings if settings is not None else _settings()
    return bool(getattr(source, flag, False))


def shadow_mode(*, settings: Any = None) -> bool:
    """影子运行：新逻辑只算差异、不改返回。"""
    return feature_enabled(SHADOW_FLAG, settings=settings)


def flag_snapshot(*, settings: Any = None) -> dict[str, bool]:
    """当前开关快照（排障/日志用；值本身不含敏感信息）。"""
    return {flag: feature_enabled(flag, settings=settings) for flag in ALL_FLAGS}


__all__ = [
    "ALL_FLAGS",
    "FEATURE_FLAGS",
    "SHADOW_FLAG",
    "feature_enabled",
    "flag_snapshot",
    "shadow_mode",
]
