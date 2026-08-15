"""质检层 —— 工具执行结果的质量校验.

当前分支：对 code 任务生成/修改的代码做 LLM 审查（不通过则打回重做，由
execute_dag 的重试机制兜底）；检索等其余任务默认放行。
LLM 审查失败时放行（质检不能阻塞主流程）。
"""

import json
import re
from abc import ABC, abstractmethod

from loguru import logger

from app.agents.orchestration.models import ReviewVerdict, TaskNode
from app.agents.orchestration.workers import WorkerContext
from app.core.config import settings


class ReviewHook(ABC):
    """质检接口."""

    @abstractmethod
    async def review(self, node: TaskNode, result: dict, ctx: WorkerContext) -> ReviewVerdict:
        ...


class NoopReviewer(ReviewHook):
    """框架占位：不校验，一律通过."""

    async def review(self, node: TaskNode, result: dict, ctx: WorkerContext) -> ReviewVerdict:
        return ReviewVerdict(approved=True)


class LlmReviewHook(ReviewHook):
    """LLM 质检：审查 code 任务的结果（与执行 agent 同模型）；失败自动放行."""

    _SYSTEM_PROMPT = (
        "你是代码质检员。根据用户指令审查生成/修改的代码是否满足要求："
        "功能是否实现、是否有明显错误。"
        "只输出 JSON：{\"approved\": true 或 false, \"feedback\": \"不通过时说明原因，通过时留空\"}"
    )

    async def review(self, node: TaskNode, result: dict, ctx: WorkerContext) -> ReviewVerdict:
        if not settings.AGENT_REVIEW_ENABLED:
            return ReviewVerdict(approved=True)
        # 小分支：只审查 code / code_writer 任务且结果带可审内容；其余一律放行
        if node.agent not in ("code", "code_writer"):
            return ReviewVerdict(approved=True)
        new_content = (result or {}).get("new_content")
        if not new_content:
            return ReviewVerdict(approved=True)

        instruction = (result or {}).get("instruction") or node.params.get("instruction") or ""
        path = (result or {}).get("path") or node.params.get("target_file") or ""
        prompt = (
            f"用户指令：{instruction}\n文件路径：{path}\n"
            f"生成的文件内容：\n{new_content[:12000]}"
        )
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_REVIEW

            llm = LLMClient()
            reply = await llm.chat(
                [
                    {"role": "system", "content": self._SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=4096,
                usage_user_id=ctx.user_id,
                usage_category=CATEGORY_REVIEW,
                reasoning_effort="low",
                disable_reasoning_effort=True,
                api_key=ctx.llm_api_key,
            )
            data = _extract_json(reply)
            if data:
                return ReviewVerdict(
                    approved=bool(data.get("approved")),
                    feedback=str(data.get("feedback") or ""),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Review] LLM 质检失败，默认放行: {}", exc)
        return ReviewVerdict(approved=True)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


REVIEWER: ReviewHook = LlmReviewHook()


def get_reviewer() -> ReviewHook:
    return REVIEWER
