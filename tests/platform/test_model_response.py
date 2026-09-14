from app.platform.model.model_response import normalize_tool_response


def test_normalize_dsml_tool_call():
    text = '<｜｜DSML｜｜invoke name="get_current_date" arguments="{\\"format\\":\\"datetime\\"}"/>'
    clean, calls, warnings = normalize_tool_response(text)
    assert clean == ""
    assert calls[0]["function"]["name"] == "get_current_date"
    assert calls[0]["function"]["arguments"] == {"format": "datetime"}
    assert warnings == []


def test_normalize_invalid_dsml_args_is_structured_warning():
    _, calls, warnings = normalize_tool_response(
        '<｜｜DSML｜｜invoke name="calculator" arguments="not-json"/>'
    )
    assert calls[0]["function"]["arguments"] == {}
    assert warnings


def test_normalize_text_json_tool_call_and_direct_answer():
    clean, calls, warnings = normalize_tool_response(
        '{"name":"web_search","arguments":{"query":"LangGraph"}}'
    )
    assert clean == ""
    assert calls[0]["function"]["name"] == "web_search"
    assert calls[0]["function"]["arguments"] == {"query": "LangGraph"}
    assert warnings == []


def test_normalize_native_tool_call_preserves_reasoning_content():
    _, calls, _ = normalize_tool_response(
        "",
        [{
            "id": "c1",
            "type": "function",
            "reasoning_content": "先读取用户工作区",
            "function": {"name": "workspace_read", "arguments": {}},
        }],
    )
    assert calls[0]["reasoning_content"] == "先读取用户工作区"

    clean, calls, warnings = normalize_tool_response('{"answer":"可以直接回答"}')
    assert clean == "可以直接回答"
    assert calls == []
    assert warnings == []
