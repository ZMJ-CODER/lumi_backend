from app.services.skill_telemetry import _error_class


def test_skill_telemetry_normalizes_unknown_error_codes():
    assert _error_class(None) == "NONE"
    assert _error_class("mcp_timeout") == "MCP_TIMEOUT"
    assert _error_class("remote-provider-raw-stacktrace") == "OTHER"
