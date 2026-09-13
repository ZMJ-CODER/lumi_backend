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
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, convert_to_messages
from loguru import logger

from app.agents.langchain.models import get_chat_model
from app.core.config import settings
from app.core.llm_config import get_llm_config
from app.core.model_catalog import normalize_provider_base_url
from app.core.network import resolve_http_proxy
from app.core.resilience import get_breaker, is_transient_dependency_error
from app.services.usage import estimate_tokens, record_usage
from app.core.model_response import normalize_tool_response


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
            # 402 is provider/account specific (for example DeepSeek
            # ``Insufficient Balance``).  It is safe to try a configured
            # *different* provider for ordinary chat; office/BYOK callers are
            # still blocked by the scene/config guards below.
            return exc.response.status_code >= 500 or exc.response.status_code in (401, 402, 429)
        if isinstance(exc, (httpx.TransportError, httpx.TimeoutException)):
            return True
        name, text = type(exc).__name__.lower(), str(exc).lower()
        return is_transient_dependency_error(exc) or any(
            token in name or token in text
            for token in ("timeout", "connection", "rate", "servererror", "503", "402", "insufficient balance")
        )

    @staticmethod
    def _has_tool_messages(messages: list[dict]) -> bool:
        return any(isinstance(m, dict) and (m.get("role") == "tool" or m.get("tool_calls")) for m in messages or [])

    @staticmethod
    def _tool_fallback_messages(messages: list[dict], tools: list[dict]) -> list[BaseMessage]:
        names = []
        for item in tools or []:
            fn = item.get("function") if isinstance(item, dict) else {}
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
        contract = (
            "当前模型不支持原生 Function Calling。请严格只输出一个 JSON 对象："
            '{"name":"工具名","arguments":{}} 表示调用一个工具；或 '
            '{"answer":"最终回答"} 表示无需工具直接回答。可用工具：'
            + ", ".join(names)
            + "。不要输出 Markdown、解释或其他文本。"
        )
        return [SystemMessage(content=contract), *convert_to_messages(messages)]

    @staticmethod
    def _is_tool_capability_error(exc: Exception) -> bool:
        text = str(exc).casefold()
        return "does not support tools" in text or "tool calling" in text or "bind_tools" in text

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
        llm_config: dict[str, Any] | None = None,
        role: str | None = None,
        require_tools: bool = False,
    ):
        """解析一次调用用的模型。

        优先级：显式 ``llm_config``（BYOK / Job 冻结）→ 显式 model/base_url →
        **角色档位**（``role=``，见 ``app.core.model_roles``）→ 既有 scene 链。

        ``require_tools=True`` 时，若角色档位声明"不支持工具调用"，则按角色回退
        策略升级到 ``main``（例如写入类决策不能由低成本模型在没有工具能力时继续）。
        """
        role_name = str(role or "").strip()
        role_info = None
        if role_name and llm_config is None and not (model or base_url):
            from app.core import model_roles

            role_info = await model_roles.resolve_role(
                role_name, scene=scene, user_id=user_id, request_api_key=api_key
            )
            if require_tools and not role_info.supports_tools:
                policy = model_roles.role_fallback_policy(role_name)
                upgraded = await model_roles.resolve_role(
                    role_name, scene=scene, user_id=user_id, request_api_key=None
                )
                from app.core.model_roles import PROFILE_MAIN

                if policy == "main" and upgraded.profile != PROFILE_MAIN:
                    main_cfg = await model_roles.resolve_role(
                        model_roles.ROLE_DIRECT_ANSWER, scene=scene, user_id=user_id
                    )
                    logger.warning(
                        "[model-role] {} 档位不支持工具调用，按回退策略升级到 main（{} → {}）",
                        role_name, role_info.model, main_cfg.model,
                    )
                    role_info = main_cfg
                else:
                    logger.warning(
                        "[model-role] {} 使用的档位声明不支持工具调用（model={}）；"
                        "如需工具能力请在后台把该角色指向 main 档位",
                        role_name, role_info.model,
                    )
            cfg = dict(role_info.api_dict())
        else:
            cfg = dict(llm_config or await get_llm_config(scene, self.provider, user_id=user_id))
            # 角色给"默认模型"，显式参数仍可覆盖（调用方知道自己在做什么）。
            if role_name and (model or base_url) and llm_config is None:
                from app.core import model_roles

                fallback_role = await model_roles.resolve_role(
                    role_name, scene=scene, user_id=user_id, request_api_key=api_key
                )
                cfg.setdefault("model", fallback_role.model)
                cfg.setdefault("base_url", fallback_role.base_url)
                cfg.setdefault("api_key", fallback_role.api_key)
        # 所有入口（.env、Redis 动态配置、用户 BYOK、角色档位）在真正创建客户端前走
        # 同一套地址规范化。否则历史 DeepSeek ``/v1`` 配置会在断路器、日志
        # 与实际请求之间产生不一致，也不利于定位连接问题。
        selected_base_url = normalize_provider_base_url(base_url or cfg.get("base_url") or "")
        selected_api_key = api_key or cfg.get("api_key") or ""
        selected_model = model or cfg.get("model") or settings.DEEPSEEK_MODEL
        # 超时的**唯一**收口点，顺序必须是：
        #   模型默认值 → 运行时策略覆盖 → 与当前剩余 Deadline 取最小值
        # 不能反过来：先取 deadline 再套策略会让一个**更大的**策略值重新放宽超时，
        # 于是"请求只剩 3 秒"却给了模型 120 秒——deadline 形同虚设。
        selected_timeout = float(timeout or cfg.get("timeout") or 120.0)
        try:
            from app.services.runtime_policy import policy_store, runtime_policy_enabled

            provider_id = str(getattr(role_info, "provider", "") or cfg.get("provider") or "")
            if provider_id or selected_model:
                if not policy_store.enabled(provider_id=provider_id, model=selected_model):
                    raise RuntimeError(
                        f"模型 {selected_model} 已被运行时策略停用（运维面板 policies）"
                    )
                if runtime_policy_enabled():
                    selected_timeout = policy_store.timeout_seconds(
                        provider_id=provider_id, model=selected_model, fallback=selected_timeout
                    )
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - 策略读取失败绝不能拦下模型调用
            logger.debug("运行时策略读取失败（沿用默认超时/启停）: {}", str(exc)[:120])
        # 最后才与剩余预算取小：`ainvoke/astream` 外面没有 asyncio.wait_for，
        # SDK 的 timeout= 就是唯一边界，必须把剩余预算压进来。
        from app.core.deadline import ensure_budget, request_budget_seconds

        ensure_budget(what=f"llm:{selected_model}")
        selected_timeout = request_budget_seconds(cap=selected_timeout)
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
            llm_config=cfg,
        ), selected_model, selected_base_url, role_info

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

    async def _record(
        self,
        reply: BaseMessage,
        *,
        messages: list[dict],
        user_id: str | None,
        category: str | None,
        model: str,
        text: str,
        role: str | None = None,
        role_info: Any = None,
        fallback_used: bool = False,
        duration_ms: int = 0,
        structured_ok: bool | None = None,
    ) -> None:
        prompt_tokens, completion_tokens = self._usage(reply)
        await record_usage(
            user_id,
            category or "chat",
            model,
            prompt_tokens if prompt_tokens is not None else sum(estimate_tokens(str(m.get("content") or "")) for m in messages),
            completion_tokens if completion_tokens is not None else estimate_tokens(text),
            model_role=str(role or "") or None,
            model_profile=str(getattr(role_info, "profile", "") or "") or None,
            config_source=str(getattr(role_info, "source", "") or "") or None,
            fallback_used=bool(fallback_used),
            duration_ms=int(duration_ms or 0),
            structured_ok=structured_ok,
        )

    async def chat(
        self,
        messages: list[dict],
        *,
        scene: str | None = None,
        role: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        disable_reasoning_effort: bool = False,
        llm_config: dict[str, Any] | None = None,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        **kwargs: Any,
    ) -> str:
        """LangChain 非流式对话；保留动态配置、BYOK、角色档位与备用供应商策略。"""
        temperature, max_tokens = kwargs.pop("temperature", None), kwargs.pop("max_tokens", None)
        if kwargs:
            logger.debug("忽略 ChatModel 不支持的 legacy 参数: {}", sorted(kwargs))

        async def invoke(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            chat_model, used_model, used_base_url, role_info = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=disable_reasoning_effort, messages=messages,
                llm_config=llm_config, role=role,
            )
            breaker = get_breaker(f"llm:{used_base_url}:{used_model}")
            reply = await breaker.call(lambda: chat_model.ainvoke(convert_to_messages(messages)))
            text = self._message_text(reply)
            if not text.strip():
                raise RuntimeError("模型返回空内容")
            return reply, text, used_model, role_info

        try:
            reply, text, used_model, role_info = await invoke(base_url, api_key, model)
        except Exception as exc:
            # 角色回退策略（app/core/model_roles.ROLE_FALLBACK）：低成本中间角色失败时
            # 按角色决定"换 main 重试"还是"直接失败交给确定性兜底"。
            upgraded = await self._role_fallback_target(role, exc, scene=scene, user_id=usage_user_id)
            if upgraded is not None:
                logger.warning(
                    "[model-role] {} 调用失败，按回退策略切换 {} 重试: {}",
                    role, upgraded["model"], str(exc)[:120],
                )
                reply, text, used_model, role_info = await invoke(
                    upgraded.get("base_url"), upgraded.get("api_key"), upgraded.get("model")
                )
                await self._record(
                    reply, messages=messages, user_id=usage_user_id, category=usage_category,
                    model=used_model, text=text, role=role, fallback_used=True,
                )
                return text
            fallback = self._fallback_cfg()
            if scene == "office" or llm_config or role or not (fallback and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 主供应商调用失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
            reply, text, used_model, role_info = await invoke(fallback["base_url"], fallback["api_key"], fallback["model"])
        await self._record(
            reply, messages=messages, user_id=usage_user_id, category=usage_category,
            model=used_model, text=text, role=role, role_info=role_info,
        )
        return text

    async def _role_fallback_target(
        self,
        role: str | None,
        exc: Exception,
        *,
        scene: str | None,
        user_id: str | None,
    ) -> dict[str, Any] | None:
        """角色回退目标（``main`` 策略才升级；只对可重试错误生效）。"""
        name = str(role or "").strip()
        if not name or not self._is_retryable_error(exc):
            return None
        from app.core import model_roles

        if model_roles.role_fallback_policy(name) != "main":
            return None
        resolved = await model_roles.resolve_role(
            model_roles.ROLE_DIRECT_ANSWER, scene=scene, user_id=user_id
        )
        return resolved.api_dict()

    async def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        scene: str | None = None,
        role: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        llm_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> tuple[str, list[dict]]:
        """LangChain 工具绑定，返回原有 OpenAI tool-call 字典形状。

        ``role`` 决定用哪个档位；工具能力由档位声明保证（``require_tools=True``：
        声明不支持工具的低成本档位会按角色回退策略升级到 main，而不是静默降级成
        "没有工具的模型"）。
        """
        if kwargs:
            logger.debug("忽略 ChatModel 工具调用的 legacy 参数: {}", sorted(kwargs))

        async def invoke(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            chat_model, used_model, used_base_url, role_info = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=None, max_tokens=None, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=False, messages=messages,
                llm_config=llm_config, role=role, require_tools=role is not None,
            )
            breaker = get_breaker(f"llm:{used_base_url}:{used_model}")
            try:
                bound = chat_model.bind_tools(tools, parallel_tool_calls=False)
                reply: AIMessage = await breaker.call(lambda: bound.ainvoke(convert_to_messages(messages)))
                calls = [
                    {"id": str(call.get("id") or ""), "type": "function", "function": {"name": str(call.get("name") or ""), "arguments": call.get("args") or {}}}
                    for call in (reply.tool_calls or [])
                ]
            except Exception as exc:
                if not self._is_tool_capability_error(exc):
                    raise
                logger.warning("模型不支持原生工具调用，使用文本 JSON 决策适配: {}", str(exc)[:160])
                reply = await breaker.call(lambda: chat_model.ainvoke(self._tool_fallback_messages(messages, tools)))
                calls = []
            normalized_text, normalized_calls, _warnings = normalize_tool_response(self._message_text(reply), calls)
            # Preserve provider thinking state across the manual tool loop.
            # DeepSeek rejects the next request when an assistant tool-call
            # message omits the reasoning_content returned with that call.
            reasoning = None
            try:
                reasoning = (getattr(reply, "additional_kwargs", {}) or {}).get("reasoning_content")
                if reasoning is None:
                    reasoning = (getattr(reply, "response_metadata", {}) or {}).get("reasoning_content")
            except Exception:  # noqa: BLE001
                reasoning = None
            if reasoning is not None:
                for item in normalized_calls:
                    item["reasoning_content"] = reasoning
            return reply, normalized_text, normalized_calls, used_model, role_info

        try:
            reply, content, tool_calls, used_model, role_info = await invoke(base_url, api_key, model)
        except Exception as exc:
            upgraded = await self._role_fallback_target(role, exc, scene=scene, user_id=usage_user_id)
            if upgraded is not None:
                logger.warning(
                    "[model-role] {} 工具调用失败，按回退策略切换 {} 重试: {}",
                    role, upgraded["model"], str(exc)[:120],
                )
                reply, content, tool_calls, used_model, role_info = await invoke(
                    upgraded.get("base_url"), upgraded.get("api_key"), upgraded.get("model")
                )
                await self._record(
                    reply, messages=messages, user_id=usage_user_id, category=usage_category,
                    model=used_model, text=content, role=role, role_info=role_info, fallback_used=True,
                )
                return content, tool_calls
            fallback = self._fallback_cfg()
            if scene == "office" or llm_config or role or not (fallback and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 工具调用主供应商失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
            reply, content, tool_calls, used_model, role_info = await invoke(
                fallback["base_url"], fallback["api_key"], fallback["model"]
            )
        await self._record(
            reply, messages=messages, user_id=usage_user_id, category=usage_category,
            model=used_model, text=content, role=role, role_info=role_info,
        )
        return content, tool_calls

    async def chat_with_tools_with_usage(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        scene: str | None = None,
        role: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        reasoning_effort: str | None = None,
        usage_user_id: str | None = None,
        usage_category: str | None = None,
        llm_config: dict[str, Any] | None = None,
        record_usage_event: bool = True,
        **kwargs: Any,
    ) -> tuple[str, list[dict], dict[str, Any]]:
        """Invoke function calling and return provider token usage for offline evaluation.

        Normal product paths should continue to use :meth:`chat_with_tools`.
        This narrow variant exists for offline, non-executing evaluation: callers
        can set ``record_usage_event=False`` so a benchmark does not pollute a
        real user's usage ledger.  It never executes a returned tool call.
        """
        if kwargs:
            logger.debug("忽略 ChatModel 工具调用的 legacy 参数: {}", sorted(kwargs))

        async def invoke(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            chat_model, used_model, used_base_url, role_info = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=None, max_tokens=None, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=False, messages=messages,
                llm_config=llm_config, role=role, require_tools=role is not None,
            )
            breaker = get_breaker(f"llm:{used_base_url}:{used_model}")
            try:
                bound = chat_model.bind_tools(tools, parallel_tool_calls=False)
                reply: AIMessage = await breaker.call(lambda: bound.ainvoke(convert_to_messages(messages)))
                calls = [
                    {"id": str(call.get("id") or ""), "type": "function", "function": {"name": str(call.get("name") or ""), "arguments": call.get("args") or {}}}
                    for call in (reply.tool_calls or [])
                ]
            except Exception as exc:
                if not self._is_tool_capability_error(exc):
                    raise
                logger.warning("模型不支持原生工具调用，使用文本 JSON 决策适配: {}", str(exc)[:160])
                reply = await breaker.call(lambda: chat_model.ainvoke(self._tool_fallback_messages(messages, tools)))
                calls = []
            normalized_text, normalized_calls, _warnings = normalize_tool_response(self._message_text(reply), calls)
            return reply, normalized_text, normalized_calls, used_model, role_info

        try:
            reply, content, tool_calls, used_model, role_info = await invoke(base_url, api_key, model)
        except Exception as exc:
            fallback = self._fallback_cfg()
            if scene == "office" or llm_config or not (fallback and self._is_retryable_error(exc)):
                raise
            logger.warning("LLM 工具调用主供应商失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
            reply, content, tool_calls, used_model, role_info = await invoke(
                fallback["base_url"], fallback["api_key"], fallback["model"]
            )

        prompt_tokens, completion_tokens = self._usage(reply)
        prompt_source = "provider" if prompt_tokens is not None else "estimated"
        completion_source = "provider" if completion_tokens is not None else "estimated"
        prompt_tokens = prompt_tokens if prompt_tokens is not None else sum(
            estimate_tokens(str(message.get("content") or "")) for message in messages
        )
        completion_tokens = completion_tokens if completion_tokens is not None else estimate_tokens(content)
        if record_usage_event:
            await record_usage(
                usage_user_id,
                usage_category or "chat",
                used_model,
                prompt_tokens,
                completion_tokens,
                model_role=str(role or "") or None,
                model_profile=str(getattr(role_info, "profile", "") or "") or None,
                config_source=str(getattr(role_info, "source", "") or "") or None,
            )
        return content, tool_calls, {
            "model": used_model,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(prompt_tokens + completion_tokens),
            "prompt_token_source": prompt_source,
            "completion_token_source": completion_source,
        }

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
        role: str | None = None,
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
        llm_config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """LangChain ``astream``；首个输出前失败才允许切备用供应商。

        角色档位同样只在**首 token 之前**参与切换：一旦已经吐出内容，就不再换模型
        （换成不同模型会让同一段回答前后风格/能力不一致，且工具状态无法迁移）。
        """
        if kwargs:
            logger.debug("忽略 ChatModel 流式调用的 legacy 参数: {}", sorted(kwargs))

        usage: tuple[int | None, int | None] = (None, None)
        role_state: dict[str, Any] = {"info": None}
        started_at = time.perf_counter()
        first_token_at: float | None = None

        async def stream_once(call_base_url: str | None, call_api_key: str | None, call_model: str | None):
            nonlocal usage
            chat_model, used_model, used_base_url, role_info = await self._model(
                scene=scene, user_id=usage_user_id, api_key=call_api_key, model=call_model, base_url=call_base_url,
                timeout=timeout, temperature=temperature, max_tokens=max_tokens, reasoning_effort=reasoning_effort,
                disable_reasoning_effort=disable_reasoning_effort, messages=messages,
                llm_config=llm_config, role=role,
            )
            role_state["info"] = role_info
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

        runtime_cfg = dict(llm_config or await get_llm_config(scene, self.provider, user_id=usage_user_id))
        text, used_model, emitted = "", model or runtime_cfg.get("model") or settings.DEEPSEEK_MODEL, False
        fallback_used = False
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
            # 首 token 之前才允许换模型：角色回退（main 策略）优先，再退备用供应商。
            upgraded = None if emitted else await self._role_fallback_target(
                role, exc, scene=scene, user_id=usage_user_id
            )
            if upgraded is not None:
                logger.warning(
                    "[model-role] {} 流式调用失败（首 token 前），按回退策略切换 {}: {}",
                    role, upgraded.get("model"), str(exc)[:120],
                )
                fallback_used = True
                used_model = str(upgraded.get("model") or used_model)
                async for delta in stream_once(
                    upgraded.get("base_url"), upgraded.get("api_key"), upgraded.get("model")
                ):
                    emitted = True
                    text += delta
                    yield delta
            else:
                fallback = self._fallback_cfg()
                if scene == "office" or llm_config or role or emitted or not (fallback and self._is_retryable_error(exc)):
                    raise
                logger.warning("LLM 流式主供应商失败，切换 {} 重试: {}", fallback["model"], str(exc)[:120])
                fallback_used = True
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
            model_role=str(role or "") or None,
            model_profile=str(getattr(role_state.get("info"), "profile", "") or "") or None,
            config_source=str(getattr(role_state.get("info"), "source", "") or "") or None,
            fallback_used=fallback_used,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
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
        client_options: dict[str, Any] = {
            "base_url": normalize_provider_base_url(cfg.get("base_url") or ""),
            "headers": {"Authorization": f"Bearer {cfg.get('api_key') or ''}"},
            "timeout": float(cfg.get("timeout") or 120.0),
        }
        # ChatModel 与 embedding 必须使用同一网络出口。此前仅前者读取
        # ``LLM_HTTP_PROXY``，会造成对话可用而 RAG/记忆写入持续连接失败。
        if proxy := resolve_http_proxy(settings.LLM_HTTP_PROXY):
            client_options["proxy"] = proxy
        async with AsyncClient(**client_options) as client:
            response = await client.post("/embeddings", json={"model": model or settings.EMBEDDING_MODEL, "input": texts})
            response.raise_for_status()
            return [item["embedding"] for item in response.json()["data"]]
