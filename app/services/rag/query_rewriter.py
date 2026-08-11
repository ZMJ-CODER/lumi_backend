"""服务端查询重写（可插拔）.

设计：
  - 改写发生在服务端，默认走云端 qwen-turbo；
  - 仅办公模式（scene=office）启用，其他场景直接原文，保证回复速度；
  - 服务端本地小模型为预留插槽：RAG_QUERY_REWRITE_PROVIDER=local
    并配置 BASE_URL / MODEL 后生效（OpenAI 兼容端点，如 Ollama）。

优先级：客户端提供的 retrieval_query > 服务端重写 > 原始 content。
"""

from loguru import logger

from app.core.config import settings
from app.services.usage import CATEGORY_REWRITE, record_usage

REWRITE_SYSTEM_PROMPT = (
    "你是查询精炼助手。把用户的口语化提问改写为适合知识库检索的查询："
    "保留关键实体和术语，补充必要的同义词，去除口语冗余和语气词。"
    "如果原问题已经清晰可直接检索，原样输出。只输出改写后的查询文本，不要输出其他内容。"
)


async def _rewrite(
    base_url: str, api_key: str | None, model: str, text: str, user_id: str | None = None
) -> str | None:
    """调用 OpenAI 兼容端点改写查询；失败返回 None（由调用方回退原文）."""
    try:
        import httpx

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=settings.RAG_QUERY_REWRITE_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers=headers,
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                        {"role": "user", "content": f"用户提问：{text}"},
                    ],
                    "temperature": 0.2,
                    "max_tokens": 256,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage") or {}
            await record_usage(
                user_id,
                CATEGORY_REWRITE,
                model,
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
            )
            rewritten = (data["choices"][0]["message"]["content"] or "").strip()
            return rewritten or None
    except Exception as e:
        logger.warning("服务端查询重写失败，使用原文: {}", e)
        return None


async def rewrite_query(text: str, user_id: str | None = None) -> str | None:
    """服务端改写：默认云端 qwen-turbo；本地小模型插槽配置后走本地."""
    if not settings.RAG_QUERY_REWRITE_ENABLED:
        return None
    if (
        settings.RAG_QUERY_REWRITE_PROVIDER == "local"
        and settings.RAG_QUERY_REWRITE_MODEL.strip()
    ):
        return await _rewrite(
            settings.RAG_QUERY_REWRITE_BASE_URL,
            None,
            settings.RAG_QUERY_REWRITE_MODEL.strip(),
            text,
            user_id,
        )
    return await _rewrite(
        settings.QWEN_BASE_URL, settings.QWEN_API_KEY, settings.QWEN_TURBO_MODEL, text, user_id
    )


async def get_retrieval_query(
    content: str,
    client_query: str | None = None,
    scene: str | None = None,
    user_id: str | None = None,
) -> str:
    """决定本次检索查询：客户端精炼 > 服务端重写（仅办公模式）> 原文."""
    if client_query and client_query.strip():
        return client_query
    # 仅办公模式启用改写，其他场景直接原文（保回复速度）
    if scene != "office":
        return content
    rewritten = await rewrite_query(content, user_id)
    return (rewritten or content).strip()[:500]
