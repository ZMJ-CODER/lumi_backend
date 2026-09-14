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
    assert classify_model_error("404 model not found")[0] == "MODEL_NOT_FOUND"
    code, message = classify_model_error("400 unsupported parameter: reasoning_effort")
    assert code == "MODEL_CONFIG_ERROR"
    assert "高级参数" in message


def test_missing_credentials_is_key_missing_not_auth_error():
    """缺密钥是"去填 Key"（400），不是"密钥无效/登录过期"（401）。"""
    for error in (
        "Missing credentials. Please pass an api_key",
        "api_key is required",
        "No API key provided",
    ):
        code, message = classify_model_error(error)
        assert code == "MODEL_API_KEY_MISSING", error
        assert "API Key" in message
    decision = decide_failure("MODEL_API_KEY_MISSING", "")
    assert decision.category == "model_action_required"
    assert decision.user_action_required is True
    assert decision.retry_same is False


def test_structured_error_code_wins_over_text_heuristics():
    """上游已归一的结构化码优先，不再靠文本二次猜测。"""

    class FakeAppException(Exception):
        error_code = "MODEL_API_KEY_MISSING"

    code, message = classify_model_error(FakeAppException("当前使用「自带密钥」，请在设置里填写模型 API Key 后重试"))
    assert code == "MODEL_API_KEY_MISSING"
    assert "自带密钥" in message
    assert classify_model_error("401 unauthorized")[0] == "MODEL_AUTH_ERROR"


def test_tool_call_dialect_error_is_not_reported_as_model_name_error():
    code, message = classify_model_error("400 invalid_request: tool_choice is not supported")
    assert code == "MODEL_TOOL_CALL_UNSUPPORTED"
    assert "工具调用格式" in message
    decision = decide_failure(code, message)
    assert decision.user_action_required is True
    assert decision.replan_required is False
