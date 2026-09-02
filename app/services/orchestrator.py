"""多智能体编排服务 —— 会话上下文管理、记忆注入、智能体路由.

核心职责:
  1. 维护 Redis 中的短期对话上下文（最近 N 轮）
  2. 注入长期记忆关键事实
  3. 路由到对应场景的智能体
  4. 触发异步记忆提取
"""

import asyncio
import base64
import json
import mimetypes
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from app.agents.base import AgentContext
from app.agents.registry import AgentRegistry
from app.agents.skills.executor import run_skill_loop
from app.agents.orchestration.intent import requires_office_execution
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.llm import LLMClient
from app.core.llm_config import get_llm_config
from app.core.redis import get_redis
from app.models.db_models import Message
from app.services.speech import speech_to_text
from app.services.content_codec import normalize_content, serialize_content
from app.services.rag.query_rewriter import get_retrieval_queries
from app.services.rag.knowledge import search_user_knowledge
from app.services.rag.scope import RetrievalScope, has_memory_reference, route_chat_retrieval_scope
from app.services.scene_manager import get_scene_config, get_scene_knowledge_tags
from app.services.memory.retrieval import search_user_memories
from app.services.memory.privacy import resolve_decrypt_candidates
from app.services.conversation_memory import ConversationRecall, retrieve_conversation_recall
from app.services.prompts import get_base_system_prompt, get_prompt_content
from app.services.usage import CATEGORY_CHAT, CATEGORY_SKILL, CATEGORY_TITLE
from app.services.tool_output_projection import project_citations

# Redis Key 模板
CONTEXT_KEY = "conv:ctx:{conversation_id}"  # 会话上下文 (list of json)
SUMMARY_KEY = "conv:summary:{conversation_id}"  # 对话摘要（旧消息压缩，节省 token）
MEMORY_CACHE_KEY = "mem:user:{user_id}"  # 用户长期记忆缓存
EXTRACT_OFFSET_KEY = "mem:extract_offset:{conversation_id}"  # 记忆抽取进度（已抽取消息条数偏移）
TITLE_KEY = "conv:title:{conversation_id}"  # 会话标题

# 记忆注入常量
_TYPE_CN = {"identity": "身份", "preference": "偏好", "experience": "经历", "goal": "目标"}
_PRIVACY_RULES = (
    "\n\n隐私规则：\n"
    "1. [隐私] 标记的内容为脱敏描述，不得输出其背后的明文细节；\n"
    "2. 不得主动询问或推断用户的证件号、手机号、邮箱等精确身份信息；\n"
    "3. 仅当用户明确要求且后端已在本轮授权解密时，才可使用隐私明文；\n"
    "4. 涉及隐私的回复应模糊化（如\"您常用的联系方式\"而非直接复述）。"
)

# 多模态模型关键字（图片注入判断；qwen-vl-* / gpt-4o / gemini / llava 等）
_MULTIMODAL_KEYWORDS = ("vl", "vision", "4o", "gemini", "llava")
_MAX_IMAGE_BYTES = 15 * 1024 * 1024  # 单图 ≤ 15MB（base64 后约 20MB，贴近接口上限）
_MAX_IMAGES_PER_MESSAGE = 10
_WEB_DECISION_PROMPT = (
    "你是受控的联网工具选择器。web_search 只是可选的只读工具，不是所有问题的预处理器。\n"
    "仅当回答需要核实公开互联网中的外部事实、新闻、政策或用户明确要求搜索/网页来源时才调用。\n"
    "用户自己的任务状态、对话历史、上传附件、知识库内容、总结、改写、创作、计算和普通问答，绝不调用。\n"
    "‘今天/当前/实时’等词本身不是充分条件；例如‘我今天的待办’属于私有上下文，不应联网。\n"
    "不确定时不要调用，改为基于已有上下文回答或向用户澄清。"
)

# 这些词只用于显式联网意图的快速候选判断，绝不代表后端强制执行搜索。
_WEB_INTENT_KEYWORDS = (
    "联网", "网上搜", "网页搜索", "搜索网页", "检索公开资料", "查网页", "给我来源",
    "搜索新闻", "最新新闻", "公开资料", "web search", "search the web", "browse the web",
)

# 这些词只决定是否给模型展示受控工具目录，绝不决定是否联网。避免让一般
# 闲聊、文档问答、识图或语音转写失去原有逐字流式体验；文档问答已由 RAG
# 预处理完成。
_CHAT_TOOL_GRAPH_KEYWORDS = (
    "搜索", "查一下", "查查", "查找",
    "联网", "网上搜", "网页搜索", "搜索网页", "检索公开资料", "查网页", "给我来源",
    "搜索新闻", "最新新闻", "公开资料", "web search", "search the web", "browse the web",
    "现在几点", "当前时间", "当前日期", "几号", "星期几",
    "算一下", "计算", "加减乘除", "百分比", "表达式",
    "打开", "启动", "记事本", "notepad",
)
_CHAT_LOCAL_CONTEXT_MARKERS = (
    "上传的", "刚上传", "附件", "这个文件", "这份文件", "知识库", "我的资料",
    "会议纪要", "帮我总结", "帮我改写", "润色", "写一篇", "写个",
)
_CHAT_SMALLTALK_MARKERS = ("你好", "嗨", "哈喽", "在吗", "谢谢", "再见", "晚安", "早上好")

def _should_retrieve_chat_knowledge(
    content: str, attachments: list | None, retrieval_query: str | None
) -> bool:
    """兼容旧调用方：普通聊天的资料检索必须由 scope 路由授权。"""
    return route_chat_retrieval_scope(content, attachments, retrieval_query) == RetrievalScope.PERSONAL_KNOWLEDGE


def _needs_memory_fact_retrieval(content: str, retrieval_query: str | None) -> bool:
    """兼容旧调用方：仅检测历史引用，不承担跨库优先级裁决。"""
    return has_memory_reference(content, retrieval_query)


def _looks_like_chitchat(question: str) -> bool:
    """仅识别明显寒暄；不能把短的实质问题误判为无需工具。"""
    q = (question or "").strip()
    if not q:
        return True
    return len(q) <= 12 and any(k in q.casefold() for k in _CHAT_SMALLTALK_MARKERS)


def _needs_chat_tool_graph(content: str) -> bool:
    """普通聊天的模型工具选择入口。

    这里仅决定是否把有限工具目录交给模型，不决定联网。除寒暄、明确本地
    文本处理和附件问答外，实质性提问都可以进入受控 ToolNode；模型不调用
    工具时会直接回复，因而不会产生网络请求。
    """
    text = (content or "").strip().lower()
    if not text or _looks_like_chitchat(text):
        return False
    if any(marker in text for marker in _CHAT_LOCAL_CONTEXT_MARKERS):
        return False
    if any(keyword in text for keyword in _CHAT_TOOL_GRAPH_KEYWORDS):
        return True
    return bool(
        len(text) >= 16
        or any(marker in text for marker in ("?", "？", "什么", "为什么", "怎么", "如何", "多少", "吗", "是否"))
    )


def _append_web_search_preference(messages: list[dict]) -> list[dict]:
    """Expose an explicit UI preference to the model without bypassing ToolNode."""
    preference = (
        "\n\n[本轮工具偏好]\n用户已主动开启联网偏好。仅当本次回答确实需要公开网页来源时，"
        "才调用 web_search；不得因该偏好查询用户私有状态、附件或对话内容。"
    )
    enriched = [dict(message) for message in messages]
    for message in enriched:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += preference
            return enriched
    return [{"role": "system", "content": preference.strip()}] + enriched


def _append_chat_tool_contract(messages: list[dict], *, web_search_preferred: bool) -> list[dict]:
    """Add the tool-selection contract to the existing trusted system prompt."""
    contract = "\n\n[工具选择规则]\n" + _WEB_DECISION_PROMPT
    contract += (
        "\n精确算术、百分比或带括号表达式必须调用候选中的 calculator，不要自行心算。"
        "用户明确要求打开本机应用时，如候选中存在 open_app，必须发起该工具调用；"
        "客户端未连接、用户拒绝确认或工具失败时，应如实说明该结果，不得声称没有此能力。"
    )
    if web_search_preferred:
        contract += (
            "\n用户已主动开启联网偏好；这只提高公开来源检索的候选优先级，"
            "不改变上述私有信息和最小调用限制。"
        )
    enriched = [dict(message) for message in messages]
    for message in enriched:
        if message.get("role") == "system" and isinstance(message.get("content"), str):
            message["content"] += contract
            return enriched
    return [{"role": "system", "content": contract.strip()}] + enriched


def _requires_fresh_web_data(content: str) -> bool:
    """Deprecated compatibility helper; live data is selected by the model/tool gate.

    Kept for third-party imports during the migration, but intentionally never
    forces a network request based on lexical markers.
    """
    return False


def _chat_reasoning_effort(thinking_mode: str) -> str | None:
    """聊天推理强度：fast=low（快速回复），think=None（沿用用户全局设置，通常是 high）."""
    return None if thinking_mode == "think" else "low"


