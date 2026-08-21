"""统一的技能/模型失败分类与恢复决策。

执行器和两套 DAG 引擎都使用同一套语义，避免把参数错误、权限拒绝、
沙箱不可用和临时网络错误当成同一种失败反复执行。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecoveryDecision:
    category: str
    retry_same: bool = False
    try_alternative: bool = False
    replan_required: bool = False
    user_action_required: bool = False
    safe_to_retry: bool = False


_INPUT = {"INVALID_ARGS", "RULE_VIOLATION", "TOOL_NOT_PLANNED", "TOOL_NOT_CALLED", "NON_ATOMIC_TOOL_CALL"}
_PERMISSION = {"FORBIDDEN", "NEEDS_CONFIRMATION", "REJECTED"}
_CAPABILITY = {"SANDBOX_REQUIRED", "SKILL_NOT_FOUND", "MCP_UNAVAILABLE", "CLIENT_OFFLINE", "CLIENT_TIMEOUT"}
_TRANSIENT = {"TIMEOUT", "RATE_LIMIT", "NETWORK_ERROR", "MODEL_EMPTY_RESPONSE", "MODEL_UNAVAILABLE"}
_MODEL_ACTION_REQUIRED = {
    "MODEL_INSUFFICIENT_BALANCE",
    "MODEL_AUTH_ERROR",
    "MODEL_NOT_FOUND",
    "MODEL_CONFIG_ERROR",
    "MODEL_TOOL_CALL_UNSUPPORTED",
}


def classify_model_error(error: Exception | str) -> tuple[str, str]:
    """将上游 OpenAI-compatible 错误转换为稳定、面向用户的错误语义。

    供应商对同一问题的响应格式并不一致（HTTP 状态、JSON code 或纯文本），
    因此不能把所有异常都笼统记为 ``MODEL_UNAVAILABLE`` 后盲目重试。
    """
    text = str(error or "")
    lowered = text.lower()
    if (
        "402" in lowered
        or "insufficient balance" in lowered
        or "insufficient_balance" in lowered
        or "余额不足" in text
    ):
        return (
            "MODEL_INSUFFICIENT_BALANCE",
            "当前模型账户余额不足，办公任务已停止。请充值，或在模型设置中切换到可用模型后重试。",
        )
    missing_credentials = (
        "missing credentials" in lowered
        or "api key is required" in lowered
        or "api_key is required" in lowered
    )
    if (
        "401" in lowered
        or "invalid api key" in lowered
        or "authentication" in lowered
        or "unauthorized" in lowered
        or missing_credentials
    ):
        message = (
            "当前自备模型未携带 API 密钥，办公任务已停止。请在模型设置中重新保存自备 API，"
            "或切换到内置模型后重试。"
            if missing_credentials
            else "模型 API 密钥无效或已失效，办公任务已停止。请在模型设置中检查密钥后重试。"
        )
        return (
            "MODEL_AUTH_ERROR",
            message,
        )
    if "404" in lowered or "model_not_found" in lowered or "model not found" in lowered:
        return (
            "MODEL_NOT_FOUND",
            "当前选择的模型不可用或已下架，办公任务已停止。请在模型设置中切换到可用模型后重试。",
        )
    # A text/JSON capable model may still reject the provider's Function
    # Calling dialect.  This is a capability mismatch of that request, not an
    # invalid model name or API key, and must not trigger repeated replanning.
    if any(marker in lowered for marker in (
        "tool_choice", "tool choice", "function calling", "function_call",
        "tool calls are not supported", "tools are not supported",
    )):
        return (
            "MODEL_TOOL_CALL_UNSUPPORTED",
            "当前模型接口不支持工具调用格式。系统会优先使用已规划参数直接执行；"
            "若仍无法完成，请切换支持 Function Calling 的模型接口后重试。",
        )
    if "unsupported parameter" in lowered or "unknown parameter" in lowered:
        return (
            "MODEL_CONFIG_ERROR",
            "模型服务商拒绝了高级参数，办公任务已停止。系统已默认关闭不兼容的推理参数；"
            "请重新发起任务，如仍失败请检查自备接口的模型名称和地址。",
        )
    if "400" in lowered or "invalid_request" in lowered:
        return (
            "MODEL_CONFIG_ERROR",
            "当前模型配置不被服务商支持，办公任务已停止。请检查模型名称、接口地址和高级参数后重试。",
        )
    return ("MODEL_UNAVAILABLE", "模型服务暂时不可用，请稍后重试或切换到其他模型。")


def classify_failure(error_code: str | None, error: str | None = "", retryable: bool = False) -> str:
    code = str(error_code or "").upper()
    if code in _MODEL_ACTION_REQUIRED:
        return "model_action_required"
    # 兼容尚未使用 ``classify_model_error`` 的历史调用点。
    if code == "MODEL_UNAVAILABLE":
        inferred, _ = classify_model_error(error or "")
        if inferred in _MODEL_ACTION_REQUIRED:
            return "model_action_required"
    if code in _INPUT:
        return "input"
    if code in _PERMISSION:
        return "permission"
    if code in _CAPABILITY:
        return "capability_unavailable"
    if code in _TRANSIENT:
        return "transient"
    text = str(error or "").lower()
    if any(x in text for x in ("timeout", "timed out", "429", "连接", "network", "temporarily", "空内容")):
        return "transient"
    if retryable:
        return "transient"
    return "execution"


def decide_failure(
    error_code: str | None,
    error: str | None = "",
    *,
    retryable: bool = False,
    effectful: bool = False,
    alternatives_remaining: bool = False,
) -> RecoveryDecision:
    """返回是否可重试/换工具；副作用步骤永不盲目重试。"""
    category = classify_failure(error_code, error, retryable)
    if effectful:
        return RecoveryDecision(category, replan_required=category in {"input", "capability_unavailable"})
    if category == "transient":
        return RecoveryDecision(category, retry_same=True, safe_to_retry=True)
    if category == "capability_unavailable":
        return RecoveryDecision(
            category,
            try_alternative=alternatives_remaining,
            replan_required=not alternatives_remaining,
            safe_to_retry=alternatives_remaining,
        )
    if category == "execution":
        # 同一实现已报错时，优先尝试规划器给出的不同方法；例如解析器无法识别
        # 特殊文件时可转脚本/另一个结构化读取器。没有备用方法则要求重规划。
        return RecoveryDecision(
            category,
            retry_same=retryable,
            try_alternative=alternatives_remaining,
            replan_required=not retryable and not alternatives_remaining,
            safe_to_retry=retryable or alternatives_remaining,
        )
    if category == "permission":
        return RecoveryDecision(category, user_action_required=True)
    if category == "model_action_required":
        return RecoveryDecision(category, user_action_required=True)
    return RecoveryDecision(category, replan_required=True)


def result_recovery(result: dict, *, effectful: bool = False, alternatives_remaining: bool = False) -> RecoveryDecision:
    return decide_failure(
        result.get("error_code"),
        result.get("error"),
        retryable=bool(result.get("retryable")),
        effectful=effectful,
        alternatives_remaining=alternatives_remaining,
    )
