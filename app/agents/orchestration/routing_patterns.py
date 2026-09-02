"""既有路由与策略特征共用的纯路由分类器。"""

from __future__ import annotations

import re

from app.agents.orchestration.policy.route_intent_patterns import (
    has_configured_agent_operation,
    has_configured_file_operation,
    has_configured_rag_operation,
    has_configured_external_operation,
    has_configured_multi_operation,
    has_configured_stateful_reasoning,
    has_configured_factual_document_question,
)

class _ConfiguredPattern:
    """兼容旧 ``pattern.search`` 调用，实际词汇来自 YAML。"""

    def __init__(self, matcher):
        self._matcher = matcher

    def search(self, text: str):
        return re.search(r"(?s).+", text or "") if self._matcher(text or "") else None


FILE_OPERATION = _ConfiguredPattern(lambda text: has_configured_file_operation(text))
RAG_OPERATION = _ConfiguredPattern(lambda text: has_configured_rag_operation(text))
EXTERNAL_OPERATION = _ConfiguredPattern(has_configured_external_operation)
MULTI_OPERATION = _ConfiguredPattern(has_configured_multi_operation)
STATEFUL_REASONING = _ConfiguredPattern(has_configured_stateful_reasoning)
FACTUAL_DOCUMENT_QUESTION = _ConfiguredPattern(has_configured_factual_document_question)


def file_operation_matches(text: str) -> bool:
    return bool(FILE_OPERATION.search(text or ""))


def rag_operation_matches(text: str) -> bool:
    return bool(RAG_OPERATION.search(text or ""))


def agent_operation_matches(text: str) -> bool:
    return has_configured_agent_operation(text)
