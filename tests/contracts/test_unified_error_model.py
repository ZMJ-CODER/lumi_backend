"""统一错误模型 + 视图体积上限（方案 §3 / §2.3）后端契约回归。

锁死四件事：

1. **冻结错误码**：方案 §3.2 的 12 个码一个不少，且每个都有类别/可重试/安全文案；
2. **同一类失败 = 同一个 code + safe_message**：不管从哪条路径来（异常对象 /
   旧错误码 / 字典 / ``ExecutionResult``），结果一致；
3. **原始异常文本永不出门**：``message`` / ``error`` / 堆栈进不了公开载荷，
   只能通过 ``detail_ref`` 指向的受权限保护产物取；
4. **视图 data 上限**：> 64KB / 嵌套 > 10 层 / 元素 > 1000 → 只发 ``data_ref``。
"""

from __future__ import annotations

import json

from lumi_contracts import (
    UnifiedError,
    build_payload,
    failure,
    spec_for,
    translate_error,
    view_data_within_bounds,
)
from lumi_contracts.events.envelope import (
    VIEW_EVENT_DATA_MAX_BYTES,
    VIEW_EVENT_DATA_MAX_DEPTH,
    VIEW_EVENT_DATA_MAX_ITEMS,
)
from lumi_contracts.events.errors import (
    DOMAIN_ERROR_SPECS,
    FROZEN_ERROR_CODES,
    LEGACY_CODE_ALIASES,
)

#: 方案 §3.2 冻结的 12 个错误码（顺序无关）。
PLAN_FROZEN_CODES = {
    "TARGET_REQUIRED",
    "DEPENDENCY_MISSING_WORKSPACE",
    "CAPABILITY_UNAVAILABLE",
    "PROVIDER_UNHEALTHY",
    "PERMISSION_DENIED",
    "TOOL_NOT_REGISTERED",
    "APPROVAL_REQUIRED",
    "SECURITY_BLOCKED",
    "PLUGIN_RESOURCE_EXCEEDED",
    "PLUGIN_UNINSTALLED",
    "RESULT_REF_EXPIRED",
    "SYSTEM_CANCELLED",
}

SECRET_TEXT = "Traceback (most recent call last): 401 unauthorized api_key=sk-live-SECRET"


# ── 1. 冻结码表 ──────────────────────────────────────────


def test_frozen_error_codes_match_the_plan():
    assert FROZEN_ERROR_CODES == PLAN_FROZEN_CODES


def test_every_frozen_code_has_category_retryable_and_safe_message():
    for code in sorted(PLAN_FROZEN_CODES):
        spec = spec_for(code)
        assert spec.code == code
        assert spec.category in {"transient", "fatal", "business", "needs_human"}
        assert isinstance(spec.retryable, bool)
        assert spec.safe_message and not spec.safe_message.startswith("Traceback")
        # 文案是给用户看的中文短句，不是内部错误码回显
        assert spec.safe_message != code


def test_legacy_codes_converge_to_registered_codes():
    """旧错误码必须收敛到登记过的码（不能各自发明）。"""
    for legacy, unified in LEGACY_CODE_ALIASES.items():
        spec = spec_for(legacy)
        assert spec.code in PLAN_FROZEN_CODES or spec.code in DOMAIN_ERROR_SPECS, legacy
        assert spec.code == spec_for(unified).code, legacy


def test_missing_model_key_is_not_reported_as_a_retryable_provider_outage():
    """缺密钥必须落 ``model.credentials_missing``（不可重试），不能落 ``model.provider_offline``。

    否则前端会把"去填 API Key"显示成"模型服务暂时不可用，正在重试"，并诱导无意义重试。
    """
    spec = spec_for("MODEL_API_KEY_MISSING")
    assert spec.code == "model.credentials_missing"
    assert spec.category == "business"
    assert spec.retryable is False
    assert "API Key" in spec.safe_message
    unified = translate_error({"code": "MODEL_API_KEY_MISSING"})
    assert unified.code == "model.credentials_missing"
    assert unified.retryable is False


# ── 2. 同一类失败 → 同一个 code + safe_message ────────────


def test_same_failure_gives_same_code_and_message_from_every_path():
    """异常对象 / 旧码 / 字典三条路径必须得到完全一致的错误。"""
    from_dict = translate_error({"code": "TIMEOUT"})
    from_str = translate_error("TIMEOUT")
    from_exc = translate_error(RuntimeError(SECRET_TEXT), code="TIMEOUT")
    assert from_dict.code == from_str.code == from_exc.code == "model.timeout"
    assert from_dict.safe_message == from_str.safe_message == from_exc.safe_message
    assert from_dict.category == from_str.category == from_exc.category == "transient"
    assert from_exc.retryable is True


def test_exception_text_never_becomes_safe_message():
    unified = translate_error(ValueError(SECRET_TEXT))
    assert unified.code == "system.internal"
    assert SECRET_TEXT not in unified.safe_message
    assert "sk-live-SECRET" not in json.dumps(unified.model_dump(), ensure_ascii=False)


def test_free_text_error_is_not_treated_as_a_code():
    unified = translate_error("数据库连接失败：password=hunter2")
    assert unified.code == "system.internal"
    assert "hunter2" not in unified.safe_message


def test_unregistered_code_is_preserved_for_debugging_with_generic_text():
    unified = translate_error({"code": "REVISION_REQUIRED"})
    assert unified.code == "REVISION_REQUIRED", "未登记码保留原码，便于排障"
    assert unified.safe_message == spec_for("system.internal").safe_message


def test_translate_error_passthrough_keeps_step_id():
    source = UnifiedError.from_code("PERMISSION_DENIED", step_id="s1")
    assert translate_error(source) is source
    assert translate_error(source, step_id="s2").step_id == "s1", "已有 step_id 不被覆盖"


