"""联网搜索：Tavily API 封装（聊天"联网搜索"功能）."""

import httpx
from loguru import logger

from app.core.config import settings
from app.core.resilience import CircuitOpenError, get_breaker


class WebSearchUnavailableError(RuntimeError):
    """A fresh-data request could not obtain verifiable web sources."""

# 联网搜索工具定义（LLM 通过 function calling 自主决定是否调用）
WEB_SEARCH_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "受控只读工具：仅在用户明确要求搜索/网页来源，或回答必须核实公开互联网中的"
            "新闻、政策和外部事实时使用。不得用于用户私有状态、对话历史、上传附件、知识库"
            "内容、总结改写、创作或计算；‘今天/当前/实时’等词本身不足以触发调用。"
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


async def web_search_required(query: str, max_results: int | None = None) -> list[dict]:
    """Fetch fresh results or raise, so callers cannot silently use stale model knowledge."""
    if not query.strip():
        raise WebSearchUnavailableError("联网搜索缺少查询内容")
    if not settings.TAVILY_API_KEY:
        raise WebSearchUnavailableError("联网搜索未配置 API Key，请检查 Tavily 连接设置")
    max_results = max_results or settings.TAVILY_MAX_RESULTS
    try:
        async def _search() -> dict:
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
                return resp.json()

        data = await get_breaker("tavily:search").call(_search)
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
        if not results:
            raise WebSearchUnavailableError("联网搜索没有返回可用来源，请稍后重试或换一种问法")
        logger.info("Tavily 搜索完成: query={} results={}", query[:80], len(results))
        return results
    except CircuitOpenError as exc:
        logger.warning("Tavily 搜索暂时熔断: {}", exc)
        raise WebSearchUnavailableError("联网搜索暂时不可用，请检查网络或 Tavily 配额后重试") from exc
    except WebSearchUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tavily 搜索失败: {}", exc)
        raise WebSearchUnavailableError("联网搜索失败，请检查网络、Tavily API Key 或账户额度") from exc


async def web_search(query: str, max_results: int | None = None) -> list[dict]:
    """Best-effort compatibility wrapper for optional tool call sites."""
    try:
        return await web_search_required(query, max_results)
    except WebSearchUnavailableError as exc:
        logger.info("Tavily 可选搜索未完成: {}", exc)
        return []
