"""LangChain ChatModel 工厂：复用 Lumi 的动态模型配置与 BYOK 边界。"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, convert_to_messages
from langchain_openai import ChatOpenAI
from loguru import logger

from app.core.config import settings
from app.core.llm_config import get_llm_config


class CompatibleChatOpenAI(ChatOpenAI):
    """Preserve non-standard reasoning payloads used by compatible providers.

    Some DeepSeek/Qwen-compatible thinking APIs return ``reasoning_content``
    alongside an assistant tool call and require it on the next request after
    the tool result.  LangChain's OpenAI converter intentionally drops unknown
    fields, so a normal ``ChatOpenAI`` round-trip produces a provider 400.
    Keep that opaque field only when the provider returned it, then replay it
    on the corresponding assistant message.  It is never shown to users.
    """

    def _create_chat_result(self, response: Any, generation_info: dict | None = None):
        result = super()._create_chat_result(response, generation_info)
        try:
            response_dict = (
                response
                if isinstance(response, dict)
                else response.model_dump(warnings=False)
            )
            choices = list(response_dict.get("choices") or [])
            for generation, choice in zip(result.generations, choices, strict=False):
                raw_message = choice.get("message") if isinstance(choice, dict) else None
                reasoning = (
                    raw_message.get("reasoning_content")
                    if isinstance(raw_message, dict) else None
                )
                if reasoning is not None and isinstance(generation.message, AIMessage):
                    generation.message.additional_kwargs["reasoning_content"] = reasoning
        except Exception:  # noqa: BLE001
            # Provider compatibility metadata must never make an otherwise
            # valid response fail.  Without it, the next call simply follows
            # normal OpenAI-compatible behavior.
            pass
        return result

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        try:
            messages = convert_to_messages(input_)
            request_messages = payload.get("messages") or []
            for message, request_message in zip(messages, request_messages, strict=False):
                if not isinstance(message, AIMessage) or not isinstance(request_message, dict):
                    continue
                reasoning = message.additional_kwargs.get("reasoning_content")
                if reasoning is not None and request_message.get("role") == "assistant":
                    request_message["reasoning_content"] = reasoning
        except Exception:  # noqa: BLE001
            pass
        return payload


def _supports_reasoning_effort(model: str | None) -> bool:
    """只向管理员明确验证的模型透传推理强度。

    模型名称本身不能证明某个兼容端点支持扩展字段。默认不发送，避免把
    有效请求变为 HTTP 400；需要时由 ``LLM_REASONING_EFFORT_MODELS`` 白名单开启。
    """
    enabled = {
        item.strip().casefold()
        for item in str(settings.LLM_REASONING_EFFORT_MODELS or "").split(",")
        if item.strip()
    }
    return str(model or "").strip().casefold() in enabled


async def get_chat_model(
    *,
    scene: str | None,
    user_id: str | None,
    api_key: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    reasoning_effort: str | None = None,
    llm_config: dict[str, Any] | None = None,
) -> CompatibleChatOpenAI:
    """每次创建短生命周期模型，保证 Redis 动态配置即时生效。"""
    cfg = dict(llm_config or await get_llm_config(scene, user_id=user_id))
    from app.core.model_catalog import normalize_model_id

    selected_model = normalize_model_id(model or cfg.get("model"))
    options = {}
    if reasoning_effort and _supports_reasoning_effort(selected_model):
        options["reasoning_effort"] = reasoning_effort
    selected_base_url = (base_url or cfg.get("base_url") or "").rstrip("/")
    if scene == "office":
        # 仅记录定位兼容接口问题所需的非敏感信息；不能记录 API key、完整
        # URL path 或用户输入。该行同时可作为容器是否已加载当前镜像的探针。
        logger.info(
            "办公模型请求配置: model={} endpoint_host={} reasoning_effort_forwarded={}",
            selected_model,
            urlparse(selected_base_url).netloc or "(empty)",
            "reasoning_effort" in options,
        )
    return CompatibleChatOpenAI(
        model=selected_model,
        base_url=selected_base_url,
        api_key=api_key or cfg.get("api_key") or "",
        timeout=float(timeout or cfg.get("timeout") or 120),
        temperature=temperature,
        max_completion_tokens=max_tokens,
        stream_usage=True,
        max_retries=0,  # Lumi 恢复层统一决定重试/备用供应商，避免重复重放工具。
        **options,
    )