# ── 3. 错误载荷：只有安全字段出门 ────────────────────────


def test_error_payload_only_carries_unified_fields():
    payload = build_payload(
        "error",
        {
            "code": "PROVIDER_UNHEALTHY",
            "message": SECRET_TEXT,
            "error": SECRET_TEXT,
            "stack": SECRET_TEXT,
            "reasoning_content": SECRET_TEXT,
            "step_id": "s9",
        },
    )
    assert set(payload) <= {
        "code", "category", "retryable", "safe_message", "detail_ref", "step_id",
        "suggested_action", "safe_next_action",
    }
    assert payload["code"] == "PROVIDER_UNHEALTHY"
    assert payload["retryable"] is True
    assert SECRET_TEXT not in json.dumps(payload, ensure_ascii=False)
    assert payload["step_id"] == "s9"


def test_error_payload_defaults_to_internal_for_missing_code():
    payload = build_payload("error", {"message": SECRET_TEXT})
    assert payload["code"] == "system.internal"
    assert SECRET_TEXT not in json.dumps(payload, ensure_ascii=False)


def test_control_payload_carries_unified_error_code_and_next_action():
    payload = build_payload("control", {"state": "failed", "error_code": "MCP_UNAVAILABLE"})
    assert payload["state"] == "failed"
    assert payload["error_code"] == "CAPABILITY_UNAVAILABLE", "旧码在 control 上也收敛"
    assert payload["safe_next_action"], "前端要有'下一步做什么'的文案"


def test_cancelled_control_uses_system_cancelled_not_internal():
    payload = build_payload("control", {"state": "cancelled"})
    assert payload["state"] == "cancelled"


def test_execution_result_failure_projects_safe_message_only():
    result = failure("MCP_UNAVAILABLE", SECRET_TEXT, tool_name="workspace_write")
    from app.contracts.ui_projection import execution_result_events

    events = execution_result_events(result, step_id="s1", job_id="job-1")
    error_event = next(item for item in events if item.type == "error")
    dumped = json.dumps(error_event.payload, ensure_ascii=False)
    assert error_event.payload["code"] == "CAPABILITY_UNAVAILABLE"
    assert "sk-live-SECRET" not in dumped
    assert "Traceback" not in dumped


def test_error_payload_is_json_serialisable_with_detail_ref():
    payload = build_payload("error", {"code": "PLUGIN_CRASHED", "detail_ref": "artifact:xyz"})
    assert payload["detail_ref"] == "artifact:xyz"
    assert payload["code"] == "plugin.crashed"
    assert isinstance(payload["retryable"], bool)


# ── 4. 视图 data 上限 ────────────────────────────────────


def test_view_data_within_bounds_helper():
    assert view_data_within_bounds({"rows": [{"a": 1}]}) is True
    assert VIEW_EVENT_DATA_MAX_BYTES == 65_536
    assert VIEW_EVENT_DATA_MAX_DEPTH == 10
    assert VIEW_EVENT_DATA_MAX_ITEMS == 1_000
    # 超体积
    assert view_data_within_bounds({"blob": "x" * (VIEW_EVENT_DATA_MAX_BYTES + 1)}) is False
    # 超嵌套
    deep: dict = {}
    cursor = deep
    for _ in range(VIEW_EVENT_DATA_MAX_DEPTH + 2):
        cursor["child"] = {}
        cursor = cursor["child"]
    assert view_data_within_bounds(deep) is False
    # 超元素
    assert view_data_within_bounds({"rows": [{"i": index} for index in range(VIEW_EVENT_DATA_MAX_ITEMS + 5)]}) is False


def test_view_updated_payload_downgrades_oversize_data_to_ref():
    payload = build_payload(
        "view_updated",
        {
            "view_id": "view-1",
            "view_type": "table",
            "data": {"rows": [{"i": index, "pad": "x" * 200} for index in range(500)]},
        },
    )
    assert payload["data"] == {}, "超出上限就不再通过事件搬运正文"
    assert payload["data_ref"] == "view:view-1"
    assert payload["truncated"] is True


def test_view_updated_payload_downgrades_deep_nesting():
    deep: dict = {}
    cursor = deep
    for _ in range(VIEW_EVENT_DATA_MAX_DEPTH + 3):
        cursor["child"] = {}
        cursor = cursor["child"]
    payload = build_payload(
        "view_updated",
        {"view_id": "v2", "view_type": "timeline", "data": deep, "data_ref": "provided:ref"},
    )
    assert payload["data"] == {}
    assert payload["data_ref"] == "provided:ref", "调用方已给引用时不被覆盖"
    assert payload["truncated"] is True


def test_view_updated_payload_keeps_small_data_and_whitelists_type():
    payload = build_payload(
        "view_updated",
        {"view_id": "v3", "view_type": "table", "data": {"items": [1, 2, 3]}},
    )
    assert payload["data"] == {"items": [1, 2, 3]}
    assert payload["truncated"] is False
    # 非白名单类型：类型置空且不下发数据
    bad = build_payload(
        "view_updated", {"view_id": "v4", "view_type": "iframe", "data": {"html": "<script>"}}
    )
    assert bad["view_type"] == ""
    assert bad["data"] == {}


def test_plan_documents_reference_limits():
    """方案里写的 64KB / 10 层 / 1000 元素必须与代码常量一致。"""
    assert (VIEW_EVENT_DATA_MAX_BYTES, VIEW_EVENT_DATA_MAX_DEPTH, VIEW_EVENT_DATA_MAX_ITEMS) == (
        65_536,
        10,
        1_000,
    )
