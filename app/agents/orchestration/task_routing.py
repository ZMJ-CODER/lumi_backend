"""Four-channel routing for office work units.

The scheduler deliberately classifies *execution requirements*, not document
genres.  A task list is first compiled into JSON-safe atomic work units and
then each unit independently selects the cheapest sufficient channel:

``direct_llm`` -> ``deterministic_script`` -> ``rag`` -> ``agent``.

This module is intentionally usable without an LLM.  A planner may enrich the
same schema later, but all routing decisions are validated here before a DAG
sees them.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class RouteChannel(str, Enum):
    DIRECT_LLM = "direct_llm"
    DETERMINISTIC_SCRIPT = "deterministic_script"
    RAG = "rag"
    AGENT = "agent"


class AtomicWorkItem(BaseModel):
    """Persisted, user-auditable unit of work; indices are 1-based at input."""

    id: str
    instruction: str
    description: str = ""
    estimated_type: RouteChannel = RouteChannel.AGENT
    dependencies: list[int] = Field(default_factory=list)
    estimated_tokens: int = Field(default=0, ge=0)
    # A decomposer may emit this only for a real mixed request.  It is flattened
    # before execution; no executor is allowed to treat it as a single tool.
    subtasks: list[dict[str, Any]] = Field(default_factory=list)


class RouteDecision(BaseModel):
    channel: RouteChannel
    reason: str
    estimated_tokens: int = Field(ge=0)


_FILE_OPERATION = re.compile(
    r"(?iu)(?:转换|转为|转成|导出|另存为|保存为|批量处理|运行脚本|执行脚本)"
    r".{0,36}(?:文件|附件|文档|表格|\.csv\b|\.tsv\b|\.xlsx\b|\.docx\b|\.pptx\b|\.pdf\b|\.txt\b)"
)
_RAG_OPERATION = re.compile(
    r"(?iu)(?:知识库|资料库|检索|查找|查询).{0,28}(?:资料|文档|信息|内容|记录|知识库)|"
    r"(?:根据|从).{0,20}(?:知识库|资料|文档).{0,24}(?:回答|说明|找出|查询)"
)
_EXTERNAL_OPERATION = re.compile(
    r"(?iu)(?:打开|启动|发送|删除|修改|编辑|创建|添加|取消|安排|设置)"
    r".{0,32}(?:应用|软件|浏览器|网页|邮件|日程|日历|待办|数据库|文件|文档)"
)
_MULTI_OPERATION = re.compile(
    r"(?iu)(?:先.+(?:再|然后)|(?:读取|分析|提取).{0,80}(?:并|再|然后).{0,80}"
    r"(?:核对|检查|发送|写入|修改|导出)|第\s*[一二三四五六七八九十0-9]+\s*步)"
)
_STATEFUL_REASONING = re.compile(
    r"(?iu)(?:核对|校验|验证|审查|合规|审批|比对|排查|诊断).{0,32}"
    r"(?:系统|规则|要求|标准|条款|记录|数据|状态)|"
    r"(?:核对|校验|验证|审查|合规|审批|比对|排查|诊断)"
)


def estimate_tokens(instruction: str, channel: RouteChannel) -> int:
    """Conservative pre-execution budget used as a guardrail, never billing."""
    chars = max(1, len((instruction or "").strip()))
    base = {
        RouteChannel.DIRECT_LLM: 800,
        RouteChannel.DETERMINISTIC_SCRIPT: 1_600,
        RouteChannel.RAG: 1_200,
        RouteChannel.AGENT: 3_500,
    }[channel]
    return min(20_000, base + chars // 2)


def route_atomic_instruction(
    instruction: str,
    *,
    has_authorized_documents: bool = False,
) -> RouteDecision:
    """Pick one channel for a *single* atomic request.

    The ordering is intentional.  A request that needs several capabilities is
    not a route target; it belongs to the agent channel until decomposition
    expands it into a local subgraph.
    """
    text = (instruction or "").strip()
    if _MULTI_OPERATION.search(text) or _EXTERNAL_OPERATION.search(text) or _STATEFUL_REASONING.search(text):
        channel = RouteChannel.AGENT
        return RouteDecision(channel=channel, reason="需要多步协调或外部状态操作", estimated_tokens=estimate_tokens(text, channel))
    if _FILE_OPERATION.search(text):
        channel = RouteChannel.DETERMINISTIC_SCRIPT
        return RouteDecision(channel=channel, reason="明确的文件转换或批处理", estimated_tokens=estimate_tokens(text, channel))
    if _RAG_OPERATION.search(text) or (
        has_authorized_documents and re.search(r"(?iu)(?:查|找|问答|总结|提取|分析)", text)
    ):
        channel = RouteChannel.RAG
        return RouteDecision(channel=channel, reason="需要从已授权资料检索事实", estimated_tokens=estimate_tokens(text, channel))
    channel = RouteChannel.DIRECT_LLM
    return RouteDecision(channel=channel, reason="无需外部状态的直接内容生成", estimated_tokens=estimate_tokens(text, channel))


def normalize_atomic_items(
    raw_items: list[str] | list[dict[str, Any]],
    *,
    has_authorized_documents: bool = False,
) -> list[AtomicWorkItem]:
    """Validate/route externally extracted work items and flatten subgraphs."""
    normalized: list[AtomicWorkItem] = []
    for position, raw in enumerate(raw_items, start=1):
        data = dict(raw) if isinstance(raw, dict) else {"instruction": str(raw)}
        instruction = re.sub(r"\s+", " ", str(data.get("instruction") or "")).strip()
        if not instruction:
            continue
        decision = route_atomic_instruction(instruction, has_authorized_documents=has_authorized_documents)
        raw_deps = data.get("dependencies") or []
        dependencies = [
            int(value) for value in raw_deps
            if isinstance(value, int) and 0 < value < position
        ]
        item = AtomicWorkItem(
            id=f"item-{position}",
            instruction=instruction[:2000],
            description=str(data.get("description") or instruction)[:500],
            estimated_type=decision.channel,
            dependencies=dependencies,
            estimated_tokens=decision.estimated_tokens,
            subtasks=list(data.get("subtasks") or [])[:12],
        )
        normalized.append(item)
    return normalized


def manifest_estimated_tokens(items: list[AtomicWorkItem]) -> int:
    return sum(item.estimated_tokens for item in items)
