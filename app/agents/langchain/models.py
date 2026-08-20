"""LangChain ChatModel 工厂：复用 Lumi 的动态模型配置与 BYOK 边界。"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.core.llm_config import get_llm_config


def _supports_reasoning_effort(model: str | None) -> bool:
    """只向明确支持该字段的模型传递推理强度。

    DashScope 的 qwen-turbo、Ollama 及多数 OpenAI-compatible 网关会把未知
    ``reasoning_effort`` 当作 400 请求；DeepSeek V4 的 BYOK 网关才保留此项。
    """
    return str(model or "").lower().startswith("deepseek-v4-flash")


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
) -> ChatOpenAI:
    """每次创建短生命周期模型，保证 Redis 动态配置即时生效。"""
    cfg = await get_llm_config(scene, user_id=user_id)
    selected_model = model or cfg.get("model") or ""
    options = {}
    if reasoning_effort and _supports_reasoning_effort(selected_model):
        options["reasoning_effort"] = reasoning_effort
    return ChatOpenAI(
        model=selected_model,
        base_url=(base_url or cfg.get("base_url") or "").rstrip("/"),
        api_key=api_key or cfg.get("api_key") or "",
        timeout=float(timeout or cfg.get("timeout") or 120),
        temperature=temperature,
        max_completion_tokens=max_tokens,
        stream_usage=True,
        max_retries=0,  # Lumi 恢复层统一决定重试/备用供应商，避免重复重放工具。
        **options,
    )
