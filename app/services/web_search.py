"""联网搜索：Tavily API 封装（聊天"联网搜索"功能）."""

import httpx
from loguru import logger

from app.core.config import settings

# 联网搜索工具定义（LLM 通过 function calling 自主决定是否调用）
WEB_SEARCH_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "搜索互联网获取实时信息。当用户问题涉及最新新闻、实时数据、当前事件、"
            "或本地知识库无法覆盖的信息时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询，建议精简为关键词"}
            },
            "required": ["query"],
        },
    },
}


async def web_search(query: str, max_results: int | None = None) -> list[dict]:
    """调用 Tavily 搜索，返回 [{title, url, content}]；失败返回空列表（不阻塞对话）."""
    if not settings.TAVILY_API_KEY or not query.strip():
        return []
    max_results = max_results or settings.TAVILY_MAX_RESULTS
    try:
        async with httpx.AsyncClient(timeout=settings.TAVILY_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": settings.TAVILY_API_KEY,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": settings.TAVILY_SEARCH_DEPTH,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        results: list[dict] = []
        for item in data.get("results", []):
            url = (item.get("url") or "").strip()
            if not url:
                continue
            results.append(
                {
                    "title": (item.get("title") or "").strip(),
                    "url": url,
                    "content": (item.get("content") or "").strip(),
                }
            )
        return results
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tavily 搜索失败: {}", exc)
        return []
