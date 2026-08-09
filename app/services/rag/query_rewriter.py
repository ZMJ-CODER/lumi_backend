"""服务端查询重写（可插拔，默认关闭）.

用途：
  - 手机端等没有本地小模型的客户端，由服务端小模型完成提问精炼。
  - 与客户端本地模型槽位互补：客户端能精炼就用客户端的，否则服务端兜底。

优先级：客户端提供的 retrieval_query > 服务端重写 > 原始 content。
默认关闭（RAG_QUERY_REWRITE_ENABLED=false），启用后配置 OpenAI 兼容端点即可。
"""

from loguru import logger

from app.core.config import settings

REWRITE_SYSTEM_PROMPT = (
    "你是查询精炼助手。把用户的口语化提问改写为适合知识库检索的查询："
    "保留关键实体和术语，补充必要的同义词，去除口语冗余和语气词。"
    "如果原问题已经清晰可直接检索，原样输出。只输出改写后的查询文本，不要输出其他内容。"
)


async def rewrite_query(text: str) -> str | None:
    """调用服务端配置的小模型改写查询；未启用/失败时返回 None."""
    if not settings.RAG_QUERY_REWRITE_ENABLED:
        return None
    base_url = settings.RAG_QUERY_REWRITE_BASE_URL.rstrip("/")
    model = settings.RAG_QUERY_REWRITE_MODEL.strip()
    if not model:
        return None

    try:
        import httpx

        async with httpx.AsyncClient(timeout=settings.RAG_QUERY_REWRITE_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
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
            rewritten = (data["choices"][0]["message"]["content"] or "").strip()
            return rewritten or None
    except Exception as e:
        logger.warning("服务端查询重写失败，使用原文: {}", e)
        return None


async def get_retrieval_query(content: str, client_query: str | None = None) -> str:
    """决定本次检索用的查询：客户端精炼 > 服务端重写 > 原文."""
    if client_query and client_query.strip():
        return client_query
    rewritten = await rewrite_query(content)
    return rewritten or content
