"""联网基础工具。"""

import ipaddress
from urllib.parse import urlparse
import socket

import httpx

from app.agents.skills.base import SkillContext, Tool, ToolOutput
from app.services.web_search import web_search


class WebSearchTool(Tool):
    name = "WebSearch"
    description = "检索公开网页并返回短摘要与来源；不读取用户私有数据。"
    category = "network"
    domain = "research"
    resource = "public_web"
    environment = "server"
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "allowed_domains": {"type": "array", "items": {"type": "string"}},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        "required": ["query"],
    }
    use_when = ["用户明确要求联网搜索或公开网页来源"]
    do_not_use_when = ["用户私有文件、对话历史或知识库内容", "不需要外部事实时"]
    result_contract = "返回标题、URL、短摘录和引用，不回填网页全文。"
    direct_instruction_field = "query"
    direct_required_fields = ["query"]

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        query = str(params.get("query") or "").strip()
        if not query:
            return ToolOutput(success=False, error="缺少 query", error_code="INVALID_ARGS", retryable=False)
        allowed_domains = [str(item).strip() for item in (params.get("allowed_domains") or []) if str(item).strip()]
        results = await web_search(query, min(max(int(params.get("max_results") or 5), 1), 10), allowed_domains)
        if not results:
            return ToolOutput(success=False, error="未检索到结果", error_code="NO_RESULTS", retryable=True)
        sources = [{"title": str(x.get("title") or ""), "url": str(x.get("url") or ""), "snippet": " ".join(str(x.get("content") or "").split())[:240]} for x in results]
        return ToolOutput(success=True, data={"sources": sources, "count": len(sources)}, content_type="structured", metadata={"citations": sources})


class WebFetchTool(Tool):
    name = "WebFetch"
    description = "抓取指定公开 HTTP(S) 页面并转换为受限纯文本；禁止访问本机、内网、云元数据和文件协议。"
    category = "network"
    domain = "research"
    resource = "public_web"
    environment = "server"
    parameters_schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "prompt": {"type": "string"}},
        "required": ["url", "prompt"],
    }
    use_when = ["用户明确要求抓取指定公开网页"]
    do_not_use_when = ["URL 指向内网、本机、文件或凭据服务", "仅需要搜索时应使用 WebSearch"]
    result_contract = "返回页面标题和受限正文摘要，不返回超大 HTML。"

    @staticmethod
    def _safe_url(value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                addr = ipaddress.ip_address(info[4][0])
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    return False
        except OSError:
            return False
        return True

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        url = str(params.get("url") or "").strip()
        if not self._safe_url(url):
            return ToolOutput(success=False, error="URL 不允许访问本机或内网地址", error_code="SSRF_BLOCKED", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": "Lumi-WebFetch/1.0"}) as client:
                response = await client.get(url)
            if response.status_code in {301, 302, 303, 307, 308}:
                return ToolOutput(success=False, error="检测到重定向，请先确认目标 URL 后再次抓取", error_code="REDIRECT_REQUIRES_CONFIRMATION", retryable=False)
            response.raise_for_status()
            text = response.text[:120_000]
            return ToolOutput(success=True, data={"url": url, "content": text[:12_000]}, content_type="structured", metadata={"total_size": len(response.content), "summary": text[:500]})
        except Exception as exc:
            return ToolOutput(success=False, error=f"网页抓取失败: {exc}", error_code="WEB_FETCH_FAILED", retryable=True)
