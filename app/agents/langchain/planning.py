"""规划器的 LangChain 结构化输出契约。"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage
from loguru import logger
from pydantic import BaseModel, Field

from app.agents.langchain.models import get_chat_model
from app.core.config import settings
from app.services.usage import CATEGORY_PLAN


def _parse_json_object_text(text: str) -> dict[str, Any]:
    """从模型文本里取出**单个 JSON 对象**（拒绝自由文本）。"""
    raw = str(text or "").strip()
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


class PlannerOutput(BaseModel):
    plan: str = ""
    # Generalized office contract: business-neutral capability profile and
    # logical steps. Concrete Skills/Tools are bound after planning.
    task_profile: dict[str, Any] = Field(default_factory=dict)
    abstract_tasks: list[dict[str, Any]] = Field(default_factory=list)
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
    llm_config: dict[str, Any] | None = None,
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
        llm_config=llm_config,
    )
    reply = await model.ainvoke([HumanMessage(content=prompt)])
    return _parse_json_planner_output(_message_text(reply))


async def invoke_json_object(
    prompt: str,
    *,
    user_id: str,
    api_key: str | None = None,
    llm_config: dict[str, Any] | None = None,
    max_tokens: int = 2000,
    role: str | None = None,
) -> dict[str, Any] | None:
    """无固定 Schema 的 JSON 对象，走兼容的普通聊天调用。

    ``role`` 让调用方指定模型档位（例如意图评估 → ``intent_assessor``）。
    结构化结果不合法时由调用方决定回退（意图评估会换主档位重试一次，仍失败则用
    确定性启发式画像）——**低成本模型只负责"理解意图"，不负责安全放行**。
    """
    from app.core import model_roles
    from app.core.llm import LLMClient

    role_name = str(role or "").strip()
    client = LLMClient()

    async def _ask(ask_role: str | None) -> dict[str, Any]:
        if ask_role:
            # 角色档位走统一 LLMClient（含能力校验、遥测与角色回退）。
            text = await client.chat(
                [{"role": "user", "content": prompt}],
                scene="office",
                role=ask_role,
                api_key=api_key,
                timeout=settings.AGENT_PLANNER_TIMEOUT_SECONDS,
                llm_config=llm_config,
                usage_user_id=user_id,
                usage_category=CATEGORY_PLAN,
                temperature=0.1,
                max_tokens=max_tokens,
            )
            return _parse_json_object_text(text)
        model = await get_chat_model(
            scene="office",
            user_id=user_id,
            api_key=api_key,
            temperature=0.1,
            max_tokens=max_tokens,
            timeout=settings.AGENT_PLANNER_TIMEOUT_SECONDS,
            llm_config=llm_config,
        )
        reply = await model.ainvoke([HumanMessage(content=prompt)])
        return _parse_json_object_text(_message_text(reply))

    try:
        return await _ask(role_name or None)
    except Exception as exc:  # noqa: BLE001
        # 低成本角色失败/结构化输出不合法 → 按角色回退策略换 main 重试一次。
        if not role_name or model_roles.role_fallback_policy(role_name) != "main":
            raise
        logger.warning(
            "[model-role] {} 结构化输出失败（{}），按回退策略换 main 重试", role_name, str(exc)[:120]
        )
        return await _ask(model_roles.ROLE_DIRECT_ANSWER)
