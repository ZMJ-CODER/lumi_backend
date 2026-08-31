"""Load bounded, deployment-time markers for four-channel intent features."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError

from app.core.config import settings
from app.monitoring.context import MonitorContext
from app.monitoring.logger import monitor_logger


class _PairMarkers(BaseModel):
    actions: tuple[str, ...] = Field(min_length=1, max_length=128)
    targets: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=128)
    sources: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=128)


class _AgentMarkers(BaseModel):
    phrases: tuple[str, ...] = Field(min_length=1, max_length=128)


class RouteIntentPatternDocument(BaseModel):
    version: int = 1
    file_operation: _PairMarkers
    rag_operation: _PairMarkers
    agent_operation: _AgentMarkers


@dataclass(frozen=True, slots=True)
class RouteIntentPatterns:
    file_actions: tuple[str, ...]
    file_targets: tuple[str, ...]
    rag_actions: tuple[str, ...]
    rag_sources: tuple[str, ...]
    agent_phrases: tuple[str, ...]


def _fallback() -> RouteIntentPatterns:
    return RouteIntentPatterns((), (), (), (), ())


@lru_cache(maxsize=1)
def load_route_intent_patterns() -> RouteIntentPatterns:
    path = Path(settings.AGENT_ROUTING_INTENT_PATTERN_PATH)
    try:
        document = RouteIntentPatternDocument.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        )
        if document.version != 1:
            raise ValueError(f"unsupported pattern version: {document.version}")
        return RouteIntentPatterns(
            file_actions=document.file_operation.actions,
            file_targets=document.file_operation.targets or (),
            rag_actions=document.rag_operation.actions,
            rag_sources=document.rag_operation.sources or (),
            agent_phrases=document.agent_operation.phrases,
        )
    except (OSError, ValidationError, yaml.YAMLError, ValueError) as exc:
        monitor_logger.error(
            "四通道路由意图词典加载失败，使用内置模式",
            event_type="policy_load_failure",
            category="configuration",
            code="ROUTE_INTENT_PATTERN_LOAD_FAILED",
            context=MonitorContext(component="route_intent_patterns"),
            metadata={"path": str(path), "error": str(exc)[:300]},
            exc_info=exc,
        )
        return _fallback()


def _contains_pair(text: str, left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    folded = (text or "").casefold()
    for first in left:
        for second in right:
            a, b = first.casefold(), second.casefold()
            if (a in folded and b in folded):
                return True
    return False


def has_configured_file_operation(text: str) -> bool:
    patterns = load_route_intent_patterns()
    return _contains_pair(text, patterns.file_actions, patterns.file_targets)


def has_configured_rag_operation(text: str) -> bool:
    patterns = load_route_intent_patterns()
    return _contains_pair(text, patterns.rag_actions, patterns.rag_sources)


def has_configured_agent_operation(text: str) -> bool:
    folded = (text or "").casefold()
    return any(phrase.casefold() in folded for phrase in load_route_intent_patterns().agent_phrases)

