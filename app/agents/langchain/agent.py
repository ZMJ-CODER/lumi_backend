"""受控单工具选择 Runnable。

LangChain 负责模型消息、工具绑定和工具调用解析；Lumi 仍是权限、审计、
资源互斥与实际执行的唯一权威，避免把安全边界隐含在 Agent 提示词里。
"""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.langchain.models import get_chat_model


def _json_object(content: object) -> dict:
    """Parse the smallest JSON object from an LLM response without eval."""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("模型未返回 JSON 参数对象")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("模型参数必须是 JSON 对象")
    return value


async def extract_tool_arguments(
    *,
    instruction: str,
    explicit_inputs: dict,
    dependency_results: dict,
    tool_definition: dict,
    scene: str,
    user_id: str,
    api_key: str | None = None,
    llm_config: dict | None = None,
) -> dict:
    """Extract arguments for one already-authorized tool using JSON mode.

    This does not let the model select a capability.  It can only fill the
    schema of the Planner-selected tool, after which the execution layer still
    applies permission, confirmation, resource and audit checks.
    """
    function = tool_definition.get("function") or {}
    name = str(function.get("name") or "")
    schema = function.get("parameters") or {}
    model = await get_chat_model(scene=scene, user_id=user_id, api_key=api_key, temperature=0, llm_config=llm_config)
    system = (
        "你是受控参数提取器。工具已经由规划器授权，不能改换工具、不能执行工具、"
        "不能解释任务。只输出一个 JSON 对象，键必须属于给定 JSON Schema 的 properties。"
        "优先保留显式输入；无法从用户指令或已完成依赖结果可靠得出的字段不要编造。"
    )
    user = (
        f"已授权工具：{name}\n"
        f"参数 Schema：{json.dumps(schema, ensure_ascii=False)}\n"
        f"原子步骤：{instruction}\n"
        f"显式输入：{json.dumps(explicit_inputs or {}, ensure_ascii=False, default=str)[:12000]}\n"
        f"依赖结果：{json.dumps(dependency_results or {}, ensure_ascii=False, default=str)[:24000]}"
    )
    reply = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    return _json_object(reply.content)


async def choose_single_tool(
    *,
    system: str,
    user: str,
    tool,
    scene: str,
    user_id: str,
    api_key: str | None = None,
    llm_config: dict | None = None,
) -> tuple[str, list[dict]]:
    """执行一次强制单工具选择，输出兼容现有执行器的 OpenAI tool-call 结构。"""
    model = await get_chat_model(
        scene=scene,
        user_id=user_id,
        api_key=api_key,
        temperature=0,
        llm_config=llm_config,
    )
    runnable = model.bind_tools([tool], tool_choice=tool.name)
    reply = await runnable.ainvoke([SystemMessage(content=system), HumanMessage(content=user)])
    calls = []
    for call in reply.tool_calls or []:
        calls.append(
            {
                "id": str(call.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(call.get("name") or ""),
                    "arguments": call.get("args") or {},
                },
            }
        )
    return str(reply.content or ""), calls