def _chat_model_override(scene: str, thinking_mode: str, llm_api_key: str | None) -> dict | None:
    """普通模式可选的思考模型覆盖。

    默认不覆盖 ``get_llm_config`` 的供应商选择，确保聊天、办公与 BYOK 使用
    同一份已生效配置。仅在管理员显式填写完整 ``CHAT_THINK_*`` 三元组时，
    ``think`` 档才切换到独立模型。
    """
    if scene != "chat" or llm_api_key:
        return None
    if thinking_mode == "think" and all(
        (settings.CHAT_THINK_MODEL, settings.CHAT_THINK_BASE_URL, settings.CHAT_THINK_API_KEY)
    ):
        return {
            "base_url": settings.CHAT_THINK_BASE_URL.rstrip("/"),
            "api_key": settings.CHAT_THINK_API_KEY,
            "model": settings.CHAT_THINK_MODEL,
            "timeout": 120.0,
        }
    return None


async def _get_chat_model_override(
    scene: str, thinking_mode: str, llm_api_key: str | None, user_id: str
) -> dict | None:
    """默认档位可以覆盖服务端默认值，但绝不能覆盖用户的模型选择。"""
    if scene != "chat" or llm_api_key:
        return None
    cfg = await get_llm_config(scene, user_id=user_id)
    if cfg.get("source") == "user":
        return None
    return _chat_model_override(scene, thinking_mode, llm_api_key)


# 角色提示词下的场景行为补充（角色负责性格，场景负责行为）
_SCENE_BEHAVIOR = {
    "chat": "",
    "office": "当前为办公模式：优先从用户知识库检索相关信息，回答时引用文档来源（📁 个人资料 / 🌐 公共知识库）。",
    "game": "当前为游戏模式：回复短小精悍，像队友一样；可结合攻略语料给出可执行建议。",
}

# 标题生成提示词：一句话概括对话主题
_TITLE_SYSTEM_PROMPT = (
    "你是对话标题生成助手。用一句话概括这段对话的主题，10~20 个字，"
    "不要引号、不要句号结尾、不要任何多余解释，只输出标题本身。"
)

# 纯文本生成不应被办公编排或某个专业 Skill 的格式覆盖。这个规则描述产品
# 行为而非某个文体的模板：保留用户的目标、约束和表达方式，按需直接交付。
_DIRECT_GENERATION_PROMPT = (
    "\n\n[直接生成]\n"
    "本轮不需要任何外部工具或文件操作。请直接完成用户要求的内容，"
    "以用户给出的题目、体裁、受众、语气、长度和格式为最高创作约束。"
    "不要把普通内容擅自改写成公文、通知或固定模板；未要求标题、称谓、落款、"
    "提纲或说明时不要额外添加。只交付用户需要的成品。"
)


