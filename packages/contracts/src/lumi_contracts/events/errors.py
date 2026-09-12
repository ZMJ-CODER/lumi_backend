"""统一错误模型（``UnifiedError``）：公开事件里 ``error`` / ``control`` 载荷的唯一来源。

设计约束（与《事件协议》方案一致）：

* **错误也是必须过海关的数据**：生产、投影、SSE 三个出口只消费 ``UnifiedError``，
  不允许各处自造 ``{"error": "..."}`` 之类的自由结构；
* **错误码是跨模块契约**：冻结码表 :data:`FROZEN_ERROR_CODES` 之外的码必须登记，
  不能随手拼字符串；
* **前端只展示 ``safe_message``**：原始异常文本、供应商响应、堆栈**永不**进入
  ``safe_message``，只允许进日志；完整细节走 ``detail_ref``（产物引用，受权限保护）；
* **同一类失败从任何路径返回同一个 code + safe_message**：映射表只有这一份，
  旧错误码通过 :data:`LEGACY_CODE_ALIASES` 收敛，不做二次发明。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

#: 公开错误码版本（破坏性改码名才升版本）。
UNIFIED_ERROR_VERSION = 1


class ErrorCategory(StrEnum):
    """错误大类：决定前端默认动作（重试 / 提示 / 转人工）。"""

    TRANSIENT = "transient"
    FATAL = "fatal"
    BUSINESS = "business"
    NEEDS_HUMAN = "needs_human"


class ErrorDomain(StrEnum):
    """错误域前缀（``model.`` / ``tool.`` / …），未登记错误码按域取默认口径。"""

    MODEL = "model"
    TOOL = "tool"
    CAPABILITY = "capability"
    PERMISSION = "permission"
    VALIDATION = "validation"
    PLUGIN = "plugin"
    RESOURCE = "resource"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """一个错误码的稳定口径（类别 / 可重试 / 用户可见文案 / 下一步动作）。"""

    code: str
    category: ErrorCategory
    retryable: bool
    safe_message: str
    next_action: str = ""


#: 冻结错误码清单（方案 §3.2）：跨端契约，新增必须登记并同步前端文案表。
FROZEN_ERROR_SPECS: dict[str, ErrorSpec] = {
    "TARGET_REQUIRED": ErrorSpec(
        "TARGET_REQUIRED", ErrorCategory.BUSINESS, False,
        "请先说明要操作的目标（文件、目录或对象）。", "补充目标后重试",
    ),
    "DEPENDENCY_MISSING_WORKSPACE": ErrorSpec(
        "DEPENDENCY_MISSING_WORKSPACE", ErrorCategory.BUSINESS, False,
        "当前任务缺少工作区依赖，请先选择或连接工作区。", "选择工作区",
    ),
    "CAPABILITY_UNAVAILABLE": ErrorSpec(
        "CAPABILITY_UNAVAILABLE", ErrorCategory.BUSINESS, False,
        "当前能力暂不可用（提供方未连接或已停用）。", "检查能力提供方",
    ),
    "PROVIDER_UNHEALTHY": ErrorSpec(
        "PROVIDER_UNHEALTHY", ErrorCategory.TRANSIENT, True,
        "模型服务暂时不可用，正在重试。", "稍后重试",
    ),
    "PERMISSION_DENIED": ErrorSpec(
        "PERMISSION_DENIED", ErrorCategory.BUSINESS, False,
        "你没有执行该操作的权限。", "联系管理员",
    ),
    "TOOL_NOT_REGISTERED": ErrorSpec(
        "TOOL_NOT_REGISTERED", ErrorCategory.BUSINESS, False,
        "该工具未注册或已下线，任务未执行。", "更换工具",
    ),
    "APPROVAL_REQUIRED": ErrorSpec(
        "APPROVAL_REQUIRED", ErrorCategory.NEEDS_HUMAN, False,
        "该操作需要你的确认后才能继续。", "等待确认",
    ),
    "SECURITY_BLOCKED": ErrorSpec(
        "SECURITY_BLOCKED", ErrorCategory.BUSINESS, False,
        "该操作被安全策略拦截，任务未执行。", "调整请求",
    ),
    "PLUGIN_RESOURCE_EXCEEDED": ErrorSpec(
        "PLUGIN_RESOURCE_EXCEEDED", ErrorCategory.TRANSIENT, True,
        "插件资源超限，已中止该步骤。", "重试",
    ),
    "PLUGIN_UNINSTALLED": ErrorSpec(
        "PLUGIN_UNINSTALLED", ErrorCategory.BUSINESS, False,
        "插件已卸载，无法继续该步骤。", "重新安装插件",
    ),
    "RESULT_REF_EXPIRED": ErrorSpec(
        "RESULT_REF_EXPIRED", ErrorCategory.BUSINESS, False,
        "产物引用已过期，请重新生成或重新打开。", "重新生成",
    ),
    "SYSTEM_CANCELLED": ErrorSpec(
        "SYSTEM_CANCELLED", ErrorCategory.BUSINESS, False,
        "任务已取消。", "重新发起",
    ),
}

#: 域内默认错误码（方案 §3.2 的域表）：未登记的码走域默认口径。
DOMAIN_ERROR_SPECS: dict[str, ErrorSpec] = {
    "model.timeout": ErrorSpec("model.timeout", ErrorCategory.TRANSIENT, True, "模型响应超时，正在重试。", "稍后重试"),
    "model.rate_limited": ErrorSpec("model.rate_limited", ErrorCategory.TRANSIENT, True, "模型调用被限流，正在重试。", "稍后重试"),
    "model.provider_offline": ErrorSpec("model.provider_offline", ErrorCategory.TRANSIENT, True, "模型服务暂时不可用，正在重试。", "稍后重试"),
    "tool.failed": ErrorSpec("tool.failed", ErrorCategory.FATAL, False, "工具执行失败，该步骤未完成。", "查看过程日志"),
    "tool.timeout": ErrorSpec("tool.timeout", ErrorCategory.TRANSIENT, True, "工具执行超时，正在重试。", "稍后重试"),
    "capability.disabled": ErrorSpec("capability.disabled", ErrorCategory.BUSINESS, False, "该能力已被管理员停用。", "联系管理员"),
    "capability.uninstalled": ErrorSpec("capability.uninstalled", ErrorCategory.BUSINESS, False, "该能力已卸载。", "重新安装"),
    "permission.denied": ErrorSpec("permission.denied", ErrorCategory.BUSINESS, False, "你没有执行该操作的权限。", "联系管理员"),
    "validation.schema_mismatch": ErrorSpec("validation.schema_mismatch", ErrorCategory.BUSINESS, False, "请求结构与预期不一致，需要澄清后重试。", "补充说明"),
    "validation.target_required": ErrorSpec("validation.target_required", ErrorCategory.BUSINESS, False, "请先说明要操作的目标（文件、目录或对象）。", "补充目标后重试"),
    "plugin.crashed": ErrorSpec("plugin.crashed", ErrorCategory.TRANSIENT, True, "插件异常退出，已中止该步骤。", "重试"),
    "resource.quota_exceeded": ErrorSpec("resource.quota_exceeded", ErrorCategory.BUSINESS, False, "已超出配额限制。", "调整用量"),
    "resource.result_ref_expired": ErrorSpec("resource.result_ref_expired", ErrorCategory.BUSINESS, False, "产物引用已过期，请重新生成或重新打开。", "重新生成"),
    "system.cancelled": ErrorSpec("system.cancelled", ErrorCategory.BUSINESS, False, "任务已取消。", "重新发起"),
    "system.internal": ErrorSpec("system.internal", ErrorCategory.FATAL, False, "任务执行器内部发生错误，请稍后重试；若持续出现请联系管理员。", "稍后重试"),
}

#: 域默认口径（未登记的具体码按域取）。
_DOMAIN_DEFAULTS: dict[str, ErrorSpec] = {
    ErrorDomain.MODEL.value: DOMAIN_ERROR_SPECS["model.provider_offline"],
    ErrorDomain.TOOL.value: DOMAIN_ERROR_SPECS["tool.failed"],
    ErrorDomain.CAPABILITY.value: FROZEN_ERROR_SPECS["CAPABILITY_UNAVAILABLE"],
    ErrorDomain.PERMISSION.value: FROZEN_ERROR_SPECS["PERMISSION_DENIED"],
    ErrorDomain.VALIDATION.value: DOMAIN_ERROR_SPECS["validation.schema_mismatch"],
    ErrorDomain.PLUGIN.value: DOMAIN_ERROR_SPECS["plugin.crashed"],
    ErrorDomain.RESOURCE.value: DOMAIN_ERROR_SPECS["resource.quota_exceeded"],
    ErrorDomain.SYSTEM.value: DOMAIN_ERROR_SPECS["system.internal"],
}

#: 仓库内既有错误码 → 统一错误码（唯一收敛处；新增旧码只往这里加）。
LEGACY_CODE_ALIASES: dict[str, str] = {
    # ── 权限 / 安全 ──
    "FORBIDDEN": "PERMISSION_DENIED",
    "UNAUTHORIZED": "PERMISSION_DENIED",
    "AUTH_REQUIRED": "PERMISSION_DENIED",
    "SSRF_BLOCKED": "SECURITY_BLOCKED",
    "SIDE_EFFECT_FORBIDDEN": "SECURITY_BLOCKED",
    "REDIRECT_REQUIRES_CONFIRMATION": "SECURITY_BLOCKED",
    # ── 能力 / 工具 ──
    "MCP_UNAVAILABLE": "CAPABILITY_UNAVAILABLE",
    "SKILL_NOT_FOUND": "CAPABILITY_UNAVAILABLE",
    "AGENT_NOT_FOUND": "CAPABILITY_UNAVAILABLE",
    "CAPABILITY_MISSING": "CAPABILITY_UNAVAILABLE",
    "CLIENT_OFFLINE": "CAPABILITY_UNAVAILABLE",
    "NOT_SUPPORTED_BY_PROVIDER": "CAPABILITY_UNAVAILABLE",
    "CROSS_DEVICE_UNSUPPORTED": "CAPABILITY_UNAVAILABLE",
    "TRASH_UNAVAILABLE": "CAPABILITY_UNAVAILABLE",
    "WORKSPACE_TOOLS_UNAVAILABLE": "CAPABILITY_UNAVAILABLE",
    "TOOL_NOT_FOUND": "TOOL_NOT_REGISTERED",
    "UNSUPPORTED_TOOL": "TOOL_NOT_REGISTERED",
    # ── 参数 / 校验 ──
    "INVALID_ARGS": "validation.schema_mismatch",
    "INVALID_ARGUMENTS": "validation.schema_mismatch",
    "INVALID_INPUT": "validation.schema_mismatch",
    "TARGET_MISSING": "TARGET_REQUIRED",
    "MISSING_TARGET": "TARGET_REQUIRED",
    "WORKSPACE_REQUIRED": "DEPENDENCY_MISSING_WORKSPACE",
    "MODEL_ACTION_REQUIRED": "APPROVAL_REQUIRED",
    # ── 模型 ──
    "TIMEOUT": "model.timeout",
    "MODEL_TIMEOUT": "model.timeout",
    "MODEL_RATE_LIMITED": "model.rate_limited",
    "MODEL_AUTH_ERROR": "PROVIDER_UNHEALTHY",
    "MODEL_UNAVAILABLE": "PROVIDER_UNHEALTHY",
    "MODEL_INSUFFICIENT_BALANCE": "PROVIDER_UNHEALTHY",
    "MODEL_TOOL_CALL_INVALID": "model.provider_offline",
    "MODEL_RESPONSE_INVALID": "model.provider_offline",
    "WEB_SEARCH_UNAVAILABLE": "model.provider_offline",
    "WEB_FETCH_FAILED": "tool.failed",
    # ── 资源 / 引用 ──
    "RESULT_REF_EXPIRED": "RESULT_REF_EXPIRED",
    "QUOTA_EXCEEDED": "resource.quota_exceeded",
    "OFFICE_JOB_LIMIT": "resource.quota_exceeded",
    "CHAT_STREAM_RATE_LIMIT": "resource.quota_exceeded",
    # ── 插件 ──
    "PLUGIN_CRASHED": "plugin.crashed",
    "PLUGIN_RESOURCE_EXCEEDED": "PLUGIN_RESOURCE_EXCEEDED",
    "PLUGIN_UNINSTALLED": "PLUGIN_UNINSTALLED",
    "DOCUMENT_RENDER_FAILED": "plugin.crashed",
    # ── 取消 / 兜底 ──
    "CANCELLED": "SYSTEM_CANCELLED",
    "CANCELED": "SYSTEM_CANCELLED",
    "TASK_CANCELLED": "SYSTEM_CANCELLED",
    "JOB_CANCELLED": "SYSTEM_CANCELLED",
    "USER_CANCELLED": "SYSTEM_CANCELLED",
    "USER_CANCELED": "SYSTEM_CANCELLED",
    "CANCELLED_BY_USER": "SYSTEM_CANCELLED",
    "USER_ABORTED": "SYSTEM_CANCELLED",
    "TIMEOUT_CANCELLED": "SYSTEM_CANCELLED",
    "INTERNAL_ERROR": "system.internal",
    "UNHANDLED_ERROR": "system.internal",
    "TASK_EXECUTION_ERROR": "system.internal",
    "EXEC_ERROR": "tool.failed",
    "RUN_NEXT_STREAM_INTERRUPTED": "system.internal",
}

#: 冻结码集合（前端文案表按此对齐；不含域内默认码）。
FROZEN_ERROR_CODES: frozenset[str] = frozenset(FROZEN_ERROR_SPECS)


def _normalize_code(value: Any) -> str:
    return str(value or "").strip()


def spec_for(code: Any) -> ErrorSpec:
    """错误码 → 稳定口径（冻结码 > 域内码 > 旧码别名 > 域默认 > ``system.internal``）。

    未登记但"看起来像错误码"的值会**保留原码**（排障需要），口径取兜底：
    前端只展示 ``safe_message``，未登记码落回通用文案。
    """
    text = _normalize_code(code)
    if not text:
        return DOMAIN_ERROR_SPECS["system.internal"]
    if text in FROZEN_ERROR_SPECS:
        return FROZEN_ERROR_SPECS[text]
    if text in DOMAIN_ERROR_SPECS:
        return DOMAIN_ERROR_SPECS[text]
    alias = LEGACY_CODE_ALIASES.get(text)
    if alias and alias != text:
        return spec_for(alias)
    prefix = text.split(".", 1)[0].strip().casefold()
    if "." in text and prefix in _DOMAIN_DEFAULTS:
        base = _DOMAIN_DEFAULTS[prefix]
        # 未登记的具体码：保留原码（排障用），口径取域默认。
        return ErrorSpec(text, base.category, base.retryable, base.safe_message, base.next_action)
    if _looks_like_code(text):
        # 未登记码（如 REVISION_REQUIRED）：保留原码，但文案/类别走兜底。
        base = DOMAIN_ERROR_SPECS["system.internal"]
        return ErrorSpec(text, base.category, base.retryable, base.safe_message, base.next_action)
    return DOMAIN_ERROR_SPECS["system.internal"]


class UnifiedError(BaseModel):
    """公开事件里的统一错误（前端只展示 ``safe_message``）。

    * ``detail_ref``：完整堆栈/供应商原文所在产物的引用（受权限保护），没有则为空；
    * ``suggested_action``：兼容既有 ``ErrorEnvelope``/前端按钮文案；
    * **不含** 原始异常文本、供应商响应、堆栈、工具参数与文件路径。
    """

    model_config = ConfigDict(extra="ignore")

    code: str = "system.internal"
    category: str = ErrorCategory.FATAL.value
    retryable: bool = False
    safe_message: str = ""
    detail_ref: str = ""
    step_id: str = ""
    suggested_action: str = ""

    @classmethod
    def from_code(
        cls,
        code: Any = "",
        *,
        step_id: str = "",
        detail_ref: str = "",
        suggested_action: str = "",
        safe_message: str = "",
        retryable: bool | None = None,
    ) -> "UnifiedError":
        spec = spec_for(code)
        message = str(safe_message or "")
        if looks_internal(message):
            # 调用方递进来的"用户文案"其实带着堆栈/库路径 → 丢弃，用登记文案兜底。
            message = ""
        return cls(
            code=spec.code,
            category=spec.category.value,
            retryable=spec.retryable if retryable is None else bool(retryable),
            safe_message=message or spec.safe_message,
            detail_ref=str(detail_ref or ""),
            step_id=str(step_id or ""),
            suggested_action=str(suggested_action or spec.next_action),
        )

    def to_payload(self) -> dict[str, Any]:
        """事件载荷形态（``None``/空串一律不出现，前端不必判空）。"""
        data = self.model_dump(mode="json")
        return {key: value for key, value in data.items() if value not in ("", None)}


def _code_of(source: Any) -> str:
    for attr in ("error_code", "code"):
        value = getattr(source, attr, None)
        if value:
            return _normalize_code(value)
    return ""


def _looks_like_code(text: str) -> bool:
    """粗略判定"这是一个错误码"而不是自然语言句子。"""
    raw = text.strip()
    if not raw or len(raw) > 64 or " " in raw or "\n" in raw:
        return False
    return all(char.isalnum() or char in "._-" for char in raw)


#: 内部痕迹标记：命中即认为这段文本是"排障信息"而不是"用户文案"，
#: 一律换成登记过的 ``safe_message``（方案 §3.3：堆栈只进日志/产物）。
_INTERNAL_MARKERS: tuple[str, ...] = (
    "traceback (most recent call last)",
    'file "',
    "stack trace",
    "site-packages",
    "node_modules",
    "at line ",
    "errno",
    "errno:",
    "0x",
)


def looks_internal(text: Any) -> bool:
    """这段文本是否带内部痕迹（堆栈、库路径、内存地址…），不能作为用户文案。"""
    lowered = str(text or "").strip().casefold()
    if not lowered:
        return False
    return any(marker in lowered for marker in _INTERNAL_MARKERS)


def translate_error(
    error: Any,
    *,
    code: str = "",
    step_id: str = "",
    detail_ref: str = "",
) -> UnifiedError:
    """任意错误来源 → :class:`UnifiedError`（唯一翻译器，出口只消费它）。

    接受 ``UnifiedError`` / ``dict`` / 异常对象 / 字符串；**绝不**把原始异常文本
    放进 ``safe_message``——那是排障信息，只允许进日志或 ``detail_ref`` 指向的产物。
    """
    if isinstance(error, UnifiedError):
        if step_id and not error.step_id:
            return error.model_copy(update={"step_id": str(step_id)})
        return error
    if isinstance(error, dict):
        explicit = _normalize_code(error.get("code") or error.get("error_code") or code)
        return UnifiedError.from_code(
            explicit,
            step_id=str(error.get("step_id") or step_id),
            detail_ref=str(error.get("detail_ref") or detail_ref),
            suggested_action=str(error.get("suggested_action") or ""),
            safe_message=str(error.get("safe_message") or ""),
            retryable=error.get("retryable") if isinstance(error.get("retryable"), bool) else None,
        )
    if isinstance(error, BaseException):
        explicit = _normalize_code(code or _code_of(error))
        retryable = getattr(error, "retryable", None)
        return UnifiedError.from_code(
            explicit,
            step_id=step_id,
            detail_ref=detail_ref,
            retryable=retryable if isinstance(retryable, bool) else None,
        )
    text = _normalize_code(error)
    if not text:
        return UnifiedError.from_code(code, step_id=step_id, detail_ref=detail_ref)
    if not code and _looks_like_code(text):
        return UnifiedError.from_code(text, step_id=step_id, detail_ref=detail_ref)
    # 自由文本：一律收敛为兜底错误码，不把原文透给前端。
    return UnifiedError.from_code(code or "system.internal", step_id=step_id, detail_ref=detail_ref)


__all__ = [
    "DOMAIN_ERROR_SPECS",
    "ErrorCategory",
    "ErrorDomain",
    "ErrorSpec",
    "FROZEN_ERROR_CODES",
    "FROZEN_ERROR_SPECS",
    "LEGACY_CODE_ALIASES",
    "UNIFIED_ERROR_VERSION",
    "UnifiedError",
    "looks_internal",
    "spec_for",
    "translate_error",
]
