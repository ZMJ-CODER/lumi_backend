"""LLM 客户端封装 —— 配置动态化（Redis 优先，.env 兜底）.

配置来源优先级:
  1. 调用时显式传入的 model
  2. Redis 动态配置（场景级 → 全局默认，进程内缓存 5 秒）
  3. .env 兜底配置

每次调用按当前配置创建短连接，避免配置更新后旧连接仍携带旧 base_url/api_key。
"""

import json

import httpx
from loguru import logger
from httpx import AsyncClient

from app.core.config import settings
from app.core.llm_config import get_llm_config
from app.services.usage import estimate_tokens, record_usage


class LLMClient:
    """异步 LLM 客户端，配置每次调用时动态读取."""

    def __init__(self, provider: str | None = None) -> None:
        # provider 仅用于 .env 兜底时的选择（兼容旧代码），实际配置以运行时为准
        self.provider = provider or settings.LLM_PROVIDER
        self._client: AsyncClient | None = None

    def _fallback_cfg(self) -> dict | None:
        """备用供应商配置（LLM_FALLBACK_PROVIDER；未配置或与主供应商相同返回 None）."""
        provider = (settings.LLM_FALLBACK_PROVIDER or "").strip().lower()
        if not provider or provider == str(self.provider or "").lower():
            return None
        if provider == "deepseek":
            return {
                "base_url": settings.DEEPSEEK_BASE_URL,
                "api_key": settings.DEEPSEEK_API_KEY,
                "model": settings.DEEPSEEK_MODEL,
            }
        if provider == "qwen":
            return {
                "base_url": settings.QWEN_BASE_URL,
                "api_key": settings.QWEN_API_KEY,
                "model": settings.QWEN_MODEL,
            }
        return None

    @staticmethod
    def _is_retryable_error(exc: Exception) -> bool:
        """可降级错误：连接/超时/5xx/空内容（4xx 参数类错误不降级）. """
        if isinstance(exc, RuntimeError) and "空内容" in str(exc):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500 or exc.response.status_code in (401, 429)
        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
        return False

    @staticmethod
    def _has_tool_messages(messages: list[dict]) -> bool:
        """消息里是否含工具调用（role=tool 或 assistant.tool_calls）."""
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "tool" or m.get("tool_calls"):
                return True
        return False

    async def start(self) -> None:
        """兼容保留：配置已改为每次调用动态获取，无需预建连接."""

    async def close(self) -> None:
        """兼容保留."""
        if self._client:
            await self._client.aclose()
            self._client = None

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
        **kwargs,
    ) -> str:
        """发送对话请求，返回模型响应文本."""
        if base_url:
            # 显式指定端点（如本地 Ollama VL 描述 / 语音通话专用模型）：跳过动态配置
            cfg = {
                "base_url": base_url.rstrip("/"),
                "api_key": api_key or "",
                "model": model or "",
                "timeout": float(timeout or 180.0),
            }
        else:
            cfg = await get_llm_config(scene, self.provider, user_id=usage_user_id)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = api_key or cfg.get("api_key") or ""
        model_name = model or cfg.get("model") or settings.DEEPSEEK_MODEL
        timeout = float(cfg.get("timeout") or 120.0)

        effort = reasoning_effort or cfg.get("reasoning_effort")
        if self._has_tool_messages(messages):
            # DeepSeek 思考模式要求带工具调用的 assistant 消息回传 reasoning_content；
            # 工具调用来自其他供应商（如 Qwen 决策），没有该字段 → 关闭思考参数避免 400
            effort = None

        async def _call(c_base: str, c_key: str, c_model: str) -> tuple[dict, str]:
            payload = {"model": c_model, "messages": messages, **kwargs}
            if effort and not disable_reasoning_effort:
                payload["reasoning_effort"] = effort
            async with AsyncClient(
                base_url=c_base,
                headers={"Authorization": f"Bearer {c_key}"},
                timeout=timeout,
            ) as client:
                resp = await client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            if content is None or not str(content or "").strip():
                # 推理强度过高时模型可能把输出预算全花在 reasoning 上，content 为空；
                # 抛异常让调用方重试（禁用推理强度）或回退，避免静默"生成失败"
                raise RuntimeError("模型返回空内容（可能推理强度过高耗尽输出预算）")
            return data, content

        try:
            data, content = await _call(base_url, api_key, model_name)
            used_model = model_name
        except Exception as exc:
            fcfg = self._fallback_cfg()
            if fcfg and self._is_retryable_error(exc):
                logger.warning(
                    "LLM 主供应商调用失败，切换 {} 重试: {}",
                    fcfg["model"],
                    str(exc)[:120],
                )
                data, content = await _call(fcfg["base_url"], fcfg["api_key"], fcfg["model"])
                used_model = fcfg["model"]
            else:
                raise
        usage = data.get("usage") or {}
        await record_usage(
            usage_user_id,
            usage_category or "chat",
            used_model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return content

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
        **kwargs,
    ) -> tuple[str, list[dict]]:
        """非流式调用并允许工具调用（tool_choice=auto）。返回 (content, tool_calls)."""
        if base_url:
            cfg = {
                "base_url": base_url.rstrip("/"),
                "api_key": api_key or "",
                "model": model or "",
                "timeout": float(timeout or 180.0),
            }
        else:
            cfg = await get_llm_config(scene, self.provider, user_id=usage_user_id)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = api_key or cfg.get("api_key") or ""
        model_name = model or cfg.get("model") or settings.DEEPSEEK_MODEL
        timeout = float(cfg.get("timeout") or 120.0)

        effort = reasoning_effort or cfg.get("reasoning_effort")
        if self._has_tool_messages(messages):
            effort = None

        async def _call(c_base: str, c_key: str, c_model: str) -> tuple[dict, dict]:
            payload = {
                "model": c_model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "auto",
                **kwargs,
            }
            if effort:
                payload["reasoning_effort"] = effort
            async with AsyncClient(
                base_url=c_base,
                headers={"Authorization": f"Bearer {c_key}"},
                timeout=timeout,
            ) as client:
                resp = await client.post("/chat/completions", json=payload)
                resp.raise_for_status()
                data = resp.json()
                return data, data["choices"][0]["message"]

        try:
            data, msg = await _call(base_url, api_key, model_name)
            used_model = model_name
        except Exception as exc:
            fcfg = self._fallback_cfg()
            if fcfg and self._is_retryable_error(exc):
                logger.warning(
                    "LLM 工具调用主供应商失败，切换 {} 重试: {}",
                    fcfg["model"],
                    str(exc)[:120],
                )
                data, msg = await _call(fcfg["base_url"], fcfg["api_key"], fcfg["model"])
                used_model = fcfg["model"]
            else:
                raise
        usage = data.get("usage") or {}
        await record_usage(
            usage_user_id,
            usage_category or "chat",
            used_model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return (msg.get("content") or ""), (msg.get("tool_calls") or [])

    async def chat_with_tools_qwen(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        model: str | None = None,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        **kwargs,
    ) -> tuple[str, list[dict]]:
        """用千问（DashScope）配置调用带工具接口（工具决策专用）.

        工具决策独立于场景配置：部分场景模型（如 qwen-vl-plus）不支持 tools，
        统一用 qwen-plus 做工具决策，避免受场景 provider 影响。
        """
        base_url = settings.QWEN_BASE_URL.rstrip("/")
        api_key = settings.QWEN_API_KEY
        model_name = model or settings.QWEN_MODEL
        payload = {
            "model": model_name,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            **kwargs,
        }
        async with AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        ) as client:
            resp = await client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        msg = data["choices"][0]["message"]
        usage = data.get("usage") or {}
        await record_usage(
            usage_user_id,
            usage_category or "chat",
            model_name,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
        return (msg.get("content") or ""), (msg.get("tool_calls") or [])

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
        **kwargs,
    ):
        """流式调用（SSE）。返回异步生成器，逐段产出文本增量."""
        if base_url:
            cfg = {
                "base_url": base_url.rstrip("/"),
                "api_key": api_key or "",
                "model": model or "",
                "timeout": float(timeout or 180.0),
            }
        else:
            cfg = await get_llm_config(scene, self.provider, user_id=usage_user_id)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = api_key or cfg.get("api_key") or ""
        model_name = model or cfg.get("model") or settings.DEEPSEEK_MODEL
        timeout = float(cfg.get("timeout") or 120.0)

        effort = reasoning_effort or cfg.get("reasoning_effort")
        if self._has_tool_messages(messages):
            effort = None

        async def _stream_once(
            c_base: str, c_key: str, c_model: str
        ) -> tuple[list[str], int | None, int | None]:
            """单次流式请求：返回 (文本增量列表, prompt_tokens, completion_tokens)."""
            payload = {
                "model": c_model,
                "messages": messages,
                "stream": True,
                "stream_options": {"include_usage": True},
                **kwargs,
            }
            if effort and not disable_reasoning_effort:
                payload["reasoning_effort"] = effort
            chunks: list[str] = []
            prompt_tokens = completion_tokens = None
            async with AsyncClient(
                base_url=c_base,
                headers={"Authorization": f"Bearer {c_key}"},
                timeout=timeout,
            ) as client:
                async with client.stream("POST", "/chat/completions", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        chunk = line[len("data:"):].strip()
                        if not chunk or chunk == "[DONE]":
                            continue
                        try:
                            data = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        choices = data.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            text = delta.get("content")
                            if text:
                                chunks.append(text)
                        elif data.get("usage"):
                            usage = data.get("usage") or {}
                            prompt_tokens = usage.get("prompt_tokens")
                            completion_tokens = usage.get("completion_tokens")
            return chunks, prompt_tokens, completion_tokens

        used_model = model_name
        streamed_text = ""
        prompt_tokens = completion_tokens = None
        try:
            chunks, pt, ct = await _stream_once(base_url, api_key, model_name)
            for text in chunks:
                streamed_text += text
                yield text
            prompt_tokens, completion_tokens = pt, ct
        except Exception as exc:
            fcfg = self._fallback_cfg()
            if not (fcfg and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 流式主供应商失败，切换 {} 重试: {}", fcfg["model"], str(exc)[:120])
            used_model = fcfg["model"]
            chunks, pt, ct = await _stream_once(fcfg["base_url"], fcfg["api_key"], fcfg["model"])
            for text in chunks:
                streamed_text += text
                yield text
            prompt_tokens, completion_tokens = pt, ct
        # 记录用量：优先取流式返回的 usage，缺失时按文本粗略估算
        if prompt_tokens is None:
            prompt_tokens = sum(estimate_tokens(str(m.get("content") or "")) for m in messages)
        if completion_tokens is None:
            completion_tokens = estimate_tokens(streamed_text)
        await record_usage(
            usage_user_id,
            usage_category or "chat",
            used_model,
            prompt_tokens,
            completion_tokens,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        scene: str | None = None,
        model: str | None = None,
    ) -> list[list[float]]:
        """生成文本嵌入向量（模型名默认取 settings.EMBEDDING_MODEL）."""
        cfg = await get_llm_config(scene, self.provider)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = cfg.get("api_key") or ""
        model_name = model or settings.EMBEDDING_MODEL
        timeout = float(cfg.get("timeout") or 120.0)

        payload = {"model": model_name, "input": texts}
        async with AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        ) as client:
            resp = await client.post("/embeddings", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return [item["embedding"] for item in data["data"]]


# 全局单例（默认 Provider）
llm_client = LLMClient()