class Orchestrator:
    """多智能体编排器.

    处理流程:
      用户消息 → 加载场景配置 → 加载 Redis 上下文 + 长期记忆 →
      RAG 检索知识库 → 拼接 Prompt → 调用 LLM → 保存消息 → 异步提取记忆
    """

    def __init__(self) -> None:
        self._llm = LLMClient()
        self._llm_started = False

    async def _ensure_llm_started(self) -> None:
        """懒启动 LLM 客户端（首次调用时初始化连接）."""
        if not self._llm_started:
            await self._llm.start()
            self._llm_started = True
            logger.debug("LLMClient 已启动 (provider={})", self._llm.provider)

    # ── 上下文管理 ──────────────────────────────────────

    async def get_context(self, conversation_id: str) -> list[dict]:
        """从 Redis 获取热窗口；缓存缺失时从 PostgreSQL 回填。

        Redis 只是一份带 TTL 的热缓存。注册用户的服务端热原文由 PostgreSQL
        保存到 token 滑动淘汰时为止，因此缓存过期或服务重启不能让最近上下文
        静默消失。游客/语音会话没有 UUID 持久化来源，保持 Redis-only。
        """
        r = get_redis()
        key = CONTEXT_KEY.format(conversation_id=conversation_id)
        raw = await r.lrange(key, 0, -1)
        if raw:
            return [json.loads(msg) for msg in raw]
        try:
            cid = uuid.UUID(str(conversation_id))
        except (ValueError, TypeError):
            return []
        try:
            async with async_session_factory() as session:
                rows = (
                    await session.execute(
                        select(Message)
                        .where(Message.conversation_id == cid)
                        .order_by(Message.created_at.asc(), Message.id.asc())
                    )
                ).scalars().all()
            restored: list[dict] = []
            used = 0
            for message in reversed(rows):
                content = normalize_content(message.content)
                cost = self._estimate_tokens(content)
                if restored and used + cost > settings.CONVERSATION_SUMMARY_KEEP_TOKENS:
                    break
                restored.append(
                    {
                        "role": message.role,
                        "content": content,
                        "timestamp": message.created_at.isoformat() if message.created_at else "",
                    }
                )
                used += cost
            restored.reverse()
            if restored:
                await r.rpush(key, *(json.dumps(item, ensure_ascii=False) for item in restored))
                await r.expire(key, 604800)
            return restored
        except Exception as exc:  # noqa: BLE001
            logger.warning("会话热窗口回填失败，继续空上下文: conv={} err={}", conversation_id, exc)
            return []

    async def append_context(self, conversation_id: str, message: dict) -> None:
        """追加一条消息到 Redis 热窗口。

        热窗口的实际淘汰由持久化后的后台 token 滑动任务负责。保留可选的
        轮次数安全阀仅兼容显式部署配置，默认关闭，避免短句聊天被提前截断。
        """
        r = get_redis()
        key = CONTEXT_KEY.format(conversation_id=conversation_id)
        await r.rpush(key, json.dumps(message, ensure_ascii=False))

        # 兼容部署方显式设置的轮次数安全阀；默认以 token 滑动窗口为准。
        if settings.CONVERSATION_CONTEXT_ROUNDS > 0:
            max_len = settings.CONVERSATION_CONTEXT_ROUNDS * 2
            current_len = await r.llen(key)
            if current_len > max_len:
                await r.ltrim(key, current_len - max_len, -1)

        # 设置过期时间（7天无活动自动清理）
        await r.expire(key, 604800)

    async def clear_context(self, conversation_id: str) -> None:
        """清除会话上下文."""
        r = get_redis()
        await r.delete(CONTEXT_KEY.format(conversation_id=conversation_id))
        await r.delete(SUMMARY_KEY.format(conversation_id=conversation_id))

    # ── 对话摘要（压缩短期记忆，节省 token） ────────────

    async def get_conversation_summary(self, conversation_id: str) -> str | None:
        """获取会话总摘要，Redis 为热缓存，数据库状态为回退来源。"""
        r = get_redis()
        cached = await r.get(SUMMARY_KEY.format(conversation_id=conversation_id))
        if cached:
            return cached
        try:
            from app.services.conversation_memory import get_conversation_global_summary

            async with async_session_factory() as session:
                summary = await get_conversation_global_summary(session, conversation_id)
            if summary:
                await r.set(SUMMARY_KEY.format(conversation_id=conversation_id), summary, ex=604800)
            return summary or None
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取会话总摘要回退失败: {}", exc)
            return None

    async def save_conversation_summary(self, conversation_id: str, summary: str) -> None:
        """保存会话摘要（7 天 TTL，与上下文一致）."""
        r = get_redis()
        if summary:
            await r.set(SUMMARY_KEY.format(conversation_id=conversation_id), summary, ex=604800)

    async def _generate_summary(
        self, prev_summary: str | None, messages: list[dict], user_id: str
    ) -> str:
        """用 qwen-turbo 生成/接力对话"剧情梗概"（轻量低成本）.

        旧的 10 万 token 原始对话 → 约 5000 token 的中文回顾（10:1 压缩），
        保留用户偏好与关键事实、结论/约定、未完成的任务。
        """
        parts: list[str] = []
        if prev_summary:
            parts.append(f"[之前的剧情梗概]\n{prev_summary}")
        for m in messages:
            speaker = "用户" if m.get("role") == "user" else "助手"
            parts.append(f"{speaker}: {normalize_content(m.get('content') or '')}")
        dialog = "\n".join(parts)

        system_prompt = (
            "你是对话记忆整理助手。把一段较长的对话浓缩成中文\"剧情梗概\""
            "（类似周报/剧情回顾），结构清晰、信息密度高。"
            "必须保留：用户的偏好与重要事实、双方达成的结论与约定、未完成的任务/待办、"
            "重要时间点与关键转折。"
            "若提供了之前的梗概，新梗概要继承其中仍然重要的信息，避免丢失。"
            "只输出梗概本身，不要任何解释或前缀。"
        )
        try:
            summary = await LLMClient().chat(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"对话内容：\n\n{dialog}"},
                ],
                model=settings.QWEN_TURBO_MODEL,
                base_url=settings.QWEN_BASE_URL,
                api_key=settings.QWEN_API_KEY,
                timeout=120,
                temperature=0.3,
                max_tokens=8192,
                usage_user_id=user_id,
                usage_category="summary",
                disable_reasoning_effort=True,
            )
            return summary[: settings.CONVERSATION_SUMMARY_MAX_CHARS]
        except Exception as e:
            # 摘要失败不阻塞对话：保留旧摘要，下次触发再试
            logger.warning("对话摘要生成失败: {}", e)
            return None

    async def _generate_summary_chunked(
        self, prev_summary: str | None, old_msgs: list[dict], user_id: str
    ) -> str | None:
        """分批接力生成梗概：单次 LLM 输入受限时按 chunk 依次压缩，前一轮输出作为下一轮上下文."""
        if not old_msgs:
            return prev_summary or ""
        chunk_budget = settings.CONVERSATION_SUMMARY_CHUNK_TOKENS
        chunks: list[list[dict]] = []
        current: list[dict] = []
        used = 0
        for m in old_msgs:
            cost = self._estimate_tokens(normalize_content(m.get("content") or ""))
            if current and used + cost > chunk_budget:
                chunks.append(current)
                current = []
                used = 0
            current.append(m)
            used += cost
        if current:
            chunks.append(current)

        summary = prev_summary or ""
        for chunk in chunks:
            summary = await self._generate_summary(summary, chunk, user_id)
            if summary is None:
                # 任一分批失败：保留旧摘要与原始记录，本次不裁剪，下次触发再试
                return None
        return summary

    async def _maybe_summarize_context(
        self, conversation_id: str, user_id: str, scene: str = "chat"
    ) -> None:
        """兼容语音会话的 Redis 滑动窗口维护。

        普通文本聊天由持久化后的后台任务统一完成“段摘要 → PostgreSQL/Redis
        同步淘汰”，以保证被删除原文已有 L1 摘要。这里不做数据库删除。
        """
        history = await self.get_context(conversation_id)
        total = sum(self._estimate_tokens(normalize_content(m.get("content") or "")) for m in history)
        if total < settings.CONVERSATION_SUMMARY_TRIGGER_TOKENS:
            return

        keep_tokens = settings.CONVERSATION_SUMMARY_KEEP_TOKENS
        # 从最新往回累计保留 token 预算内的消息。
        kept: list[dict] = []
        used = 0
        for msg in reversed(history):
            cost = self._estimate_tokens(normalize_content(msg.get("content") or ""))
            if kept and used + cost > keep_tokens:
                break
            kept.append(msg)
            used += cost
        if len(kept) == len(history):
            return
        old_msgs = history[: len(history) - len(kept)]

        prev_summary = await self.get_conversation_summary(conversation_id)
        summary = await self._generate_summary_chunked(prev_summary, old_msgs, user_id)
        if not summary:
            # 摘要生成失败：不裁剪上下文，下次触发再试
            return
        await self.save_conversation_summary(conversation_id, summary)

        # 语音会话没有 PostgreSQL 原文，保持旧的 Redis-only 压缩行为。
        r = get_redis()
        key = CONTEXT_KEY.format(conversation_id=conversation_id)
        await r.ltrim(key, -len(kept), -1)
        await r.expire(key, 604800)

    # ── 记忆抽取触发（异步，Celery）──────────────────────

    async def _submit_unextracted(
        self, conversation_id: str, user_id: str, history: list[dict], stop: int | None = None
    ) -> None:
        """把尚未做过记忆抽取的消息批量入队（按偏移量幂等推进）."""
        try:
            uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            return  # 游客/无效用户不抽取
        r = get_redis()
        key = EXTRACT_OFFSET_KEY.format(conversation_id=conversation_id)
        try:
            offset = int(await r.get(key) or 0)
        except (TypeError, ValueError):
            offset = 0
        end = len(history) if stop is None else min(stop, len(history))
        if offset >= end:
            return
        batch = [m for m in history[offset:end] if m.get("content")]
        if batch:
            try:
                from celery_app.tasks import extract_memories

                extract_memories.delay(user_id, conversation_id, batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("记忆抽取入队失败: {}", exc)
                return
        await r.set(key, str(end))
        await r.expire(key, 604800)

    async def _maybe_extract_memories(self, conversation_id: str, user_id: str) -> None:
        """对话消息攒满一批后异步抽取长期记忆（摘要路径之外的兜底）."""
        try:
            uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            return  # 游客/无效用户不抽取
        r = get_redis()
        key = EXTRACT_OFFSET_KEY.format(conversation_id=conversation_id)
        try:
            offset = int(await r.get(key) or 0)
        except (TypeError, ValueError):
            offset = 0
        history = await self.get_context(conversation_id)
        if len(history) - offset < settings.MEMORY_EXTRACTION_MIN_MESSAGES:
            return
        await self._submit_unextracted(conversation_id, user_id, history)

    async def get_conversation_title(self, conversation_id: str) -> str | None:
        """获取会话标题（Redis，可能为空）."""
        r = get_redis()
        return await r.get(TITLE_KEY.format(conversation_id=conversation_id))

    async def save_conversation_title(self, conversation_id: str, title: str) -> None:
        """保存会话标题（7 天 TTL，与上下文一致）."""
        r = get_redis()
        await r.set(TITLE_KEY.format(conversation_id=conversation_id), title, ex=604800)

    async def _generate_title(
        self,
        content: str,
        user_id: str,
        llm_api_key: str | None = None,
    ) -> str:
        """用大模型生成会话标题（轻量调用，首条消息时与回复并行）."""
        try:
            reply = await self._llm.chat(
                [
                    {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                max_tokens=32,
                usage_user_id=user_id,
                usage_category=CATEGORY_TITLE,
                api_key=llm_api_key,
            )
            title = reply.strip().strip('"“”').strip()
            return title[:30]
        except Exception as e:
            logger.warning("会话标题生成失败: {}", e)
            return ""

    # ── 记忆注入（画像常驻 + 事实按需召回）──────────────

    async def get_user_profile(self, user_id: str) -> dict | None:
        """获取用户画像（Redis 缓存 1 小时 → memory_profile 表）."""
        if not settings.MEMORY_PROFILE_INJECT_ENABLED:
            return None
        r = get_redis()
        key = MEMORY_CACHE_KEY.format(user_id=user_id)
        cached = await r.get(key)
        if cached:
            try:
                return json.loads(cached)
            except (ValueError, TypeError):
                pass
        try:
            async with async_session_factory() as session:
                from app.models.db_models import MemoryProfile

                profile = await session.get(MemoryProfile, uuid.UUID(str(user_id)))
                if not profile:
                    return None
                data = dict(profile.profile or {})
                data["version"] = profile.version
                data["updated_at"] = profile.updated_at.isoformat() if profile.updated_at else None
                await r.set(key, json.dumps(data, ensure_ascii=False), ex=3600)
                return data
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取用户画像失败: {}", exc)
            return None

    async def retrieve_memory_facts(self, user_id: str, query: str, top_k: int | None = None) -> list[dict]:
        """按当前问题混合检索用户记忆事实（L1 只含占位符）；命中后异步强化."""
        if not query or not query.strip():
            return []
        try:
            async with async_session_factory() as session:
                facts = await search_user_memories(
                    session, user_id, query, top_k=top_k or settings.MEMORY_FACT_TOP_K
                )
                # 向量低分命中宁可不注入；关键词命中没有 similarity 时仍保留，
                # 以避免“用户明确提到文件名/项目名却被向量误伤”。
                facts = [
                    item
                    for item in facts
                    if item.get("similarity") is None
                    or float(item.get("similarity") or 0) >= settings.MEMORY_FACT_MIN_VECTOR_SIMILARITY
                ]
                ids = [str(f["memory_id"]) for f in facts]
                if ids:
                    try:
                        from celery_app.tasks import touch_memories

                        touch_memories.delay(ids)
                    except Exception:  # noqa: BLE001
                        pass
                return facts
        except Exception as exc:  # noqa: BLE001
            logger.warning("记忆检索失败: {}", exc)
            return []

    async def get_memory_context(
        self,
        user_id: str,
        query: str = "",
        retrieval_query: str | None = None,
        thinking_mode: str = "fast",
        retrieve_facts: bool = True,
    ) -> tuple[dict | None, list[dict]]:
        """注入内容 = 画像（常驻） + 与当前问题相关的事实（按需召回）."""
        profile_task = asyncio.create_task(self.get_user_profile(user_id))
        facts_task = (
            asyncio.create_task(
                self.retrieve_memory_facts(
                    user_id,
                    retrieval_query or query,
                    top_k=5 if thinking_mode == "think" else 3,
                )
            )
            if retrieve_facts and _needs_memory_fact_retrieval(query, retrieval_query)
            else None
        )
        profile = await profile_task
        facts = await facts_task if facts_task is not None else []
        return profile, facts

    async def get_conversation_recall_context(
        self, conversation_id: str, query: str, thinking_mode: str
    ) -> ConversationRecall:
        """按档位加载段摘要与少量原文，不让长历史污染普通聊天上下文。"""
        try:
            async with async_session_factory() as session:
                return await retrieve_conversation_recall(session, conversation_id, query, thinking_mode)
        except Exception as exc:  # noqa: BLE001
            logger.debug("会话历史回捞失败，继续无回捞回复: {}", str(exc)[:160])
            return ConversationRecall()

    # ── 消息处理主流程 ──────────────────────────────────

    async def handle_message(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        scene: str = "chat",
        local_mode: bool = False,
        retrieval_query: str | None = None,
        attachments: list | None = None,
        office_docs: list[dict] | None = None,
        web_search_enabled: bool = False,
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        reply_style: str | None = None,
        user_role: str = "user",
    ) -> dict:
        """处理用户消息的核心流程（阻塞版，供旧接口/降级路径使用）."""
        transcript = await self._resolve_transcript(content, attachments)
        content = transcript

        # 本地模式：仅记录，不生成回复（PC端已处理）
        if local_mode:
            return {
                "message_id": str(uuid.uuid4()),
                "content": "",
                "citations": [],
                "scene": scene,
                "local_mode": True,
                "title": "",
                "transcript": transcript,
            }

        # 办公模式的知识来源只能来自 DAG 节点（office_doc / retrieval 等）。
        # 在这里预检索会造成两个问题：无关上下文可能干扰任务规划，且即便
        # 最终走的是 get_datetime 之类系统工具，也会向前端泄露无关引用。
        prep = await self._prepare_chat(
            user_id,
            conversation_id,
            content,
            scene,
            retrieval_query,
            attachments,
            reply_style,
            retrieve_knowledge=scene != "office",
            thinking_mode=thinking_mode,
        )
        image_uris = await self._load_image_data_uris(user_id, attachments)

        office_execution = scene == "office" and requires_office_execution(content, office_docs)
        logger.info(
            "办公请求路由判定: scene={} execution={} content={}",
            scene,
            office_execution,
            content[:120].replace("\n", " "),
        )
        if scene == "office" and not office_execution:
            # 办公模式也可以是纯文本创作/问答。不要为此生成虚假的任务、工具
            # 步骤或公文模板；直接使用完整对话上下文获得正常 C 端生成体验。
            prep["messages"][0]["content"] += _DIRECT_GENERATION_PROMPT
            reply = await self._call_llm_auto(
                user_id, prep["messages"], scene, image_uris, content, prep["citations"],
                conversation_id, llm_api_key, thinking_mode=thinking_mode,
                force_web_search=web_search_enabled,
            )
            title = await self.get_conversation_title(conversation_id)
            if prep["is_first"] and not title:
                title = await self._generate_title(content, user_id, llm_api_key)
                if title:
                    await self.save_conversation_title(conversation_id, title)
            await self._finalize_reply(conversation_id, user_id, reply, scene)
            return {
                "message_id": str(uuid.uuid4()), "content": reply,
                "citations": project_citations(prep["citations"]), "scene": scene, "local_mode": False,
                "title": title or "", "transcript": transcript, "steps": [],
            }

        if scene == "office":
            reply, office_steps, office_citations = await self._run_office_job(
                user_id,
                conversation_id,
                content,
                office_docs or [],
                llm_api_key,
                user_role,
            )
            prep["citations"].extend(office_citations)
            title = await self.get_conversation_title(conversation_id)
            if prep["is_first"] and not title:
                title = await self._generate_title(content, user_id, llm_api_key)
                if title:
                    await self.save_conversation_title(conversation_id, title)
            await self._finalize_reply(conversation_id, user_id, reply, scene)
            return {
                "message_id": str(uuid.uuid4()),
                "content": reply,
                "citations": project_citations(prep["citations"]),
                "scene": scene,
                "local_mode": False,
                "title": title or "",
                "transcript": transcript,
                "steps": office_steps,
            }

        # 调用 LLM：工具调用（模型自主决定联网）+ 最终回复
        title = await self.get_conversation_title(conversation_id)
        if prep["is_first"] and not title:
            # 首条消息：回复与标题生成并行（大模型"阅读的同时"总结）
            reply_task = asyncio.create_task(
                self._call_llm_auto(
                    user_id,
                    prep["messages"],
                    scene,
                    image_uris,
                    content,
                    prep["citations"],
                    conversation_id,
                    llm_api_key,
                    thinking_mode=thinking_mode,
                    force_web_search=web_search_enabled,
                )
            )
            title_task = asyncio.create_task(self._generate_title(content, user_id, llm_api_key))
            reply, title = await asyncio.gather(reply_task, title_task)
            if title:
                await self.save_conversation_title(conversation_id, title)
        else:
            reply = await self._call_llm_auto(
                user_id, prep["messages"], scene, image_uris, content, prep["citations"], conversation_id, llm_api_key,
                thinking_mode=thinking_mode,
                force_web_search=web_search_enabled,
            )

        # 普通模式短句回复：把整段回复切成多条短句（存储时合并为一次交互）
        segments = (
            self._split_short_reply(reply)
            if reply_style == "short" and scene == "chat"
            else None
        )
        # 保存助手回复 + 摘要 + 记忆抽取（办公模式不做长期记忆）
        await self._finalize_reply(conversation_id, user_id, reply, scene, segments=segments)

        return {
            "message_id": str(uuid.uuid4()),
            "content": reply,
            "citations": project_citations(prep["citations"]),
            "scene": scene,
            "local_mode": False,
            "title": title or "",
            "transcript": transcript,
            "segments": segments,
        }

    async def handle_message_stream(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        scene: str = "chat",
        local_mode: bool = False,
        retrieval_query: str | None = None,
        attachments: list | None = None,
        office_docs: list[dict] | None = None,
        web_search_enabled: bool = False,
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        reply_style: str | None = None,
        user_role: str = "user",
    ):
        """流式处理用户消息：准备流程同 handle_message，LLM 走工具调用 + SSE 流式.

        Yields: {"type": "delta", "content": ...} / {"type": "done", ...}
        """
        transcript = await self._resolve_transcript(content, attachments)
        content = transcript

        if local_mode:
            yield {
                "type": "done",
                "message_id": str(uuid.uuid4()),
                "content": "",
                "citations": [],
                "scene": scene,
                "title": "",
            }
            return

        prep = await self._prepare_chat(
            user_id,
            conversation_id,
            content,
            scene,
            retrieval_query,
            attachments,
            reply_style,
            retrieve_knowledge=scene != "office",
            thinking_mode=thinking_mode,
        )
        image_uris = await self._load_image_data_uris(user_id, attachments)
        message_id = str(uuid.uuid4())
        title = await self.get_conversation_title(conversation_id)

        # 首条消息：标题生成与回复流并行
        title_task = None
        if prep["is_first"] and not title:
            title_task = asyncio.create_task(self._generate_title(content, user_id, llm_api_key))

        full_text = ""
        atomic_steps: dict[str, dict] = {}
        office_execution = scene == "office" and requires_office_execution(content, office_docs)
        logger.info(
            "办公请求路由判定: scene={} execution={} content={}",
            scene,
            office_execution,
            content[:120].replace("\n", " "),
        )
        if scene == "office" and not office_execution:
            prep["messages"][0]["content"] += _DIRECT_GENERATION_PROMPT
        stream = (
            self._stream_office_job(
                user_id,
                conversation_id,
                content,
                office_docs or [],
                llm_api_key,
                prep["citations"],
                user_role,
            )
            if office_execution
            else self._stream_llm_auto(
                user_id, prep["messages"], scene, image_uris, content, prep["citations"], conversation_id, llm_api_key,
                thinking_mode=thinking_mode,
                force_web_search=web_search_enabled,
            )
        )
        async for evt in stream:
            if evt["type"] == "delta":
                full_text += evt["content"]
            elif evt["type"] == "step":
                step = evt.get("step") or {}
                if step.get("id"):
                    atomic_steps[str(step["id"])] = {
                        **atomic_steps.get(str(step["id"]), {}),
                        **step,
                    }
            yield evt

        if title_task is not None:
            try:
                # A title is cosmetic.  It starts concurrently with the main
                # response, but must never hold the terminal SSE event hostage
                # when a provider is slow (this used to add tens of seconds to
                # an otherwise completed first office reply).
                title = await asyncio.wait_for(asyncio.shield(title_task), timeout=2.0)
                if title:
                    await self.save_conversation_title(conversation_id, title)
            except asyncio.TimeoutError:
                title_task.cancel()
                await asyncio.gather(title_task, return_exceptions=True)
                logger.info("会话标题生成超时，已跳过以完成回复: {}", conversation_id[:12])
            except Exception as exc:  # noqa: BLE001
                # Title generation is cosmetic. Do not turn a fully streamed
                # answer into an SSE error when Redis/LLM is briefly unavailable.
                logger.warning("生成或保存会话标题失败（回复继续）：{}", str(exc)[:200])

        # 普通模式短句回复：把整段回复切成多条短句（存储时合并为一次交互）
        segments = (
            self._split_short_reply(full_text)
            if reply_style == "short" and scene == "chat"
            else None
        )
        # 保存助手回复 + 摘要 + 记忆抽取（办公模式不做长期记忆）
        try:
            await self._finalize_reply(conversation_id, user_id, full_text, scene, segments=segments)
        except Exception as exc:  # noqa: BLE001
            # Context/memory persistence is best-effort after delivery.  The
            # client must receive ``done`` rather than a misleading interrupt.
            logger.warning("回复上下文持久化失败（不影响已完成回复）：{}", str(exc)[:200])

        yield {
            "type": "done",
            "message_id": message_id,
            "content": full_text,
            "citations": project_citations(prep["citations"]),
            "scene": scene,
            "title": title or "",
            "segments": segments,
            "steps": list(atomic_steps.values()),
        }

    @staticmethod
    def _job_step(node) -> dict:
        result = node.result or {}
        raw_status = node.status.value if hasattr(node.status, "value") else str(node.status)
        if raw_status in {"ready", "pending"}:
            display_status = "pending"
        elif raw_status in {"running", "retrying"}:
            display_status = "running"
        elif raw_status == "completed":
            display_status = "completed"
        else:
            display_status = "failed"
        return {
            "id": node.id,
            "title": node.name or result.get("step_title") or node.agent,
            "status": display_status,
            "runtime_status": raw_status,
            "tool": result.get("tool") or node.params.get("preferred_tool") or node.agent,
            "output": str(result.get("content") or result.get("output") or "")[:1000],
            "error": node.error,
            "depends_on": list(node.depends_on),
            "resource_claims": [c.model_dump() for c in node.resource_claims],
            "effect_status": node.effect_status,
            "started_at": node.started_at,
            "completed_at": node.completed_at,
            "duration_ms": (
                max(0, int((node.completed_at - node.started_at) * 1000))
                if node.started_at is not None and node.completed_at is not None
                else None
            ),
        }

    @classmethod
    async def _logical_plan_steps(cls, user_id: str, routing: dict) -> list[dict]:
        """从持久化逻辑计划恢复全部节点的展示状态。

        ``Job.nodes`` 只保存当前滚动执行窗口。前沿推进后，已完成节点会从
        Job 快照移除；SSE 因此必须以逻辑计划中的状态记录补齐历史节点。结果
        正文仍由运行中的节点 delta 单独推送，避免重复把完整输出写回聊天流。
        """
        pointer = routing.get("logical_plan") if isinstance(routing, dict) else None
        plan_id = str((pointer or {}).get("plan_id") or "")
        if not plan_id:
            return []

        try:
            from app.agents.orchestration.logical_plan import load_logical_plan
            from app.agents.orchestration.models import TaskNode, TaskStatus

            plan = await load_logical_plan(user_id, plan_id)
            if not plan:
                return []
            records = plan.get("nodes") or {}
            steps: list[dict] = []
            for node_id in plan.get("order") or []:
                record = records.get(node_id)
                if not isinstance(record, dict) or not isinstance(record.get("node"), dict):
                    continue
                node = TaskNode.model_validate(record["node"])
                raw_status = str(record.get("status") or TaskStatus.PENDING.value)
                try:
                    node.status = TaskStatus(raw_status)
                except ValueError:
                    node.status = TaskStatus.FAILED
                node.error = str(record.get("error") or "") or None
                node.error_code = str(record.get("error_code") or "") or None
                node.effect_status = record.get("effect_status")
                # 完整结果仅存于 result_ref，不能在 SSE 状态同步中重复读取或发送。
                node.result = None
                steps.append(cls._job_step(node))
            return steps
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取逻辑计划 SSE 状态失败（回退当前窗口）：{}", str(exc)[:200])
            return []

    @staticmethod
    def _job_citations(job) -> list[dict]:
        out: list[dict] = []
        for node in job.nodes:
            result = node.result or {}
            metadata = result.get("tool_metadata") or result.get("metadata") or {}
            if isinstance(metadata, dict) and isinstance(metadata.get("citations"), list):
                out.extend(metadata["citations"])
        return out

    @staticmethod
    def _job_answer(job) -> str:
        result = job.result or {}
        answer = str(result.get("final_answer") or result.get("answer") or "").strip()
        if answer:
            return answer
        if result.get("type") == "clarification":
            return str(result.get("question") or "请补充任务信息。")
        if result.get("type") == "planning_error":
            return str(result.get("message") or job.error or "办公任务规划失败，请稍后重试。")
        blocks = []
        for node in job.nodes:
            node_result = node.result or {}
            content = str(node_result.get("content") or node_result.get("output") or "").strip()
            if content:
                blocks.append(content)
        if blocks:
            return "\n\n".join(blocks)
        failed = next((node for node in job.nodes if node.error), None)
        if failed and failed.error:
            return str(failed.error)
        return str(job.error or "办公任务未能完成，请检查失败步骤后重试。")

    async def _run_office_job(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        office_docs: list[dict],
        llm_api_key: str | None,
        user_role: str = "user",
    ) -> tuple[str, list[dict], list[dict]]:
        from app.agents.orchestration import orchestrator as agent_orchestrator
        from app.agents.orchestration.models import JobStatus

        job = await agent_orchestrator.submit_job(
            user_id,
            content,
            "office",
            conversation_id,
            llm_api_key=llm_api_key,
            office_docs=office_docs,
            user_role=user_role,
        )
        terminal = {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }
        while job.status not in terminal:
            await asyncio.sleep(0.15)
            job = await agent_orchestrator.get_job(job.job_id) or job
        return self._job_answer(job), [self._job_step(n) for n in job.nodes], self._job_citations(job)

    async def _stream_office_job(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        office_docs: list[dict],
        llm_api_key: str | None,
        citations: list[dict],
        user_role: str = "user",
    ):
        from app.agents.orchestration import orchestrator as agent_orchestrator
        from app.agents.orchestration.models import JobStatus

        job = await agent_orchestrator.submit_job(
            user_id,
            content,
            "office",
            conversation_id,
            llm_api_key=llm_api_key,
            office_docs=office_docs,
            user_role=user_role,
        )
        yield {
            "type": "job",
            "job_id": job.job_id,
            "conversation_id": conversation_id,
            "created_at": job.created_at,
        }
        last: dict[str, tuple] = {}
        last_plan_revision = 0
        terminal = {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.INTERRUPTED,
        }
        missing_snapshots = 0
        output_cursor = 0
        streamed_answer = ""
        try:
            while True:
                routing = getattr(job, "routing", None) or {}
                plan_revision = int(routing.get("plan_revision") or 1)
                if plan_revision > last_plan_revision:
                    if last_plan_revision:
                        reason = str(
                            routing.get("plan_change_reason")
                            or "我根据刚才的执行结果调整了后续方法。"
                        )
                        yield {
                            "type": "step",
                            "job_id": job.job_id,
                            "step": {
                                "id": f"plan-revision-{plan_revision}",
                                "title": "调整执行计划",
                                "status": "completed",
                                "runtime_status": "completed",
                                "tool": "planner",
                                "output": reason[:500],
                                "error": None,
                                "depends_on": [],
                                "resource_claims": [],
                                "effect_status": None,
                                "started_at": None,
                                "completed_at": None,
                                "duration_ms": None,
                            },
                        }
                    last_plan_revision = plan_revision
                logical_steps = await self._logical_plan_steps(user_id, routing)
                # 逻辑计划负责旧窗口的最终状态；当前窗口覆盖其状态和输出，保证
                # 正在运行节点的实时信息与文本流仍来自最新 Job 快照。
                steps_by_id = {step["id"]: step for step in logical_steps}
                for node in job.nodes:
                    step = self._job_step(node)
                    steps_by_id[step["id"]] = step
                for step in steps_by_id.values():
                    signature = (step["status"], step["error"], step["output"], step["effect_status"])
                    if last.get(step["id"]) != signature:
                        last[step["id"]] = signature
                        yield {"type": "step", "job_id": job.job_id, "step": step}
                # Text-producing office skills publish deltas independently of
                # status snapshots. Drain them while the node is still running.
                from app.services.office_stream import read_deltas

                deltas, output_cursor = await read_deltas(job.job_id, output_cursor)
                for delta in deltas:
                    text = str(delta.get("content") or "")
                    if text:
                        streamed_answer += text
                        yield {"type": "delta", "content": text, "job_id": job.job_id}
                if job.status in terminal:
                    break
                await asyncio.sleep(0.15)
                current = await agent_orchestrator.get_job(job.job_id)
                if current is None:
                    missing_snapshots += 1
                    # 短暂 Redis 抖动可以恢复；连续缺失说明任务状态已丢失。
                    # 不能继续用陈旧 running 快照无限 SSE，占住会话和用户额度。
                    if missing_snapshots >= 3:
                        # 流式请求不能只抛异常：前端已经收到 job 事件，抛异常会
                        # 被 Electron 转成“回复中断”，掩盖真正原因。构造一个本地
                        # 失败终态并收敛 SSE；下次 GET 仍会返回 404，提示状态库需
                        # 检查，但当前气泡至少能显示可行动的原因。
                        job.status = JobStatus.FAILED
                        job.error = "办公任务状态已丢失，请检查 Redis/后端实例是否使用同一状态库后重新提交。"
                        job.updated_at = time.time()
                        for node in job.nodes:
                            if node.status not in terminal:
                                node.status = getattr(type(node.status), "FAILED", "failed")
                                node.error = "任务状态已丢失"
                        state_step = (
                            self._job_step(job.nodes[0])
                            if job.nodes
                            else {
                                "id": "state",
                                "title": "任务状态",
                                "status": "failed",
                                "runtime_status": "failed",
                                "tool": "state_store",
                                "output": "",
                                "error": job.error,
                                "depends_on": [],
                                "resource_claims": [],
                                "effect_status": None,
                                "started_at": None,
                                "completed_at": None,
                                "duration_ms": None,
                            }
                        )
                        state_step.update(
                            status="failed",
                            runtime_status="failed",
                            error=job.error,
                        )
                        yield {
                            "type": "step",
                            "job_id": job.job_id,
                            "step": state_step,
                        }
                        break
                    continue
                missing_snapshots = 0
                job = current
        except asyncio.CancelledError:
            # SSE 连接属于客户端展示生命周期。切换会话、关闭窗口或网络抖动
            # 都不代表用户明确终止任务；真实终止只能走 /jobs/{id}/cancel。
            logger.info("办公任务流断开，任务继续后台执行: {}", job.job_id)
            raise
        citations.extend(self._job_citations(job))
        answer = self._job_answer(job)
        if answer and not streamed_answer:
            yield {"type": "delta", "content": answer}
        elif answer and not answer.startswith(streamed_answer):
            yield {"type": "delta", "content": "\n\n" + answer}
        elif answer and len(answer) > len(streamed_answer):
            yield {"type": "delta", "content": answer[len(streamed_answer):]}

    # ── 消息处理公共流程 ────────────────────────────────

    async def _resolve_transcript(self, content: str, attachments: list | None) -> str:
        """语音附件 → Whisper 转写 + 纠错；无论是否带文字，都把转写文本拼进消息."""
        parts = []
        for att in attachments or []:
            if isinstance(att, dict) and att.get("type") == "audio" and att.get("url"):
                t = await speech_to_text(str(att["url"]))
                if t:
                    parts.append(t)
        if not parts:
            return content or ""
        head = (content or "").strip()
        if head:
            return f"{head}\n\n【语音转写】\n" + "\n\n".join(parts)
        return "\n\n".join(parts)

    async def _prepare_chat(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        scene: str,
        retrieval_query: str | None,
        attachments: list | None,
        reply_style: str | None = None,
        retrieve_knowledge: bool = True,
        thinking_mode: str = "fast",
    ) -> dict:
        """LLM 调用前的公共准备：上下文、长期记忆、消息构建、RAG 检索、隐私解密门."""
        started_at = time.perf_counter()
        # 1. 保存用户消息到上下文
        is_first = len(await self.get_context(conversation_id)) == 0
        user_msg = {"role": "user", "content": content, "timestamp": datetime.now(timezone.utc).isoformat()}
        await self.append_context(conversation_id, user_msg)
        # 新会话首条消息：立即异步抽取（身份类事实（如名字/职业）往往出现在开场白，不等攒批）。
        # 办公模式不建长期记忆，跳过抽取。
        if is_first and scene != "office":
            await self._submit_unextracted(conversation_id, user_id, [user_msg])

        # 2. 上下文 + 长期记忆（画像常驻 + 事实按需召回）
        history = await self.get_context(conversation_id)
        summary = await self.get_conversation_summary(conversation_id)
        conversation_recall = ConversationRecall(global_summary=summary or "")
        if scene == "office":
            # 办公模式：无长期记忆注入，只靠短期窗口保证当次任务连贯
            profile, memory_facts = None, []
            retrieval_scope = RetrievalScope.NONE
        else:
            # 一个普通问题默认只读一个语料 scope。资料引用优先于历史引用，
            # 防止文件问答被用户画像/旧任务事实污染；跨 scope 组合只能由
            # 显式 DAG 节点完成，不能在聊天预处理阶段静默拼接。
            retrieval_scope = (
                route_chat_retrieval_scope(content, attachments, retrieval_query)
                if scene == "chat"
                else RetrievalScope.PERSONAL_KNOWLEDGE
            )
            profile, memory_facts = await self.get_memory_context(
                user_id,
                query=content,
                retrieval_query=retrieval_query,
                thinking_mode=thinking_mode,
                retrieve_facts=retrieval_scope == RetrievalScope.MEMORY,
            )
            conversation_recall = await self.get_conversation_recall_context(
                conversation_id, content, thinking_mode
            )
            summary = conversation_recall.global_summary or summary

        # 3. 消息列表（System Prompt + 画像 + 记忆 + 摘要 + 历史 + 当前提问）
        system_prompt = await self._get_system_prompt(user_id, scene)
        if reply_style == "short" and scene == "chat":
            system_prompt += (
                "\n\n[回复风格]\n"
                "请用多条短句分段回复用户：每句话一个意思、一句一行，"
                "像聊天消息一样自然（豆包式短句风格），不要写成长段落。"
                "每段尽量简短（一般不超过 30 字），整体控制在 3-8 段，避免机械逐字断句。"
            )
        messages = self._build_messages(
            scene,
            profile,
            memory_facts,
            history,
            content,
            summary,
            system_prompt=system_prompt,
            thinking_mode=thinking_mode,
            conversation_recall=conversation_recall,
        )

        # 4. RAG 知识库检索（按场景过滤空间标签）。办公模式由 DAG
        # 的显式检索节点决定是否查询，不能在进入规划前隐式检索。
        citations: list[dict] = []
        should_retrieve = retrieve_knowledge and retrieval_scope == RetrievalScope.PERSONAL_KNOWLEDGE
        if should_retrieve:
            knowledge_tags = get_scene_knowledge_tags(scene)
            search_queries = await get_retrieval_queries(
                content,
                retrieval_query,
                scene,
                user_id,
                thinking_mode=thinking_mode,
            )
            rag_context, citations = await self._retrieve_knowledge(
                user_id,
                search_queries[0] if search_queries else content,
                knowledge_tags,
                thinking_mode=thinking_mode,
                query_variants=search_queries,
            )
            if rag_context:
                messages[-1]["content"] = f"参考以下知识库内容回答用户问题：\n\n{rag_context}\n\n用户问题：{content}"

        # L1 隐私解密门：用户明确要求时解密注入并审计（仅白名单话题）
        if memory_facts and content.strip():
            try:
                async with async_session_factory() as session:
                    decrypted = await resolve_decrypt_candidates(
                        session, user_id, conversation_id, memory_facts, content
                    )
                if decrypted:
                    plaintext_block = "\n".join(f"- {d['plaintext']}" for d in decrypted)
                    messages[-1]["content"] += f"\n\n[已获用户授权使用的隐私信息]\n{plaintext_block}"
            except Exception as exc:  # noqa: BLE001
                logger.warning("隐私解密门处理失败: {}", exc)

        logger.info(
            "聊天准备完成: scene={} scope={} duration_ms={} rag={} memory_facts={}",
            scene,
            retrieval_scope.value,
            round((time.perf_counter() - started_at) * 1000, 1),
            should_retrieve,
            len(memory_facts),
        )
        return {"is_first": is_first, "messages": messages, "citations": citations}

    async def _get_system_prompt(self, user_id: str, scene: str) -> str:
        """系统提示词 = 一级（安全规范，最高优先级） + 二级（角色设定）.

        一级提示词固定前置且不可被覆盖：负责安全红线、防提示注入与越权指令拦截；
        二级提示词由用户选定的角色（内置/自定义）或场景默认充当，负责性格与说话方式。
        所有对话路径（流式/阻塞）统一经过此方法，保证安全底线始终生效。
        """
        base = get_base_system_prompt()
        role = await self._resolve_role_prompt(user_id, scene)
        return f"{base}\n\n[角色设定]\n{role}"

    async def _resolve_role_prompt(self, user_id: str, scene: str) -> str:
        """二级提示词：用户选定角色优先，否则场景默认（可插拔角色目录）."""
        prompt_id = None
        try:
            async with async_session_factory() as session:
                from app.models.db_models import User

                user = await session.get(User, uuid.UUID(str(user_id)))
                prompt_id = user.prompt_id if user else None
        except Exception:  # noqa: BLE001
            prompt_id = None
        if prompt_id:
            content = await get_prompt_content(prompt_id, str(user_id))
            if content:
                note = _SCENE_BEHAVIOR.get(scene, "")
                return content + (f"\n\n{note}" if note else "")
        return get_scene_config(scene)["system_prompt"]

    async def _finalize_reply(
        self,
        conversation_id: str,
        user_id: str,
        reply: str,
        scene: str = "chat",
        segments: list[str] | None = None,
    ) -> None:
        """保存助手回复到上下文，触发长期记忆抽取。

        content 以 JSON 数组形态存储（多短句合并为一次交互），
        后续"多条短句回复"策略接入时直接往数组里追加分段即可。
        """
        assistant_msg = {
            "role": "assistant",
            "content": serialize_content(segments if segments else reply),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.append_context(conversation_id, assistant_msg)
        # 会话段摘要只允许读取消息持久化后的原文，由 conversations API
        # 在 commit 后投递 Celery；这里绝不能在 SSE 收尾等待一次摘要模型调用。
        # 办公模式：无长期记忆
        if scene != "office":
            await self._maybe_extract_memories(conversation_id, user_id)

    # ── 工具调用与流式回复 ─────────────────────────────
    async def _call_llm_auto(
        self,
        user_id: str,
        messages: list[dict],
        scene: str,
        image_uris: list[str],
        user_content: str,
        citations: list[dict],
        conversation_id: str = "",
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        force_web_search: bool = False,
    ) -> str:
        """阻塞版：技能循环（开启时）或 模型自主联网 + 场景模型回复."""
        # 先确定本次实际使用的模型（快速/思考档覆盖优先），再决定图片直传还是 VL 描述成文本
        override = await _get_chat_model_override(scene, thinking_mode, llm_api_key, user_id)
        if image_uris:
            target_model = override["model"] if override else str(
                (await get_llm_config(scene, self._llm.provider, user_id=user_id)).get("model") or ""
            )
            if self._is_multimodal_model(target_model):
                messages = self._attach_images(messages, image_uris)
            else:
                messages = await self._describe_images_to_text(messages, image_uris, user_id)

        if force_web_search:
            messages = _append_web_search_preference(messages)

        # 普通聊天的可选实时/知识库查询走受控 LangGraph ToolNode；办公自动化
        # 始终由办公 DAG 负责，不能从这里旁路进入。图片、语音和 RAG 已在上方
        # 预处理完成，图只看 chat 场景白名单（web_search/query_knowledge/get_datetime）。
        if (
            scene == "chat"
            and settings.AGENT_SKILLS_ENABLED
            and (force_web_search or _needs_chat_tool_graph(user_content))
        ):
            try:
                reply, tool_records, tool_citations = await run_skill_loop(
                    self._llm,
                    user_id,
                    _append_chat_tool_contract(messages, web_search_preferred=force_web_search),
                    scene="chat",
                    conversation_id=conversation_id,
                    llm_api_key=llm_api_key,
                    llm_base_url=override["base_url"] if override else None,
                    llm_model=override["model"] if override else None,
                )
                citations.extend(tool_citations)
                if reply:
                    return reply
                if tool_records:
                    logger.warning("普通聊天技能图未产生正文，回退常规聊天")
            except Exception as exc:  # noqa: BLE001
                logger.warning("普通聊天技能图失败，回退常规聊天: {}", str(exc)[:240])

        # 普通模式快速/思考档：显式切换模型（fast=DS Flash / think=强模型）
        if override:
            try:
                return await self._llm.chat(
                    messages,
                    base_url=override["base_url"],
                    api_key=override["api_key"],
                    model=override["model"],
                    timeout=override["timeout"],
                    scene=scene,
                    usage_user_id=user_id,
                    usage_category=CATEGORY_CHAT,
                )
            except Exception as exc:  # noqa: BLE001 - 本地/强模型不可用时回退默认模型
                logger.warning(
                    "普通模式 {} 档模型 {} 调用失败，回退默认模型: {}",
                    thinking_mode, override["model"], str(exc)[:160],
                )
        return await self._llm.chat(
            messages,
            scene=scene,
            usage_user_id=user_id,
            usage_category=CATEGORY_CHAT,
            api_key=llm_api_key,
            reasoning_effort=_chat_reasoning_effort(thinking_mode),
        )

    async def _stream_llm_auto(
        self,
        user_id: str,
        messages: list[dict],
        scene: str,
        image_uris: list[str],
        user_content: str,
        citations: list[dict],
        conversation_id: str = "",
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        force_web_search: bool = False,
    ):
        """流式版：技能循环（开启时）或 模型自主联网，最终回复流式产出."""
        # 先确定本次实际使用的模型（快速/思考档覆盖优先），再决定图片直传还是 VL 描述成文本
        override = await _get_chat_model_override(scene, thinking_mode, llm_api_key, user_id)
        if image_uris:
            target_model = override["model"] if override else str(
                (await get_llm_config(scene, self._llm.provider, user_id=user_id)).get("model") or ""
            )
            if self._is_multimodal_model(target_model):
                messages = self._attach_images(messages, image_uris)
            else:
                messages = await self._describe_images_to_text(messages, image_uris, user_id)

        if force_web_search:
            messages = _append_web_search_preference(messages)

        # LangGraph 的工具调用需要先获得完整模型消息才能安全执行，避免把中间
        # tool_calls 混入 SSE。完成后按原 SSE 协议一次性投递正文；普通闲聊模型
        # 不调用工具时仍会在第一轮直接返回，且不会接触办公能力。
        if (
            scene == "chat"
            and settings.AGENT_SKILLS_ENABLED
            and (force_web_search or _needs_chat_tool_graph(user_content))
        ):
            try:
                # ToolNode 在等待模型收尾回复期间也会发出运行状态。将其放进
                # 队列而不是等整轮循环结束后再拼接，才能让 SSE 和实际执行保持
                # 同步，前端也能明确看到工具确实已被调用。
                progress_queue: asyncio.Queue[object] = asyncio.Queue()

                def on_tool_progress(event: object) -> None:
                    progress_queue.put_nowait(event)

                tool_task = asyncio.create_task(
                    run_skill_loop(
                        self._llm,
                        user_id,
                        _append_chat_tool_contract(messages, web_search_preferred=force_web_search),
                        scene="chat",
                        conversation_id=conversation_id,
                        llm_api_key=llm_api_key,
                        llm_base_url=override["base_url"] if override else None,
                        llm_model=override["model"] if override else None,
                        on_progress=on_tool_progress,
                    )
                )
                while not tool_task.done():
                    next_progress = asyncio.create_task(progress_queue.get())
                    done, _ = await asyncio.wait(
                        {tool_task, next_progress}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if next_progress in done:
                        progress = next_progress.result()
                        if isinstance(progress, dict) and progress.get("type") == "step":
                            yield {"type": "step", "step": progress}
                    else:
                        next_progress.cancel()
                        await asyncio.gather(next_progress, return_exceptions=True)

                # 在任务结束与最后一次 queue.get 竞争时，补发尚未消费的事件。
                while not progress_queue.empty():
                    progress = progress_queue.get_nowait()
                    if isinstance(progress, dict) and progress.get("type") == "step":
                        yield {"type": "step", "step": progress}

                reply, _tool_records, tool_citations = tool_task.result()
                citations.extend(tool_citations)
                if reply:
                    yield {"type": "delta", "content": reply}
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning("普通聊天技能图流式路径失败，回退常规流: {}", str(exc)[:240])

        # 普通模式快速/思考档：显式切换模型（fast=DS Flash / think=强模型）
        if override:
            try:
                async for delta in self._llm.chat_stream(
                    messages,
                    base_url=override["base_url"],
                    api_key=override["api_key"],
                    model=override["model"],
                    timeout=override["timeout"],
                    scene=scene,
                    usage_user_id=user_id,
                    usage_category=CATEGORY_CHAT,
                ):
                    yield {"type": "delta", "content": delta}
                return
            except Exception as exc:  # noqa: BLE001 - 本地/强模型不可用时回退默认模型
                logger.warning(
                    "普通模式 {} 档模型 {} 流式调用失败，回退默认模型: {}",
                    thinking_mode, override["model"], str(exc)[:160],
                )
        async for delta in self._llm.chat_stream(
            messages,
            scene=scene,
            usage_user_id=user_id,
            usage_category=CATEGORY_CHAT,
            api_key=llm_api_key,
            reasoning_effort=_chat_reasoning_effort(thinking_mode),
        ):
            yield {"type": "delta", "content": delta}

    # ── 内部方法 ────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数：中文按 1 字符 ≈ 1 token，其他按 3 字符 ≈ 1 token."""
        if not text:
            return 0
        text = normalize_content(text)
        cjk = sum(
            1 for ch in text
            if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"
        )
        other = len(text) - cjk
        return cjk + other // 3 + 2

    @staticmethod
    def _split_short_reply(text: str, max_segments: int = 12) -> list[str]:
        """把整段回复切成多条短句（豆包式多段短句显示）.

        - 按句末标点 + 换行切分，标点随句保留；
        - 过短的残句并入前一段，避免出现一两个字的分段；
        - 无标点的超长句按 60 字硬切兜底；
        - 超过 max_segments 段时，超出部分合并为最后一段。
        """
        if not text or not text.strip():
            return []
        import re

        parts = re.split(r"(?<=[。！？!?；;…])\s*", text.strip())
        parts = [p.strip() for p in parts if p and p.strip()]
        segments: list[str] = []
        for part in parts:
            if segments and len(part) < 8:
                segments[-1] += part
                continue
            while len(part) > 80:
                segments.append(part[:60].strip())
                part = part[60:].strip()
            if part:
                segments.append(part)
        if len(segments) > max_segments:
            merged = "".join(segments[max_segments - 1 :])
            segments = segments[: max_segments - 1] + [merged]
        return segments

    def _trim_history(self, history: list[dict], budget: int) -> list[dict]:
        """按 token 预算从旧到新裁剪历史，保留最近的消息（当前提问始终保留）."""
        kept: list[dict] = []
        used = 0
        for msg in reversed(history[:-1]):  # 最新在前；最后一条是当前提问
            cost = self._estimate_tokens(str(msg.get("content") or ""))
            if used + cost > budget and kept:
                break
            kept.append(msg)
            used += cost
        return list(reversed(kept))

    def _build_messages(
        self,
        scene: str,
        profile: dict | None,
        facts: list[dict],
        history: list[dict],
        current: str,
        summary: str | None = None,
        system_prompt: str | None = None,
        thinking_mode: str = "fast",
        conversation_recall: ConversationRecall | None = None,
    ) -> list[dict]:
        """构建 LLM 请求消息列表（画像 + 记忆事实 + 摘要 + 历史按 token 预算裁剪）."""
        system_prompt = system_prompt or get_scene_config(scene)["system_prompt"]

        # 注入对话摘要（旧消息的压缩记忆）
        if summary:
            system_prompt += f"\n\n[对话历史摘要]\n{summary}"

        # 注入用户画像（常驻）
        if profile:
            system_prompt += f"\n\n[用户画像]\n{self._render_profile(profile)}"

        # 注入按需召回的记忆事实
        if facts:
            system_prompt += f"\n\n[用户长期记忆]\n{self._render_facts(facts)}"

        # 仅在当前问题明确指向旧上下文时注入段摘要；它们是带来源的参考，
        # 不是新的系统指令。原文片段作为独立消息加入，避免淹没当前窗口。
        recall = conversation_recall or ConversationRecall()
        if recall.segment_summaries:
            blocks = "\n".join(f"- {item}" for item in recall.segment_summaries)
            system_prompt += f"\n\n[相关此前话题摘要，仅作参考]\n{blocks}"

        # 隐私规则（恒常附加）
        system_prompt += _PRIVACY_RULES

        messages = [{"role": "system", "content": system_prompt}]

        # 注入最近历史（token 预算内），排除最后一条（当前消息已包含）。
        # 不以轮次数截断：深度陪伴对话常有短句，token 才是稳定的上下文度量。
        if scene == "office":
            budget = settings.LLM_HISTORY_MAX_TOKENS_WORK
        else:
            budget = settings.LLM_HISTORY_MAX_TOKENS
        for msg in self._trim_history(history, budget):
            messages.append({"role": msg["role"], "content": normalize_content(msg.get("content") or "")})

        if recall.raw_messages:
            evidence = "\n".join(
                f"{('用户' if item.get('role') == 'user' else '助手')}: {item.get('content', '')}"
                for item in recall.raw_messages
            )
            messages.append(
                {
                    "role": "system",
                    "content": "[此前对话原文片段，仅用于回答当前问题]\n" + evidence,
                }
            )

        messages.append({"role": "user", "content": current})
        return messages

    @staticmethod
    def _render_profile(profile: dict) -> str:
        """画像 → 注入文本."""
        parts: list[str] = []
        for k, v in (profile.get("identity") or {}).items():
            parts.append(f"{k}：{v}")
        prefs = profile.get("preferences") or []
        if prefs:
            parts.append("偏好：" + "、".join(str(p) for p in prefs))
        for g in profile.get("goals") or []:
            if isinstance(g, dict):
                parts.append(f"目标：{g.get('目标', g.get('goal', ''))}（{g.get('状态', '进行中')}）")
        for p in profile.get("privacy") or []:
            if isinstance(p, dict):
                parts.append(f"隐私项：{p.get('占位', '')}（未获授权不读取）")
        return "\n".join(parts) or "（暂无画像信息）"

    @staticmethod
    def _render_facts(facts: list[dict]) -> str:
        """记忆事实 → 注入文本（L1 只显示占位符）."""
        lines: list[str] = []
        for f in facts:
            text = str(f.get("fact") or "")
            if f.get("privacy_level") == 1:
                lines.append(f"- [隐私] {text}（未获授权不读取具体内容）")
                continue
            t = _TYPE_CN.get(str(f.get("memory_type") or ""), str(f.get("memory_type") or "记忆"))
            imp = f.get("importance")
            suffix = f"（重要度 {round(float(imp), 1)}）" if imp is not None else ""
            created_at = f.get("created_at")
            source = f"，记录于 {str(created_at)[:10]}" if created_at else ""
            lines.append(f"- [{t}] {text}{suffix}{source}")
        return "\n".join(lines)

    async def _retrieve_knowledge(
        self,
        user_id: str,
        query: str,
        space_tags: list[str],
        thinking_mode: str = "fast",
        query_variants: list[str] | None = None,
    ) -> tuple[str, list[dict]]:
        """RAG 检索 —— pgvector 相似度检索（个人空间 + 公共空间）.

        Returns:
            (拼接后的上下文文本, 引用列表)
        """
        try:
            async with async_session_factory() as session:
                return await search_user_knowledge(
                    session,
                    user_id=user_id,
                    query=query,
                    space_tags=space_tags,
                    top_k=settings.RAG_TOP_K,
                    threshold=settings.RAG_SIMILARITY_THRESHOLD,
                    # 代码文件走 code 索引，不混入普通聊天/办公知识检索
                    exclude_categories=["code"],
                    rerank_enabled=thinking_mode == "think",
                    query_variants=query_variants,
                )
        except Exception as e:
            # 检索失败不阻塞对话主流程，仅记录并跳过知识库
            logger.warning("RAG 检索失败，跳过知识库: {}", e)
            return "", []

    @staticmethod
    def _is_multimodal_model(model: str) -> bool:
        """按模型名判断是否支持图片输入."""
        name = (model or "").lower()
        return any(k in name for k in _MULTIMODAL_KEYWORDS)

    @staticmethod
    def _attach_images(messages: list[dict], images: list[str]) -> list[dict]:
        """把最后一条用户消息改写为 text + image_url 分片（OpenAI 兼容格式）."""
        if not messages or messages[-1].get("role") != "user":
            return messages
        text = str(messages[-1].get("content") or "")
        result = list(messages)
        result[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                *[{"type": "image_url", "image_url": {"url": uri}} for uri in images],
            ],
        }
        return result

    async def _describe_one_image(self, image_uri: str, user_id: str) -> str | None:
        """用本地 qwen2.5vl:7b 描述单张图片；本地不可用时回退云端 qwen-vl-plus."""
        prompt = (
            "请用中文详细描述这张图片的内容：主体、场景、动作、文字信息等，"
            "描述要具体完整，供纯文本大模型理解这张图片。"
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": image_uri}},
                ],
            }
        ]
        # 本地 VL 优先（Ollama qwen2.5vl:7b）
        if settings.VL_MODEL:
            try:
                desc = await self._llm.chat(
                    messages,
                    base_url=settings.VL_BASE_URL.rstrip("/"),
                    api_key=settings.VL_API_KEY,
                    model=settings.VL_MODEL,
                    timeout=float(settings.VL_TIMEOUT),
                    usage_user_id=user_id,
                    usage_category=CATEGORY_SKILL,
                )
                if desc and desc.strip():
                    return desc.strip()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Vision] 本地 VL 描述失败，回退云端: {}", str(exc)[:120])
        # 云端 qwen-vl-plus 兜底
        try:
            desc = await self._llm.chat(
                messages,
                scene="chat",
                model=settings.QWEN_VL_MODEL,
                usage_user_id=user_id,
                usage_category=CATEGORY_SKILL,
            )
            return (desc or "").strip() or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Vision] 云端 VL 描述失败: {}", str(exc)[:120])
            return None

    async def _describe_images_to_text(
        self, messages: list[dict], image_uris: list[str], user_id: str
    ) -> list[dict]:
        """主模型不支持图片时：VL 模型描述图片 → 文本注入最后一条用户消息."""
        if not messages or messages[-1].get("role") != "user":
            return messages
        descriptions: list[str] = []
        for i, uri in enumerate(image_uris, 1):
            desc = await self._describe_one_image(uri, user_id)
            if desc:
                descriptions.append(f"【图片{i}】\n{desc}")
        if not descriptions:
            logger.warning("[Vision] 图片描述全部失败，图片内容不会提供给主模型")
            return messages
        text = str(messages[-1].get("content") or "")
        result = list(messages)
        result[-1] = {
            "role": "user",
            "content": f"{text}\n\n[用户上传的图片（已由视觉模型描述）]\n"
            + "\n\n".join(descriptions),
        }
        return result

    @staticmethod
    async def _load_image_data_uris(user_id: str, attachments: list | None) -> list[str]:
        """把图片附件读为 base64 data URI（供多模态模型使用；路径校验防越权）."""
        if not attachments:
            return []
        base = (Path(settings.UPLOAD_DIR) / "chat" / str(user_id)).resolve()
        uris: list[str] = []
        for att in attachments:
            if not isinstance(att, dict) or att.get("type") != "image":
                continue
            url = str(att.get("url") or "")
            parts = [p for p in url.split("/") if p]
            # URL 形如 /uploads/{user_id}/{filename}（静态挂载目录即 UPLOAD_DIR/chat）
            if len(parts) < 3 or parts[0] != "uploads" or parts[1] != str(user_id):
                continue
            target = (base / parts[-1]).resolve()
            if not target.is_relative_to(base) or not target.is_file():
                logger.warning("[Vision] 图片文件不存在或路径越界，跳过: {}", url)
                continue
            try:
                data = target.read_bytes()
            except OSError:
                continue
            if len(data) > _MAX_IMAGE_BYTES:
                logger.warning(
                    "[Vision] 图片过大（{}MB），跳过: {}", len(data) // 1024 // 1024, parts[-1]
                )
                continue
            mime = att.get("mime_type") or mimetypes.guess_type(target.name)[0] or "image/png"
            uris.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
            if len(uris) >= _MAX_IMAGES_PER_MESSAGE:
                break
        return uris

    async def _call_llm(
        self,
        user_id: str,
        messages: list[dict],
        scene: str = "chat",
        images: list[str] | None = None,
    ) -> str:
        """调用云端 LLM 生成回复（配置动态读取: Redis → .env）.

        当前模型支持多模态且存在图片时，把最后一条用户消息改写为
        OpenAI 兼容的 content 分片（text + image_url data URI）。
        """
        await self._ensure_llm_started()
        if images:
            cfg = await get_llm_config(scene, self._llm.provider)
            model = str(cfg.get("model") or "")
            if self._is_multimodal_model(model) and messages and messages[-1].get("role") == "user":
                messages = self._attach_images(messages, images)
            else:
                logger.warning("当前模型不支持多模态（{}），本轮图片已忽略", model)
        reply = await self._llm.chat(
            messages, scene=scene, usage_user_id=user_id, usage_category=CATEGORY_CHAT
        )
        return reply

    # ── 智能体路由 ──────────────────────────────────────

    async def route_and_execute(self, agent_name: str, message: str, session_id: str | None = None) -> dict:
        """按名称路由到指定智能体并执行（保留兼容）."""
        agent = AgentRegistry.get(agent_name)
        if not agent:
            return {
                "error": f"智能体 '{agent_name}' 未找到",
                "agents_available": [a.name for a in AgentRegistry.list_all()],
            }
        context = AgentContext(session_id=session_id)
        content = await agent.execute(message, context)
        return {
            "agent_name": agent.name,
            "content": content,
            "session_id": session_id,
            "metadata": context.metadata,
        }

    async def list_agents(self) -> list[dict]:
        """列出所有可用智能体."""
        return [{"name": a.name, "description": a.description} for a in AgentRegistry.list_all()]

    async def list_scenes(self) -> list[dict]:
        """列出所有可用场景模式."""
        from app.services.scene_manager import SCENE_CONFIGS

        return [{"id": k, "name": v["name"], "local_acceleration": v["local_acceleration"]} for k, v in SCENE_CONFIGS.items()]


# 全局单例
orchestrator = Orchestrator()
