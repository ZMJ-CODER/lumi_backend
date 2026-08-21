"""Retrieval scope routing.

The vector infrastructure is shared, but personal memories, long-lived
knowledge and office attachments must not be retrieved as one corpus.  This
module contains only deterministic, explainable routing rules so the fast chat
path never pays for a classifier call before its first model request.
"""

from __future__ import annotations

from enum import StrEnum


class RetrievalScope(StrEnum):
    NONE = "none"
    MEMORY = "memory"
    PERSONAL_KNOWLEDGE = "personal_knowledge"
    OFFICE_ATTACHMENT = "office_attachment"
    PUBLIC_KNOWLEDGE = "public_knowledge"


_MEMORY_MARKERS = (
    "上次", "之前", "以前", "刚才", "还记得", "记得我", "我的偏好",
    "我的习惯", "我的设置", "我的计划", "我的目标", "按我的", "延续",
)
_KNOWLEDGE_MARKERS = (
    "知识库", "资料库", "查资料", "检索", "搜索我的资料", "上传的文档",
    "上传的文件", "这份文档", "这个文档", "附件", "文档里", "文件里",
    "资料里", "根据文档", "根据资料", "之前上传", "刚才上传",
)
_PUBLIC_MARKERS = ("公共知识库", "公共资料库")


def has_memory_reference(content: str, retrieval_query: str | None = None) -> bool:
    text = (retrieval_query or content or "").lower()
    return any(marker in text for marker in _MEMORY_MARKERS)


def route_chat_retrieval_scope(
    content: str,
    attachments: list | None = None,
    retrieval_query: str | None = None,
) -> RetrievalScope:
    """Choose one default source for a normal-chat request.

    Explicit document/attachment references take precedence over historical
    references.  A request that genuinely needs both sources must be modelled
    as an explicit orchestration step instead of silently merging corpora.
    """
    if retrieval_query and retrieval_query.strip():
        return RetrievalScope.PERSONAL_KNOWLEDGE
    for attachment in attachments or []:
        if isinstance(attachment, dict) and str(attachment.get("type") or "").lower() not in {
            "image", "audio", "video"
        }:
            return RetrievalScope.PERSONAL_KNOWLEDGE
    text = (content or "").lower()
    if "当前挂载的文档内容" in text:
        return RetrievalScope.PERSONAL_KNOWLEDGE
    if any(marker in text for marker in _PUBLIC_MARKERS):
        return RetrievalScope.PUBLIC_KNOWLEDGE
    if any(marker in text for marker in _KNOWLEDGE_MARKERS):
        return RetrievalScope.PERSONAL_KNOWLEDGE
    if has_memory_reference(content, retrieval_query):
        return RetrievalScope.MEMORY
    return RetrievalScope.NONE
