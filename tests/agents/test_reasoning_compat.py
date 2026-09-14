"""OpenAI-compatible thinking-mode tool-loop compatibility tests."""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.langchain.models import CompatibleChatOpenAI


def _model() -> CompatibleChatOpenAI:
    return CompatibleChatOpenAI(
        model="compatible-thinking-model",
        api_key="test-key",
        base_url="http://example.invalid/v1",
    )


def test_compatible_model_preserves_reasoning_content_from_response():
    response = {
        "id": "chatcmpl-test",
        "model": "compatible-thinking-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "internal provider state",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    result = _model()._create_chat_result(response)
    message = result.generations[0].message
    assert isinstance(message, AIMessage)
    assert message.additional_kwargs["reasoning_content"] == "internal provider state"


def test_compatible_model_replays_reasoning_content_after_tool_result():
    tool_call = {"name": "lookup", "args": {}, "id": "call-1"}
    assistant = AIMessage(
        content="",
        tool_calls=[tool_call],
        additional_kwargs={"reasoning_content": "internal provider state"},
    )
    payload = _model()._get_request_payload(
        [
            HumanMessage(content="find data"),
            assistant,
            ToolMessage(content="result", tool_call_id="call-1", name="lookup"),
        ]
    )
    assistant_payload = payload["messages"][1]
    assert assistant_payload["tool_calls"][0]["id"] == "call-1"
    assert assistant_payload["reasoning_content"] == "internal provider state"
