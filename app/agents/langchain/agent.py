"""受控单工具选择 Runnable。

LangChain 负责模型消息、工具绑定和工具调用解析；Lumi 仍是权限、审计、
资源互斥与实际执行的唯一权威，避免把安全边界隐含在 Agent 提示词里。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.langchain.models import get_chat_model


async def choose_single_tool(
    *,
    system: str,
    user: str,
    tool,
    scene: str,
    user_id: str,
    api_key: str | None = None,
) -> tuple[str, list[dict]]:
    """执行一次强制单工具选择，输出兼容现有执行器的 OpenAI tool-call 结构。"""
    model = await get_chat_model(
        scene=scene,
        user_id=user_id,
        api_key=api_key,
        temperature=0,
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
