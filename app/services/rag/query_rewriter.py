"""服务端查询重写（可插拔）.

设计：
  - 改写发生在服务端，默认走云端 qwen-turbo；
  - 仅办公模式（scene=office）启用，其他场景直接原文，保证回复速度；
  - 服务端本地小模型为预留插槽：RAG_QUERY_REWRITE_PROVIDER=local。
    仅在安装了独立文本 Ollama 模型时启用；视觉模型不作为改写器使用。

优先级：客户端提供的 retrieval_query > 服务端重写 > 原始 content。
"""

from loguru import logger

from app.core.config import settings
from app.core.llm import LLMClient
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
        rewritten = await LLMClient().chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                {"role": "user", "content": f"用户提问：{text}"},
            ],
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=settings.RAG_QUERY_REWRITE_TIMEOUT_SECONDS,
            temperature=0.2,
            max_tokens=256,
            usage_user_id=user_id,
            usage_category=CATEGORY_REWRITE,
            disable_reasoning_effort=True,
        )
        return rewritten.strip() or None
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
        return await _rewrite_local(text, user_id)
    return await _rewrite(
        settings.QWEN_BASE_URL, settings.QWEN_API_KEY, settings.QWEN_TURBO_MODEL, text, user_id
    )


async def _rewrite_local(text: str, user_id: str | None = None) -> str | None:
    """本地小模型改写（Ollama 原生 /api/chat）.

    qwen3 系列默认 thinking 模式：OpenAI 兼容端点会把 token 全耗在推理上、
    content 返回空。这里走原生 API 并显式 think=False，直接输出改写结果。
    """
    try:
        import httpx

        # qwen2.5vl 是视觉模型，不能作为纯文本检索改写器。禁止把它当成
        # "本地文本模型" 调用，以免慢、输出质量差且与识图负载相互争抢。
        model_name = settings.RAG_QUERY_REWRITE_MODEL.strip()
        if "vl" in model_name.lower() or "vision" in model_name.lower():
            logger.warning("本地查询重写模型 {} 为视觉模型，改用原文", model_name)
            return None
        base = settings.RAG_QUERY_REWRITE_BASE_URL.rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]  # 兼容遗留的 /v1 后缀 → 原生根路径
        async with httpx.AsyncClient(timeout=settings.RAG_QUERY_REWRITE_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{base}/api/chat",
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": REWRITE_SYSTEM_PROMPT},
                        {"role": "user", "content": f"用户提问：{text}"},
                    ],
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.2, "num_predict": 512},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            rewritten = ((data.get("message") or {}).get("content") or "").strip()
            # 噪声守卫：qwen3 系可能输出推理过程而非改写结果，识别后回退原文
            # （避免污染检索查询；换用 qwen2.5 等非 thinking 模型后自然通过）
            noise_markers = ("首先", "我需要", "我的任务", "作为", "分析", "示例", "改写后", "用户提问是", "用户提问：")
            head = rewritten[:80]
            if len(rewritten) > max(200, len(text) * 3) or any(m in head for m in noise_markers):
                logger.info("本地重写输出疑似推理噪声，回退原文: {}", head)
                return None
            await record_usage(
                user_id,
                CATEGORY_REWRITE,
                model_name,
                data.get("prompt_eval_count"),
                data.get("eval_count"),
            )
            return rewritten or None
    except Exception as e:  # noqa: BLE001
        logger.warning("本地查询重写失败，使用原文: {}", e)
        return None


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
