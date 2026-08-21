"""统一 LLM 门面：既有调用契约 + LangChain ChatModel 运行时。

业务代码可继续调用 ``LLMClient.chat/chat_stream/chat_with_tools``，但所有
文本模型请求都通过 LangChain ``ChatOpenAI`` 执行。Embedding 属于独立 API，
暂保留 OpenAI-compatible HTTP 调用，避免把 ChatModel 与向量接口混为一层。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import time
from typing import Any

import httpx
from httpx import AsyncClient
from langchain_core.messages import AIMessage, BaseMessage, convert_to_messages
from loguru import logger

from app.agents.langchain.models import get_chat_model
from app.core.config import settings
from app.core.llm_config import get_llm_config
from app.core.resilience import get_breaker, is_transient_dependency_error
from app.services.usage import estimate_tokens, record_usage


class LLMClient:
    """兼容门面；所有 Chat API 统一转发给 LangChain。"""

    def __init__(self, provider: str | None = None) -> None:
        self.provider = provider or settings.LLM_PROVIDER
        self._client: AsyncClient | None = None

    def _fallback_cfg(self) -> dict | None:
        provider = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
        if not provider or provider == str(self.provider or "").lower():
            return None
        if provider == "deepseek":
            return {"base_url": settings.DEEPSEEK_BASE_URL, "api_key": settings.DEEPSEEK_API_KEY, "model": settings.DEEPSEEK_MODEL}
        if provider == "qwen":
            return {"base_url": settings.QWEN_BASE_URL, "api_key": settings.QWEN_API_KEY, "model": settings.QWEN_MODEL}
        return None

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        if isinstance(exc, RuntimeError) and "空内容" in str(exc):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500 or exc.response.status_code in (401, 429)
        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
        name, text = type(exc).__name__.lower(), str(exc).lower()
        return is_transient_dependency_error(exc) or any(
            token in name or token in text
            for token in ("timeout", "connection", "rate", "servererror", "503")
        )

    @staticmethod
    def _has_tool_messages(messages: list[dict]) -> bool:
        return any(isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls")) for m in messages or [])

    async def start(self) -> None:
        """兼容保留：短生命周期 LangChain 模型无需显式启动。"""

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _model(
        self,
        *,
        scene: str | None,
        user_id: str | None,
        api_key: str | None,
        model: str | None,
        base_url: str | None,
        timeout: float | None,
        temperature: float | None,
        max_tokens: int | None,
        reasoning_effort: str | None,
        disable_reasoning_effort: bool,
        messages: list[dict],
    ):
        cfg = await get_llm_config(scene, self.provider, user_id=user_id)
        selected_base_url = (base_url or cfg.get("base_url") or "").rstrip("/")
        selected_api_key = api_key or cfg.get("api_key") or ""
        selected_model = model or cfg.get("model") or settings.DEEPSEEK_MODEL
        selected_timeout = float(timeout or cfg.get("timeout") or 120.0)
        effort = None if (disable_reasoning_effort or self._has_tool_messages(messages)) else (reasoning_effort or cfg.get("reasoning_effort"))
        return await get_chat_model(
            scene=scene,
            user_id=user_id,
            api_key=selected_api_key,
            model=selected_model,
            base_url=selected_base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=selected_timeout,
            reasoning_effort=effort,
        ), selected_model, selected_base_url

    @staticmethod
    def _message_text(reply: BaseMessage) -> str:
        content = reply.content
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
        return str(content or "")

    @staticmethod
    def _usage(reply: BaseMessage) -> tuple[int | None, int | None]:
        usage = getattr(reply, "usage_metadata", None) or {}
        return usage.get("input_tokens") or usage.get("prompt_tokens"), usage.get("output_tokens") or usage.get("completion_tokens")

    async def _record(self, reply: BaseMessage, *, messages: list[dict], user_id: str | None, category: str | None, model: str, text: str) -> None:
        prompt_tokens, completion_tokens = self._usage(reply)
        await record_usage(
            user_id,
            category or "chat",
            model,
            prompt_tokens if prompt_tokens is not None else sum(estimate_tokens(str(m.get("content") or "")) for m in messages),
            completion_tokens if completion_tokens is not None else estimate_tokens(text),
        )

    async def chat(
        self,
        messages: list[dict],
        *,
        scene: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        disable_reasoning_effort: bool = False,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        **kwargs: Any,
    ) -> str:
        """LangChain 非流式对话；保留动态配置、BYOK 与备用供应商策略。"""
        temperature, max_tokens = kwargs.pop("temperature", None), kwargs.pop("max_tokens", None)
        if kwargs:
            logger.debug("忽略 ChatModel 不支持的 legacy 参数: {}", sorted(kwargs))

        async def invoke(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            chat_model, used_model, used_base_url = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=disable_reasoning_effort, messages=messages,
            )
            breaker = get_breaker(f"llm:{used_base_url}:{used_model}")
            reply = await breaker.call(lambda: chat_model.ainvoke(convert_to_messages(messages)))
            text = self._message_text(reply)
            if not text.strip():
                raise RuntimeError("模型返回空内容")
            return reply, text, used_model

        try:
            reply, text, used_model = await invoke(base_url, api_key, model)
        except Exception as exc:
            fallback = self._fallback_cfg()
            if not (fallback and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 主供应商调用失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
            reply, text, used_model = await invoke(fallback["base_url"], fallback["api_key"], fallback["model"])
        await self._record(reply, messages=messages, user_id=usage_user_id, category=usage_category, model=used_model, text=text)
        return text

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        scene: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, list[dict]]:
        """LangChain 工具绑定，返回原有 OpenAI tool-call 字典形状。"""
        if kwargs:
            logger.debug("忽略 ChatModel 工具调用的 legacy 参数: {}", sorted(kwargs))

        async def invoke(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            chat_model, used_model, used_base_url = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=None, max_tokens=None, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=False, messages=messages,
            )
            breaker = get_breaker(f"llm:{used_base_url}:{used_model}")
            bound = chat_model.bind_tools(tools, parallel_tool_calls=False)
            reply: AIMessage = await breaker.call(lambda: bound.ainvoke(convert_to_messages(messages)))
            calls = [
                {"id": str(call.get("id") or ""), "type": "function", "function": {"name": str(call.get("name") or ""), "arguments": call.get("args") or {}}}
                for call in (reply.tool_calls or [])
            ]
            return reply, self._message_text(reply), calls, used_model

        try:
            reply, content, tool_calls, used_model = await invoke(base_url, api_key, model)
        except Exception as exc:
            fallback = self._fallback_cfg()
            if not (fallback and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 工具调用主供应商失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
            reply, content, tool_calls, used_model = await invoke(fallback["base_url"], fallback["api_key"], fallback["model"])
        await self._record(reply, messages=messages, user_id=usage_user_id, category=usage_category, model=used_model, text=content)
        return content, tool_calls

    async def chat_with_tools_qwen(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        model: str | None = None,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        **kwargs: Any,
    ) -> tuple[str, list[dict]]:
        """轻量联网决策的固定 Qwen 配置适配。

        仅供历史 ``_maybe_decide_web`` 兼容入口使用；通用技能循环已统一
        由 LangGraph ToolNode 承担。
        """
        return await self.chat_with_tools(
            messages,
            tools,
            scene="chat",
            model=model or settings.QWEN_MODEL,
            api_key=settings.QWEN_API_KEY,
            base_url=settings.QWEN_BASE_URL,
            timeout=30,
            usage_user_id=usage_user_id,
            usage_category=usage_category,
            **kwargs,
        )

    async def chat_stream(
        self,
        messages: list[dict],
        *,
        scene: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        disable_reasoning_effort: bool = False,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """LangChain ``astream``；首个输出前失败才允许切备用供应商。"""
        if kwargs:
            logger.debug("忽略 ChatModel 流式调用的 legacy 参数: {}", sorted(kwargs))

        usage: tuple[int | None, int | None] = (None, None)
        started_at = time.perf_counter()
        first_token_at: float | None = None

        async def stream_once(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            nonlocal usage
            chat_model, used_model, used_base_url = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=disable_reasoning_effort, messages=messages,
            )
            breaker = get_breaker(f"llm:{used_base_url}:{used_model}")
            await breaker.before_call()
            try:
                async for chunk in chat_model.astream(convert_to_messages(messages)):
                    chunk_usage = self._usage(chunk)
                    if chunk_usage != (None, None):
                        usage = chunk_usage
                    delta = self._message_text(chunk)
                    if delta:
                        yield delta
            except Exception as exc:
                await breaker.record_failure(exc)
                raise
            else:
                await breaker.record_success()

        runtime_cfg = await get_llm_config(scene, self.provider, user_id=usage_user_id)
        text, used_model, emitted = "", model or runtime_cfg.get("model") or settings.DEEPSEEK_MODEL, False
        try:
            async for delta in stream_once(base_url, api_key, model):
                emitted = True
                text += delta
                if first_token_at is None:
                    first_token_at = time.perf_counter()
                    logger.info(
                        "LLM 流首 token: scene={} model={} ttft_ms={}",
                        scene or "default",
                        used_model,
                        round((first_token_at - started_at) * 1000, 1),
                    )
                yield delta
        except Exception as exc:
            fallback = self._fallback_cfg()
            if emitted or not (fallback and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 流式主供应商失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
            used_model = fallback["model"]
            async for delta in stream_once(fallback["base_url"], fallback["api_key"], fallback["model"]):
                text += delta
                yield delta
        await record_usage(
            usage_user_id,
            usage_category or "chat",
            used_model or settings.DEEPSEEK_MODEL,
            usage[0] if usage[0] is not None else sum(estimate_tokens(str(message.get("content") or "")) for message in messages),
            usage[1] if usage[1] is not None else estimate_tokens(text),
        )
        logger.info(
            "LLM 流完成: scene={} model={} duration_ms={} output_chars={}",
            scene or "default",
            used_model,
            round((time.perf_counter() - started_at) * 1000, 1),
            len(text),
        )

    async def embed(self, texts: list[str], *, scene: str | None = None, model: str | None = None) -> list[list[float]]:
        """Embedding 专用 OpenAI-compatible 调用（不属于 ChatModel 迁移范围）。"""
        cfg = await get_llm_config(scene, self.provider)
        async with AsyncClient(
            base_url=(cfg.get("base_url") or "").rstrip("/"), headers={"Authorization": f"Bearer {cfg.get('api_key') or ''}"},
            timeout=float(cfg.get("timeout") or 120.0),
        ) as client:
            response = await client.post("/embeddings", json={"model": model or settings.EMBEDDING_MODEL, "input": texts})
            response.raise_for_status()
            return [item["embedding"] for item in response.json()["data"]]
