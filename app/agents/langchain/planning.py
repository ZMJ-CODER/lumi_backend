"""规划器的 LangChain 结构化输出契约。"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from app.agents.langchain.models import get_chat_model
from app.core.config import settings


class PlannedTask(BaseModel):
    id: str = ""
    name: str = ""
    agent: str
    params: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class PlannerOutput(BaseModel):
    plan: str = ""
    tasks: list[PlannedTask] = Field(default_factory=list)
    clarification: str = ""


def _message_text(reply: Any) -> str:
    content = getattr(reply, "content", reply)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _parse_json_planner_output(text: str) -> PlannerOutput:
    """解析模型普通文本中的单个 JSON 对象，拒绝非 JSON 规划结果。"""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # 兼容模型在 JSON 前后附加了一句说明；只取第一个完整对象，仍交由
        # Pydantic 验证字段，不能把任意自由文本当成计划。
        start = raw.find("{")
        if start < 0:
            raise ValueError("模型未返回 JSON 格式的任务计划") from None
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
    return PlannerOutput.model_validate(value)


async def invoke_structured_planner(
    prompt: str,
    *,
    user_id: str,
    api_key: str | None = None,
) -> PlannerOutput:
    """调用规划模型并解析普通 JSON。

    办公模型要兼容大量 OpenAI-compatible 网关。许多网关会在接收
    ``response_format=json_schema`` 后先长时间推理、再返回 400；先发原生
    structured-output 请求会让每个任务至少多一次完整模型往返。规划任务本身
    已有严格 JSON 提示词和 Pydantic 校验，因此默认直接使用普通聊天调用。
    """
    model = await get_chat_model(
        scene="office",
        user_id=user_id,
        api_key=api_key,
        temperature=0.1,
        max_tokens=settings.AGENT_PLANNER_MAX_TOKENS,
        timeout=settings.AGENT_PLANNER_TIMEOUT_SECONDS,
    )
    reply = await model.ainvoke([HumanMessage(content=prompt)])
    return _parse_json_planner_output(_message_text(reply))


async def invoke_json_object(
    prompt: str,
    *,
    user_id: str,
    api_key: str | None = None,
    max_tokens: int = 2000,
) -> dict[str, Any] | None:
    """无固定 Schema 的 JSON 对象，走兼容的普通聊天调用。"""
    model = await get_chat_model(
        scene="office",
        user_id=user_id,
        api_key=api_key,
        temperature=0.1,
        max_tokens=max_tokens,
        timeout=settings.AGENT_PLANNER_TIMEOUT_SECONDS,
    )
    reply = await model.ainvoke([HumanMessage(content=prompt)])
    raw = _message_text(reply).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        if start < 0:
            raise ValueError("模型未返回 JSON 对象") from None
        value, _ = json.JSONDecoder().raw_decode(raw[start:])
    if not isinstance(value, dict):
        raise ValueError("模型返回的 JSON 不是对象")
    return value or None
