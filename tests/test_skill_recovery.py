"""统一失败恢复语义测试。"""

from app.agents.skills.recovery import classify_model_error, decide_failure


def test_sandbox_requires_alternative_instead_of_same_retry():
    decision = decide_failure("SANDBOX_REQUIRED", alternatives_remaining=True)
    assert decision.category == "capability_unavailable"
    assert decision.try_alternative is True
    assert decision.retry_same is False


def test_effectful_failure_never_retries_or_switches_tool():
    decision = decide_failure("TIMEOUT", retryable=True, effectful=True, alternatives_remaining=True)
    assert decision.retry_same is False
    assert decision.try_alternative is False


def test_model_insufficient_balance_requires_user_action_not_retry():
    code, message = classify_model_error("Error code: 402 Insufficient Balance")
    assert code == "MODEL_INSUFFICIENT_BALANCE"
    assert "余额不足" in message
    decision = decide_failure(code, message, retryable=True)
    assert decision.category == "model_action_required"
    assert decision.user_action_required is True
    assert decision.retry_same is False


def test_model_auth_and_missing_model_are_actionable():
    assert classify_model_error("401 unauthorized")[0] == "MODEL_AUTH_ERROR"
    code, message = classify_model_error("Missing credentials. Please pass an api_key")
    assert code == "MODEL_AUTH_ERROR"
    assert "未携带" in message
    assert classify_model_error("404 model not found")[0] == "MODEL_NOT_FOUND"
    code, message = classify_model_error("400 unsupported parameter: reasoning_effort")
    assert code == "MODEL_CONFIG_ERROR"
    assert "高级参数" in message


def test_tool_call_dialect_error_is_not_reported_as_model_name_error():
    code, message = classify_model_error("400 invalid_request: tool_choice is not supported")
    assert code == "MODEL_TOOL_CALL_UNSUPPORTED"
    assert "工具调用格式" in message
    decision = decide_failure(code, message)
    assert decision.user_action_required is True
    assert decision.replan_required is False
