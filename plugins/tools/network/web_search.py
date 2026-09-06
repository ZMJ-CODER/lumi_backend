"""规范公网工具：``web_search`` 搜索，``web_fetch`` 抓取并清洗。"""

import ipaddress
import re
import socket
from urllib.parse import urlparse

import httpx

from app.agents.skills.base import Tool, SkillContext, ToolOutput
from app.services.web_search import WebSearchUnavailableError, web_search_required


class WebSearchSkill(Tool):
    name = "web_search"
    user_workflow_allowed = True
    description = (
        "受控只读网页检索：仅在用户明确要求联网/网页来源，或必须核实公开互联网中的"
        "新闻、政策和外部事实时使用。不得用于用户私有任务、对话历史、上传附件、知识库"
        "内容、总结改写、创作或计算；时间词、天气或价格词本身不是调用理由。返回带来源的结果。"
    )
    category = "network"
    environment = "server"
    scenes = ["chat", "office", "game"]
    domain = "research"
    intent_tags = ["联网", "网页", "公开资料", "新闻", "来源"]
    use_when = [
        "用户明确要求联网搜索、网页来源或公开资料",
        "需要核实公开新闻、政策或外部事实",
    ]
    do_not_use_when = [
        "用户私有任务、对话历史、上传附件或知识库内容",
        "当前日期时间应使用 get_datetime",
        "通用常识且用户未要求来源时直接回答",
    ]
    selection_examples = [
        "“联网搜索本周 AI 政策并给来源” → 使用",
        "“我今天的待办还有哪些？” → 不使用",
    ]
    result_contract = "返回 URL、标题、摘录和 citation；无结果时建议收窄公开查询条件。"
    output_contract = {
        "content_type": "structured",
        "budget": 2200,
        "truncate_strategy": "priority_fields",
        "on_overflow": "compress",
        "citation_required": True,
    }
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "公开网页检索关键词：保留用户给出的核心实体、时间和地域限定；不得翻译、扩写、替换实体或凭空补充年份。只移除“请搜索/给来源”等请求外壳。例如“最近那个 AI 监管新规”可写为“AI 监管 新规”。"},
            "allowed_domains": {"type": "array", "items": {"type": "string"}, "maxItems": 20, "description": "可选的域名白名单"},
            "max_results": {"type": "integer", "description": "必须显式填写返回条数：用户要求列 N 条时填 N；未指定时填 5；最多 10。", "minimum": 1, "maximum": 10},
        },
        "required": ["query", "max_results"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        query = str(params.get("query") or "").strip()
        if not query:
            return ToolOutput(
                success=False,
                error="缺少搜索关键词 query",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        max_results = int(params.get("max_results") or 5)
        try:
            allowed_domains = [str(item).strip() for item in (params.get("allowed_domains") or []) if str(item).strip()]
            results = await web_search_required(query, max_results, allowed_domains)
        except WebSearchUnavailableError as exc:
            return ToolOutput(success=False, error=f"公开资料检索失败，未完成来源核验：{exc}", error_code="WEB_SEARCH_UNAVAILABLE", retryable=True)
        if not results:
            return ToolOutput(
                success=False,
                error="未检索到相关结果，请换个关键词重试",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        # output 是回填模型上下文的摘要，不承载网页全文；完整抓取内容仅用于
        # 服务端审计与按需引用，避免模型把搜索结果逐字复制到最终回复。
        output_parts = []
        sources = []
        for i, item in enumerate(results):
            summary = " ".join(item["content"].split())[:240]
            output_parts.append(f"[{i + 1}] {item['title']}\n{item['url']}\n{summary}")
            sources.append({"title": item["title"], "url": item["url"], "snippet": summary})
        output = "\n\n".join(output_parts)
        return ToolOutput(
            success=True,
            output=output,
            data={"sources": sources, "count": len(sources)},
            content_type="structured",
            metadata={
                "citations": [
                    {"type": "web", "title": r["title"], "content": " ".join(r["content"].split())[:240], "source": r["url"]}
                    for r in results
                ],
                "decision_signals": {
                    "result_count": len(results),
                    "confidence_hint": {
                        "level": "medium",
                        "basis": ["public_web_snippets", f"result_count={len(results)}"],
                    },
                    "more_available": len(results) >= max_results,
                    "refine_suggestion": "若来源不够具体，请增加主体、地域或时间限定词后重试。",
                },
            },
        )


class WebFetchTool(Tool):
    """抓取指定网页，只返回清洗后的短摘要，不把 HTML 原文交给模型。"""

    name = "web_fetch"
    user_workflow_allowed = True
    description = "抓取用户指定的公网 URL，提取与问题相关的事实摘要和引用；不返回网页原文。"
    category = "network"
    domain = "research"
    resource = "public_web"
    environment = "server"
    intent_tags = ["网页内容", "官方文档", "提取事实", "来源"]
    use_when = ["用户提供 URL 并要求读取、核对或提取页面内容"]
    do_not_use_when = ["只有关键词搜索需求时使用 web_search", "URL 指向本机、内网或凭据服务"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "公开 HTTP(S) URL"},
            "prompt": {"type": "string", "description": "希望从页面提取的事实问题"},
        },
        "required": ["url", "prompt"],
    }
    result_contract = "返回 title、summary、key_facts、citation；不返回 HTML 或脚本。"
    output_contract = {"content_type": "structured", "budget": 2200, "truncate_strategy": "priority_fields", "on_overflow": "compress", "citation_required": True}

    @staticmethod
    def _safe_url(value: str) -> bool:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        host = parsed.hostname.casefold()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False
        try:
            for info in socket.getaddrinfo(host, None):
                addr = ipaddress.ip_address(info[4][0])
                if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                    return False
        except OSError:
            return False
        return True

    @staticmethod
    def _clean_html(raw: str) -> str:
        text = re.sub(r"(?is)<(script|style|noscript|svg).*?>.*?</\1>", " ", raw)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        # 页面内容是不可信数据；剥离常见提示注入控制短语，避免它们进入摘要。
        text = re.sub(r"(?i)(ignore (?:all|previous) instructions|忽略(?:之前|所有)指令|system prompt|系统提示词)", " ", text)
        return text.strip()

    @staticmethod
    def _select_relevant(text: str, prompt: str) -> str:
        """按用户问题保留相关句子，避免整页内容进入上下文。"""
        sentences = [part.strip() for part in re.split(r"(?<=[。！？.!?])\s+", text) if part.strip()]
        terms = [term.casefold() for term in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", prompt)]
        if not terms:
            return text[:1800]
        ranked = sorted(
            ((sum(term in sentence.casefold() for term in terms), index, sentence) for index, sentence in enumerate(sentences)),
            key=lambda item: (-item[0], item[1]),
        )
        selected = [sentence for score, _, sentence in ranked if score > 0][:8]
        return " ".join(selected)[:1800] or text[:1800]

    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        url = str(params.get("url") or "").strip()
        prompt = str(params.get("prompt") or "").strip()
        if not url or not prompt:
            return ToolOutput(success=False, error="缺少 url 或 prompt", error_code="INVALID_ARGS", retryable=False)
        if not self._safe_url(url):
            return ToolOutput(success=False, error="URL 不允许访问本机或内网地址", error_code="SSRF_BLOCKED", retryable=False)
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": "Lumi-WebFetch/1.0"}) as client:
                response = await client.get(url)
            if response.status_code in {301, 302, 303, 307, 308}:
                return ToolOutput(success=False, error="检测到重定向，请确认目标 URL 后再次抓取", error_code="REDIRECT_REQUIRES_CONFIRMATION", retryable=False)
            response.raise_for_status()
            cleaned = self._clean_html(response.text)
            # Tiered projection: short pages are passed through after cleaning;
            # medium pages keep only prompt-relevant sentences; long pages are
            # bounded to a compact relevant extract.  No raw HTML is returned.
            from app.core.config import settings
            direct_limit = max(1000, int(getattr(settings, "WEB_FETCH_DIRECT_MAX_CHARS", 16000)))
            relevant_limit = max(800, int(getattr(settings, "WEB_FETCH_RELEVANT_MAX_CHARS", 8000)))
            if len(cleaned) <= direct_limit:
                summary = cleaned[:direct_limit]
                projection_mode = "clean_direct"
            elif len(cleaned) <= direct_limit * 3:
                summary = self._select_relevant(cleaned, prompt)[:relevant_limit]
                projection_mode = "relevant_extract"
            else:
                summary = self._select_relevant(cleaned, prompt)[:1800]
                projection_mode = "compressed_extract"
            title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", response.text)
            title = self._clean_html(title_match.group(1))[:200] if title_match else url
            citation = {"title": title, "source": url, "snippet": summary[:240]}
            return ToolOutput(success=True, output=summary, data={"url": url, "title": title, "summary": summary, "key_facts": [summary[:600]], "citation": citation}, content_type="structured", metadata={"citations": [citation], "total_size": len(response.content), "summary": summary[:400], "quality_hints": {"projection_mode": projection_mode, "cleaned_chars": len(cleaned)}})
        except Exception as exc:
            return ToolOutput(success=False, error=f"网页抓取失败: {exc}", error_code="WEB_FETCH_FAILED", retryable=True)

