"""InformationResolver：统一信息解析（适配器 + smart_slice）。

职责：
  - 按 ``TaskProfile.info_sources`` 选择适配器取资料；
  - USER_PROVIDED / CONVERSATION_MEMORY / INTERNAL_KNOWLEDGE 已在上下文，
    默认不产生外部调用（可注入 provider 以取 RAG/记忆）；
  - 合并、去重、按来源限长，再 smart_slice（关键词定位/目录定位/头尾保留）；
  - **绝不抛出 CONTEXT_TOO_LARGE**：超长只返回 SLICED；
    仅当调用方声明需要跨段综合分析时返回 MULTI_STEP_REQUIRED（建议 M2）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from loguru import logger

from lumi_orch.upgrade_policy import ContextFitStatus, ContextPlan, plan_context_fit

SourceReader = Callable[[str], Awaitable[str]]

_CONTEXT_ONLY_SOURCES = frozenset({"USER_PROVIDED", "CONVERSATION_MEMORY", "INTERNAL_KNOWLEDGE"})
_CROSS_SEGMENT_HINT = re.compile(
    r"(?iu)(?:整本|全书|全篇|逐段|通读|全部内容|完整分析|每一章|summarize the whole|entire document)"
)


@dataclass(slots=True)
class ResolvedContext:
    """解析结果：text 为可直接注入模型的资料；status 表达适配结果。"""

    text: str = ""
    citations: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    plan: ContextPlan = field(default_factory=lambda: ContextPlan(ContextFitStatus.OK, 0))
    per_source_chars: dict[str, int] = field(default_factory=dict)

    @property
    def status(self) -> ContextFitStatus:
        return self.plan.status

    @property
    def needs_multi_step(self) -> bool:
        return self.status == ContextFitStatus.MULTI_STEP_REQUIRED


def requires_cross_segment(query: str) -> bool:
    return bool(_CROSS_SEGMENT_HINT.search(str(query or "")))


# 明确要求"整份/全文/通读"的意图：这类请求应该按页读到文件结束，而不是只给一页。
_COMPLETE_READ_HINT = re.compile(
    r"(整份|完整|全文|全部(页|内容|章节)?|通读|从头到尾|读到结尾|读到底|整个文档|整个文件"
    r"|每一页|逐页|所有页|不要截断|别截断|read\s+(it\s+)?all|entire\s+(document|file)|full\s+text)",
    re.IGNORECASE,
)


def requires_complete_read(query: str) -> bool:
    """用户是否明确要求把文件读完（用于给 read 打 read_to_end 标记）。"""
    return bool(_COMPLETE_READ_HINT.search(str(query or "")))


def smart_slice(text: str, query: str, *, keep_chars: int) -> str:
    """关键词/标题定位优先，其次保留头尾，避免整段丢弃关键信息。"""
    raw = str(text or "")
    limit = max(64, int(keep_chars))
    if len(raw) <= limit:
        return raw
    keywords = [token for token in re.split(r"[\s,，。;；:：!?！？/\\]+", str(query or "")) if len(token) >= 2]
    for token in keywords:
        index = raw.casefold().find(token.casefold())
        if index >= 0:
            start = max(0, index - limit // 3)
            return raw[start:start + limit]
    head = limit * 2 // 3
    tail = limit - head
    return raw[:head] + "\n…（内容过长，已截取相关片段）…\n" + raw[-tail:]


class InformationResolver:
    """适配器集合：宿主注入具体实现（工作区读取/联网/第三方/知识库/记忆）。"""

    def __init__(
        self,
        *,
        workspace_reader: SourceReader | None = None,
        web_searcher: SourceReader | None = None,
        service_querier: SourceReader | None = None,
        knowledge_searcher: SourceReader | None = None,
        memory_loader: SourceReader | None = None,
        per_source_limit: int = 12000,
        total_size_limit: int = 24000,
    ) -> None:
        self._workspace = workspace_reader
        self._web = web_searcher
        self._service = service_querier
        self._knowledge = knowledge_searcher
        self._memory = memory_loader
        self._per_source_limit = max(1000, int(per_source_limit))
        self._total_size_limit = max(2000, int(total_size_limit))

    async def _read(self, source: str, query: str) -> tuple[str, str]:
        """返回 (text, status_note)。任何异常都降级为空文本 + 说明。"""
        try:
            if source in _CONTEXT_ONLY_SOURCES:
                if source == "INTERNAL_KNOWLEDGE" and self._knowledge is not None:
                    return await self._knowledge(query), "ok"
                if source == "CONVERSATION_MEMORY" and self._memory is not None:
                    return await self._memory(query), "ok"
                # 已在上下文中：无需外部调用。
                return "", "in_context"
            if source == "WORKSPACE":
                if self._workspace is None:
                    return "", "adapter_unavailable"
                return await self._workspace(query), "ok"
            if source == "EXTERNAL_WEB":
                if self._web is None:
                    return "", "adapter_unavailable"
                return await self._web(query), "ok"
            if source == "PRIVATE_SERVICE":
                if self._service is None:
                    return "", "adapter_unavailable"
                return await self._service(query), "ok"
        except Exception as exc:  # noqa: BLE001 - 适配器失败不得中断本轮
            logger.warning("InformationResolver 读取失败 source={}: {}", source, str(exc)[:160])
            return "", f"error:{str(exc)[:60]}"
        return "", "unknown_source"

    async def resolve(
        self,
        sources: list[str],
        query: str,
        *,
        requires_cross_segment_analysis: bool | None = None,
    ) -> ResolvedContext:
        blocks: list[str] = []
        citations: list[dict] = []
        records: list[dict] = []
        per_source: dict[str, int] = {}
        seen: set[str] = set()
        for source in sources:
            text, note = await self._read(source, query)
            per_source[source] = len(text)
            records.append({"source": source, "chars": len(text), "status": note})
            stripped = str(text or "").strip()
            if not stripped or note != "ok":
                continue
            fingerprint = stripped[:200]
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            blocks.append(f"[{source}]\n{stripped[: self._per_source_limit]}")
            citations.append({"source": source})

        merged = "\n\n".join(blocks)
        cross_segment = (
            requires_cross_segment_analysis
            if requires_cross_segment_analysis is not None
            else requires_cross_segment(query)
        )
        plan = plan_context_fit(
            text_length=len(merged),
            size_limit=self._total_size_limit,
            requires_cross_segment=cross_segment,
        )
        if plan.status == ContextFitStatus.SLICED:
            merged = smart_slice(merged, query, keep_chars=plan.keep_chars)
        elif plan.status == ContextFitStatus.MULTI_STEP_REQUIRED:
            merged = merged[: plan.keep_chars]
        return ResolvedContext(
            text=merged,
            citations=citations,
            records=records,
            plan=plan,
            per_source_chars=per_source,
        )


__all__ = [
    "InformationResolver",
    "ResolvedContext",
    "requires_cross_segment",
    "requires_complete_read",
    "smart_slice",
]
