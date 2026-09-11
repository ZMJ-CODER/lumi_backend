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
from app.services.prompts import OFFICE_DECISION_PROMPT, get_base_system_prompt, get_prompt_content
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
    "你是受控的公网资料工具选择器。web_search 负责按关键词发现来源，web_fetch 负责读取用户指定 URL 并提取事实。\n"
    "当用户说查一下、检索、调研、找资料、官方资料、给来源或要求最新/近期信息时，应优先进行公网检索；普通常识解释无需工具。\n"
    "用户自己的任务状态、对话历史、上传附件、知识库内容、总结、改写、创作、计算和普通问答，绝不调用。\n"
    "天气、气温、降雨、汇率、股价、行情、新闻等带有‘今天/当前/实时/最新’限定的公开事实，必须先调用 web_search；‘我今天的待办’属于私有上下文，不应联网。\n"
    "不确定时不要调用；搜索失败时必须如实说明未完成来源核验，不得用模型记忆伪装成已联网。"
)

# 这些词只用于显式联网意图的快速候选判断，绝不代表后端强制执行搜索。
_WEB_INTENT_KEYWORDS = (
    "联网", "网上搜", "网页搜索", "搜索网页", "检索公开资料", "查网页", "给我来源",
    "搜索新闻", "最新新闻", "公开资料", "查资料", "查信息", "检索", "调研", "研究一下", "找资料", "官方资料", "官方文档", "web search", "search the web", "browse the web",
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

# 普通聊天没有项目授权上下文，不能让模型把自身训练知识包装成“已检查
# Lumi 后端”的结论。命中时直接返回边界说明，不进入检索或工具链。
_INTERNAL_PROJECT_MARKERS = (
    "lumi项目", "本项目", "项目代码", "后端代码", "后端实现", "源码", "源代码",
    "测试脚本", "测试数据", "编排引擎", "执行引擎", "项目架构", "代码实现",
)


def _is_internal_project_question(content: str) -> bool:
    text = (content or "").casefold().replace(" ", "")
    return any(marker in text for marker in _INTERNAL_PROJECT_MARKERS)


_INTERNAL_PROJECT_BOUNDARY_REPLY = (
    "我不能在普通对话中读取或核验 Lumi 的源代码、后端实现、测试数据、部署配置或内部提示词。"
    "如果需要执行项目代码任务，请在任务提交时明确选择已授权的项目；否则我只能提供与具体仓库无关的一般性说明。"
)

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
    "office": "当前为办公模式：先用通用知识直接完成不依赖外部事实的请求。只有用户明确要求实时信息、公司内部资料、已上传文件或外部操作时，才使用相应工具；不要为了回答而强行检索。",
    "game": "当前为游戏模式：回复短小精悍，像队友一样；可结合攻略语料给出可执行建议。",
}

# 标题生成提示词：一句话概括对话主题
_TITLE_SYSTEM_PROMPT = (
    "你是对话标题生成助手。用一句话概括这段对话的主题，10~20 个字，"
    "不要引号、不要句号结尾、不要任何多余解释，只输出标题本身。"
)


async def _office_workspace_summary_text(
    user_id: str, conversation_id: str | None, workspace_id: str | None
) -> str:
    """为办公直接回答路径加载绑定工作区的目录/状态摘要。

    只读、无副作用；不可用/降级时同样返回带边界说明的摘要文本，
    让模型既能继续不依赖工作区的部分，也不会假装看到文件。
    """
    if not (conversation_id or workspace_id):
        return ""
    try:
        from app.services.workspace_context import (
            load_workspace_context,
            workspace_summary_text,
        )

        wctx = await load_workspace_context(
            user_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
        )
        return workspace_summary_text(wctx)
    except Exception:  # noqa: BLE001 - 摘要失败不阻断直接回答
        return ""


def _workspace_content_question(content: str) -> bool:
    """只读问题的粗略判定：内容指向本地工作区文件/目录时才开启读取窗口。

    仅作为提示信号；真正的调用仍受 workspace 授权门与只读工具白名单约束。
    """
    value = (content or "").casefold()
    markers = (
        "文件", "目录", "文件夹", "内容", "项目代码", "项目结构", "结构",
        "代码", "读取", "查看", "查找", "搜索", "打开", "看看", "读一下",
        "里有什么", "这份", "该文件", "附件", "资料", "文档", "正文",
        "主要讲", "概括", "总结", "摘要", "说明", "介绍", "其中",
        "readme", "workspace", "project", "src/", ".py",
        ".md", ".json", ".toml", ".yaml", ".cfg", ".txt", ".pdf",
        ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
        "ppt", "pptx", "pdf", "docx", "xlsx", "演示文稿", "幻灯片",
    )
    return any(marker in value for marker in markers)


def _resolve_workspace_tool_name(name: str, names: set[str]) -> str | None:
    """Resolve provider-emitted bare workspace names to qualified MCP names.

    Desktop capabilities are exposed to the model as ``mcp__server__tool``
    names, while some OpenAI-compatible/DeepSeek DSML responses emit only the
    raw ``tool`` part (for example ``workspace_read``).  Treating that as an
    unknown tool used to terminate the read window before the tool result was
    appended, leaving only the model's introductory sentence visible.  The
    mapping is deliberately suffix-based and only succeeds when exactly one
    authorized capability matches, so it cannot broaden the workspace scope.
    """
    requested = str(name or "").strip()
    if not requested:
        return None
    if requested in names:
        return requested
    suffix = requested.split("__")[-1]
    matches = [candidate for candidate in names if candidate.split("__")[-1] == suffix]
    return matches[0] if len(matches) == 1 else None


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
        workspace_id: str | None = None,
        web_search_enabled: bool = False,
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        reply_style: str | None = None,
        user_role: str = "user",
        execution_preference: str = "use_workspace_policy",
    ) -> dict:
        """处理用户消息的核心流程（阻塞版，供旧接口/降级路径使用）."""
        transcript = await self._resolve_transcript(content, attachments)
        content = transcript

        # The conversation is the office workspace selector.  Recover the
        # server-bound workspace before any task-shape or planner decision so
        # a stale/omitted client workspace_id cannot drop the authorization
        # scope and later surface as WORKSPACE_NOT_REGISTERED.
        if scene == "office" and conversation_id and not workspace_id:
            try:
                from app.services.workspaces import workspace_for_conversation

                bound = workspace_for_conversation(user_id, conversation_id)
                workspace_id = str((bound or {}).get("workspace_id") or "") or None
            except (LookupError, ValueError, OSError):
                workspace_id = None

        if scene == "office":
            from app.agents.orchestration.task_preflight import preflight_external_effect

            preflight = preflight_external_effect(
                content,
                workspace_id=workspace_id,
                office_docs=office_docs,
            )
            if preflight.needs_clarification:
                answer = preflight.question
                await self._finalize_reply(conversation_id, user_id, answer, scene)
                return {
                    "message_id": str(uuid.uuid4()), "content": answer,
                    "citations": [], "scene": scene, "local_mode": False,
                    "title": "", "transcript": transcript, "steps": [],
                    "task_shape": {"orchestrated": False, "reasons": [preflight.reason]},
                }

        if scene == "chat" and _is_internal_project_question(content):
            # 这是权限边界而不是模型自评；不调用 LLM，避免产生看似来自
            # 仓库检查的幻觉答案，也避免把项目问题送入长期记忆抽取。
            return {
                "message_id": str(uuid.uuid4()),
                "content": _INTERNAL_PROJECT_BOUNDARY_REPLY,
                "citations": [],
                "scene": scene,
                "local_mode": False,
                "title": "",
                "transcript": transcript,
                "steps": [],
            }

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

        if scene == "office":
            # The office scene is not synonymous with orchestration.  First
            # evaluate the shape of the request across all capability domains
            # (documents are only one possible context source).  Read-only
            # context questions go straight to the model; Skills/Planner are
            # reserved for external capabilities, side effects and dynamic
            # multi-step work.
            from app.agents.orchestration.task_shape import assess_task_shape_with_skills
            from app.services.office_context import (
                OfficeContext,
                append_office_context,
                load_office_context,
            )

            office_context = await load_office_context(
                user_id, content, office_docs=office_docs, workspace_id=workspace_id
            )
            shape = await assess_task_shape_with_skills(
                content,
                context_chars=len(office_context.text),
                user_id=user_id,
                scene=scene,
            )
            if not shape.requires_orchestration:
                # 项目对话先把工作区目录/状态摘要注入模型，再决定是否只读回答；
                # 摘要缺失/设备离线时同样注入降级边界说明（不假装看到文件）。
                workspace_summary = await _office_workspace_summary_text(
                    user_id, conversation_id, workspace_id
                )
                direct_messages = append_office_context(prep["messages"], office_context, content)
                if workspace_summary:
                    direct_messages = append_office_context(
                        direct_messages, OfficeContext(text=workspace_summary), content
                    )
                # 只读项目问题：可访问工作区 + 明确读取意图时，先用受限读取窗口
                # （少量 workspace read/search 调用）查证内容，再收敛回答。
                workspace_reply, _read_records = await self._bounded_workspace_read(
                    user_id=user_id,
                    user_role=user_role,
                    conversation_id=conversation_id,
                    content=content,
                    workspace_id=workspace_id or "",
                    workspace_summary=workspace_summary,
                    llm_api_key=llm_api_key,
                )
                if workspace_reply is not None:
                    reply = workspace_reply
                else:
                    reply = await self._call_llm_auto(
                        user_id,
                        direct_messages,
                        scene,
                        image_uris,
                        content,
                        prep["citations"],
                        conversation_id,
                        llm_api_key,
                        thinking_mode=thinking_mode,
                        force_web_search=False,
                        allow_tools=False,
                    )
                prep["citations"].extend(office_context.citations)
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
                    "steps": [],
                    "task_shape": {"orchestrated": False, "reasons": list(shape.reasons), "used_rag": office_context.used_rag},
                }
            reply, office_steps, office_citations = await self._run_office_job(
                user_id,
                conversation_id,
                content,
                office_docs or [],
                llm_api_key,
                user_role,
                workspace_id,
                execution_preference,
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
        workspace_id: str | None = None,
        web_search_enabled: bool = False,
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        reply_style: str | None = None,
        user_role: str = "user",
        execution_preference: str = "use_workspace_policy",
    ):
        """流式处理用户消息：准备流程同 handle_message，LLM 走工具调用 + SSE 流式.

        Yields: {"type": "delta", "content": ...} / {"type": "done", ...}
        """
        transcript = await self._resolve_transcript(content, attachments)
        content = transcript

        if scene == "office" and conversation_id and not workspace_id:
            try:
                from app.services.workspaces import workspace_for_conversation

                bound = workspace_for_conversation(user_id, conversation_id)
                workspace_id = str((bound or {}).get("workspace_id") or "") or None
            except (LookupError, ValueError, OSError):
                workspace_id = None

        if scene == "office":
            from app.agents.orchestration.task_preflight import preflight_external_effect

            preflight = preflight_external_effect(
                content,
                workspace_id=workspace_id,
                office_docs=office_docs,
            )
            if preflight.needs_clarification:
                answer = preflight.question
                yield {"type": "delta", "content": answer}
                yield {
                    "type": "done", "message_id": str(uuid.uuid4()),
                    "content": answer, "citations": [], "scene": scene,
                    "title": "", "steps": [],
                    "task_shape": {"orchestrated": False, "reasons": [preflight.reason]},
                }
                return

        if scene == "chat" and _is_internal_project_question(content):
            yield {"type": "delta", "content": _INTERNAL_PROJECT_BOUNDARY_REPLY}
            yield {
                "type": "done",
                "message_id": str(uuid.uuid4()),
                "content": _INTERNAL_PROJECT_BOUNDARY_REPLY,
                "citations": [],
                "scene": scene,
                "title": "",
            }
            return

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

        # ── v2 统一任务画像/执行策略（灰度开关；默认关闭保持旧语义）──
        policy_meta = None
        policy_public = None
        if getattr(settings, "EXECUTION_POLICY_V2_ENABLED", False):
            from lumi_orch.execution_policy import (
                TaskEntrySignals,
                policy_meta_from_signals,
                policy_meta_public,
            )
            from app.agents.orchestration.task_shape import assess_task_shape
            from app.core.observability import observe_policy_route

            signals = TaskEntrySignals(
                request=content,
                scene=scene,
                reasons=tuple(assess_task_shape(content).reasons),
                has_attachments=bool(attachments),
                has_office_docs=bool(office_docs),
                workspace_available=bool(workspace_id),
                web_search_enabled=bool(web_search_enabled),
                conversation_has_workspace=bool(workspace_id),
            )
            policy_meta = policy_meta_from_signals(signals, enabled=True)
            policy_public = policy_meta_public(policy_meta)
            if policy_public:
                observe_policy_route(
                    str(policy_public.get("execution_policy") or "direct_stream"),
                    str((policy_public.get("task_profile") or {}).get("complexity") or "ATOMIC"),
                )
        # v2 验收追踪：单请求 SSE 证据（开关开启时每用例一条 JSON 日志）。
        # 与旧执行策略开关解耦：只要开了验收日志或 Router v2 就产出该行。
        trace = None
        job_stream_used = False
        if bool(getattr(settings, "EXECUTION_POLICY_V2_ENABLED", False)) or bool(
            getattr(settings, "ACCEPTANCE_SSE_LOG", False)
        ):
            from app.services.policy_acceptance import new_trace

            trace = new_trace(enabled=True)

        # ── 修订版任务画像 / Router v2（灰度 TASK_ROUTER_V2_ENABLED）──
        router_meta: dict | None = None
        router_mode = ""
        router_blocked_reason = ""
        if getattr(settings, "TASK_ROUTER_V2_ENABLED", False):
            try:
                from app.services.task_assessor import AssessmentContext
                from app.services.task_router_adapter import plan_and_route

                _route_started = time.perf_counter()
                routed = await plan_and_route(
                    request=content,
                    context=AssessmentContext(
                        request=content,
                        has_attachments=bool(attachments),
                        has_office_docs=bool(office_docs),
                        workspace_id=str(workspace_id or ""),
                        workspace_bound=bool(workspace_id),
                        has_conversation_memory=bool(conversation_id),
                        web_search_enabled=bool(web_search_enabled),
                    ),
                    user_id=user_id,
                    llm_api_key=llm_api_key,
                    use_llm=bool(getattr(settings, "TASK_ASSESSOR_USE_LLM", False)),
                )
                if trace is not None:
                    # route_latency_ms 只衡量“画像+路由决策”耗时（不含模型 TTFT）。
                    trace["route_latency_ms_override"] = int(
                        (time.perf_counter() - _route_started) * 1000
                    )
                router_meta = routed.meta()
                router_mode = str(router_meta.get("route_mode") or "")
                if routed.blocked:
                    router_blocked_reason = routed.blocked_reason
            except Exception as exc:  # noqa: BLE001 - 路由评估失败回退旧路径
                logger.warning("Router v2 评估失败，回退旧路径: {}", str(exc)[:200])

        if router_blocked_reason:
            blocked_event = {"type": "delta", "content": router_blocked_reason}
            if trace is not None:
                from app.services.policy_acceptance import record_trace_event

                record_trace_event(trace, blocked_event)
            yield blocked_event
            blocked_done = {
                "type": "done",
                "message_id": message_id,
                "content": router_blocked_reason,
                "citations": [],
                "scene": scene,
                "title": title or "",
                "steps": [],
            }
            if router_meta is not None:
                blocked_done["task_router"] = router_meta
            if policy_public is not None:
                blocked_done.update(policy_public)
            if trace is not None:
                from app.services.policy_acceptance import finish_trace, record_trace_event

                record_trace_event(trace, blocked_done)
                finish_trace(
                    trace,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    policy_public=policy_public,
                    router_meta=router_meta,
                )
            yield blocked_done
            return

        # 首条消息：标题生成与回复流并行
        title_task = None
        if prep["is_first"] and not title:
            title_task = asyncio.create_task(self._generate_title(content, user_id, llm_api_key))

        full_text = ""
        atomic_steps: dict[str, dict] = {}
        stream = None
        if scene == "office":
            from app.agents.orchestration.task_shape import assess_task_shape_with_skills
            from app.services.office_context import (
                OfficeContext,
                append_office_context,
                load_office_context,
            )

            office_context = await load_office_context(
                user_id, content, office_docs=office_docs, workspace_id=workspace_id
            )
            shape = await assess_task_shape_with_skills(
                content,
                context_chars=len(office_context.text),
                user_id=user_id,
                scene=scene,
            )
            # m1_atomic_action（单次副作用）必须走原子动作/编排链路（暂存→Diff→
            # 审批）。M2/M3 只在“非降级置信度”时强制编排：低置信度启发式画像
            # 会把只读问答误判为复杂任务，强制编排会导致只出计划、没有正文。
            router_confident = float(
                ((router_meta or {}).get("task_profile") or {}).get("confidence") or 0.0
            ) >= 0.55
            force_orchestrate = router_mode == "m1_atomic_action" or (
                router_mode in {"sequential_workflow", "dynamic_agent"} and router_confident
            )
            if not shape.requires_orchestration and not force_orchestrate:
                prep["citations"].extend(office_context.citations)
                # 只读项目问题：注入工作区目录/状态摘要（或降级边界说明），
                # 并让可访问工作区走受限读取窗口（少量 read/search 调用）。
                workspace_summary = await _office_workspace_summary_text(
                    user_id, conversation_id, workspace_id
                )
                direct_messages = append_office_context(prep["messages"], office_context, content)
                if workspace_summary:
                    direct_messages = append_office_context(
                        direct_messages, OfficeContext(text=workspace_summary), content
                    )
                # Router v2：direct_chat 不触发任何读取；m1_atomic_read 走
                # 受控读取 → 真实 chat_stream；其余沿用既有 v2/旧安全路径。
                if router_mode == "direct_chat":
                    stream = self._stream_llm_auto(
                        user_id,
                        direct_messages,
                        scene,
                        image_uris,
                        content,
                        prep["citations"],
                        conversation_id,
                        llm_api_key,
                        thinking_mode=thinking_mode,
                        force_web_search=False,
                        allow_tools=False,
                    )
                elif router_mode == "m1_atomic_read" or bool(
                    getattr(settings, "EXECUTION_POLICY_V2_ENABLED", False)
                ):
                    stream = self._stream_v2_atomic_read(
                        user_id=user_id,
                        user_role=user_role,
                        conversation_id=conversation_id,
                        content=content,
                        direct_messages=direct_messages,
                        workspace_id=workspace_id or "",
                        workspace_summary=workspace_summary,
                        llm_api_key=llm_api_key,
                        thinking_mode=thinking_mode,
                    )
                else:
                    workspace_reply, _read_records = await self._bounded_workspace_read(
                        user_id=user_id,
                        user_role=user_role,
                        conversation_id=conversation_id,
                        content=content,
                        workspace_id=workspace_id or "",
                        workspace_summary=workspace_summary,
                        llm_api_key=llm_api_key,
                    )
                    if workspace_reply is not None:
                        stream = self._text_delta_stream(workspace_reply)
                    else:
                        stream = self._stream_llm_auto(
                            user_id,
                            direct_messages,
                            scene,
                            image_uris,
                            content,
                            prep["citations"],
                            conversation_id,
                            llm_api_key,
                            thinking_mode=thinking_mode,
                            force_web_search=False,
                            allow_tools=False,
                        )
            else:
                job_stream_used = True
                stream = self._stream_office_job(
                    user_id,
                    conversation_id,
                    content,
                    office_docs or [],
                    llm_api_key,
                    prep["citations"],
                    user_role,
                    workspace_id,
                    execution_preference,
                )
        else:
            stream = self._stream_llm_auto(
                user_id, prep["messages"], scene, image_uris, content, prep["citations"], conversation_id, llm_api_key,
                thinking_mode=thinking_mode,
                force_web_search=web_search_enabled,
            )
        # v2/灰度元数据：作为首批 SSE 事件（前端无需再次推断）。
        answer_stage_at = time.perf_counter()
        first_response_at: float | None = None
        first_delta_at: float | None = None
        last_delta_at: float | None = None
        # 内部流（如 plan_first 的 plan_ready/done）若已发出终态 done，
        # 外层不再补发第二个 done，保证“每次回复只有一个 done”。
        done_emitted_inner = False
        prefix_events: list[dict] = []
        if policy_public is not None:
            prefix_events.append({"type": "task_policy", **policy_public})
        if router_meta is not None:
            prefix_events.append({"type": "task_router", **router_meta})
        if prefix_events:
            base_stream = stream

            async def stream_with_meta():
                for event in prefix_events:
                    yield event
                async for evt in base_stream:
                    yield evt

            stream = stream_with_meta()
        async for evt in stream:
            if evt["type"] != "task_policy" and first_response_at is None:
                first_response_at = time.perf_counter()
            if evt["type"] == "delta":
                full_text += evt["content"]
                now = time.perf_counter()
                if first_delta_at is None:
                    first_delta_at = now
                last_delta_at = now
            elif evt["type"] == "step":
                step = evt.get("step") or {}
                if step.get("id"):
                    atomic_steps[str(step["id"])] = {
                        **atomic_steps.get(str(step["id"]), {}),
                        **step,
                    }
            elif evt["type"] == "done":
                done_emitted_inner = True
            if trace is not None:
                from app.services.policy_acceptance import record_trace_event

                record_trace_event(trace, evt)
            yield evt
        # v2 观测：route / first_delta / stream 时长（metrics 关闭时零开销）。
        if policy_public is not None:
            from app.core.observability import (
                observe_answer_first_delta,
                observe_answer_stream_duration,
                observe_policy_route_latency,
            )

            policy_label = str(policy_public.get("execution_policy") or "direct_stream")
            complexity_label = str(
                (policy_public.get("task_profile") or {}).get("complexity") or "ATOMIC"
            )
            if first_response_at is not None:
                observe_policy_route_latency(
                    policy_label, complexity_label, first_response_at - answer_stage_at
                )
            if first_delta_at is not None:
                observe_answer_first_delta(
                    policy_label, complexity_label, first_delta_at - answer_stage_at
                )
            if first_delta_at is not None and last_delta_at is not None:
                observe_answer_stream_duration(
                    policy_label, complexity_label, last_delta_at - first_delta_at
                )
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

        done_event = {
            "type": "done",
            "message_id": message_id,
            "content": full_text,
            "citations": project_citations(prep["citations"]),
            "scene": scene,
            "title": title or "",
            "segments": segments,
            "steps": list(atomic_steps.values()),
        }
        if policy_public is not None:
            # done 携带最终完整内容与任务元数据（v2 契约）。
            done_event.update(policy_public)
        if router_meta is not None:
            done_event["task_router"] = router_meta
        if not done_emitted_inner:
            if trace is not None:
                from app.services.policy_acceptance import record_trace_event

                record_trace_event(trace, done_event)
            yield done_event
        # v2 验收追踪输出：放在 done 之后结算，done_count 才是真实值。
        if trace is not None:
            from app.services.policy_acceptance import finish_trace

            finish_trace(
                trace,
                user_id=user_id,
                conversation_id=conversation_id,
                policy_public=policy_public,
                planner_hint=job_stream_used,
                router_meta=router_meta,
            )

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
        # Internal route-upgrade sentinels are execution control data, never a
        # user-facing answer.  Prefer the durable error/clarification text.
        if "ROUTE_UPGRADE_" in answer:
            answer = ""
        if answer:
            return answer
        if result.get("type") == "clarification":
            return str(result.get("question") or "请补充任务信息。")
        if result.get("type") == "planning_error":
            return str(result.get("message") or job.error or "办公任务规划失败，请稍后重试。")
        if result.get("type") == "execution_error":
            return str(result.get("message") or job.error or "办公任务执行失败，请稍后重试。")
        blocks = []
        retrieval_only = True
        for node in job.nodes:
            node_result = node.result or {}
            content = str(node_result.get("content") or node_result.get("output") or "").strip()
            if content:
                blocks.append(content)
                tool_name = str(node_result.get("tool") or node.params.get("preferred_tool") or "").strip()
                if tool_name not in {"web_search", "web_fetch", "kb_search", "query_knowledge"}:
                    retrieval_only = False
        if blocks:
            if retrieval_only:
                return "已完成资料检索，但当前归纳服务暂时不可用；请稍后重试以获取整理后的结论。"
            return "\n\n".join(blocks)
        failed = next((node for node in job.nodes if node.error), None)
        if failed and failed.error:
            return str(failed.error)
        return str(job.error or "办公任务未能完成，请检查失败步骤后重试。")

    # ── 只读工作区问答（受限读取工具窗口）─────────────────

    @staticmethod
    async def _text_delta_stream(text: str):
        """把最终文本切成有限段落长度的 delta 事件，供 SSE 使用。"""
        if not text:
            return
        current = ""
        for para in str(text).splitlines():
            if not para.strip():
                continue
            if len(current) + len(para) + 1 > 2000 and current:
                yield {"type": "delta", "content": current + "\n"}
                current = para
            else:
                current = (current + "\n" + para).strip("\n")
        if current:
            yield {"type": "delta", "content": current}

    async def _bounded_workspace_read(
        self,
        *,
        user_id: str,
        user_role: str,
        conversation_id: str,
        content: str,
        workspace_id: str,
        workspace_summary: str,
        llm_api_key: str | None,
    ) -> tuple[str | None, list[dict]]:
        """旧安全路径（非流式）：统一读取一次资料后由模型收敛回答。

        读取本身不再让模型选择 catalog/list/search/read 组合，
        统一走 read_workspace_context → WorkspaceReader（内部定位与解析）。
        """
        evidence, records = await self.read_workspace_context(
            user_id=user_id,
            user_role=user_role,
            conversation_id=conversation_id,
            content=content,
            workspace_id=workspace_id,
            workspace_summary=workspace_summary,
            llm_api_key=llm_api_key,
        )
        if not evidence:
            return None, records
        messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    "你是只读工作区问答助手。下面是系统受控读取到的工作区资料正文，"
                    "请仅依据这些内容回答用户问题；资料不足时明确说明，不要编造，"
                    "不要输出任何工具协议或标签。"
                ),
            },
            {"role": "user", "content": str(content or "")},
            {"role": "user", "content": "[工作区资料正文]\n" + evidence},
        ]
        try:
            final = await self._llm.chat(
                messages, scene="office", api_key=llm_api_key,
                usage_user_id=user_id, usage_category=CATEGORY_SKILL,
            )
        except Exception as exc:  # noqa: BLE001 - 收敛失败时直接交付读取正文
            logger.warning("工作区读取后收敛回答失败，直接返回读取正文: {}", str(exc)[:160])
            return evidence, records
        text = str(final or "").strip() or evidence
        return text, records

    async def read_workspace_context(
        self,
        *,
        user_id: str,
        user_role: str,
        conversation_id: str,
        content: str,
        workspace_id: str,
        workspace_summary: str,
        llm_api_key: str | None,
    ) -> tuple[str, list[dict]]:
        """统一读取：一个 workspace_read 内部完成定位 / 解析 / 分页。

        模型不参与工具选择（不再暴露 list/search/catalog 组合），只需把结构化
        正文注入上下文；读取失败返回可理解状态，绝不伪造内容。
        """
        if not str(workspace_id or "").strip() or not str(content or "").strip():
            return "", []
        if not _workspace_content_question(content):
            return "", []
        from app.services.information_resolver import requires_complete_read
        from app.services.workspace_reader import WorkspaceReader, unified_payload_to_text

        reader = WorkspaceReader(
            user_id=user_id,
            user_role=user_role,
            workspace_id=str(workspace_id),
            conversation_id=conversation_id,
        )
        page_chars = max(2000, int(getattr(settings, "WORKSPACE_READ_MAX_CHARS", 12000)))
        complete_read = requires_complete_read(content)
        max_pages = int(getattr(settings, "WORKSPACE_READ_MAX_PAGES_PER_REQUEST", 32)) if complete_read else 1

        sections: list[dict] = []
        cursor = ""
        pages_read = 0
        payload: dict = {}
        while pages_read < max_pages:
            payload = await reader.read(
                content if pages_read == 0 else "继续读取",
                cursor=cursor,
                max_chars=page_chars,
            )
            pages_read += 1
            for item in payload.get("content") or []:
                if isinstance(item, dict):
                    sections.append(item)
            if not payload.get("has_more"):
                break
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break

        read_complete = not bool(payload.get("has_more"))
        merged = {**payload, "content": sections}
        text = unified_payload_to_text(merged)
        records: list[dict] = [
            {
                "tool": "workspace_navigator",
                "action": "read",
                "source": str(item.get("source") or ""),
                "location": str(item.get("location") or ""),
                "status": str(payload.get("status") or ""),
            }
            for item in sections
        ]
        if not records:
            records.append({
                # 审计/SSE 记录模型实际看见的入口名，内部实现仍是 WorkspaceReader。
                "tool": "workspace_navigator",
                "action": "read",
                "status": str(payload.get("status") or "failed"),
                "summary": str(payload.get("summary") or ""),
                "error_code": str((payload.get("meta") or {}).get("error_code") or ""),
                **(
                    {"cursor": str(payload.get("cursor") or "")}
                    if payload.get("has_more")
                    else {}
                ),
            })
        # 覆盖度事实：整份请求读完了吗？下游与审计据此判断能不能声称"全文"。
        records[0]["read_complete"] = read_complete
        records[0]["pages_read"] = pages_read
        records[0]["complete_read_requested"] = complete_read
        if payload.get("has_more"):
            records[0]["cursor"] = str(payload.get("cursor") or "")
        return text, records

    async def stream_answer_from_context(
        self,
        *,
        user_id: str,
        messages: list[dict],
        content: str = "",
        conversation_id: str = "",
        llm_api_key: str | None = None,
        thinking_mode: str = "fast",
        scene: str = "office",
    ):
        """由已放入上下文的资料生成答案（v2 ATOMIC 只读后半段）。

        只调用一次真实 chat_stream（统一协议流出口），逐段产出
        delta/process/warning；不阻塞总结、不切段模拟、不发起多轮工具决策。
        """
        async for evt in self._protocol_llm_stream(
            messages=messages,
            scene=scene or "office",
            usage_user_id=user_id,
            usage_category=CATEGORY_CHAT,
            api_key=llm_api_key,
            reasoning_effort=_chat_reasoning_effort(thinking_mode),
        ):
            yield evt

    async def _stream_v2_atomic_read(
        self,
        *,
        user_id: str,
        user_role: str,
        conversation_id: str,
        content: str,
        direct_messages: list[dict],
        workspace_id: str,
        workspace_summary: str,
        llm_api_key: str | None,
        thinking_mode: str,
    ):
        """ATOMIC 只读任务完整快路径：受控读取 → process → 真实 chat_stream。

        工作区意图时先做有限次确定性读取；读取结果进上下文后直接流式作答。
        读取不可用/无正文时回退为基于注入摘要的普通 chat_stream 直答。
        """
        workspace_intent = bool(
            workspace_id
            and "已注册且可访问" in str(workspace_summary or "")
            and _workspace_content_question(content)
        )
        answer_messages = list(direct_messages)
        if workspace_intent:
            yield {"type": "process", "content": "正在读取工作区资料…"}
            started = time.perf_counter()
            # Router v2：通过 InformationResolver（适配器 + smart_slice）读取，
            # 超长内容只截断/分段，绝不因 CONTEXT_TOO_LARGE 升级复杂度。
            from lumi_orch.upgrade_policy import ContextFitStatus

            if bool(getattr(settings, "TASK_ROUTER_V2_ENABLED", False)):
                from app.services.information_resolver import InformationResolver

                async def _workspace_reader(query: str) -> str:
                    text, _recs = await self.read_workspace_context(
                        user_id=user_id,
                        user_role=user_role,
                        conversation_id=conversation_id,
                        content=query,
                        workspace_id=workspace_id,
                        workspace_summary=workspace_summary,
                        llm_api_key=llm_api_key,
                    )
                    return text

                resolved = await InformationResolver(
                    workspace_reader=_workspace_reader,
                ).resolve(["WORKSPACE"], content)
                evidence, records = resolved.text, resolved.records
                if resolved.status == ContextFitStatus.MULTI_STEP_REQUIRED:
                    yield {
                        "type": "process",
                        "content": "资料较长，本轮先给出关键片段结论，可继续分段整理。",
                    }
            else:
                evidence, records = await self.read_workspace_context(
                    user_id=user_id,
                    user_role=user_role,
                    conversation_id=conversation_id,
                    content=content,
                    workspace_id=workspace_id,
                    workspace_summary=workspace_summary,
                    llm_api_key=llm_api_key,
                )
            from app.core.observability import observe_workspace_read_duration

            observe_workspace_read_duration(time.perf_counter() - started)
            if evidence:
                from app.services.office_context import OfficeContext, append_office_context

                answer_messages = append_office_context(
                    answer_messages,
                    OfficeContext(text="[受限读取到的工作区资料正文]\n" + evidence),
                    content,
                )
                yield {"type": "process", "content": "已完成资料整理，正在生成回答…"}
            elif records:
                yield {"type": "process", "content": "未能读取到文件正文，将基于工作区目录摘要回答。"}
        # The read window above is the only place where workspace tools may be
        # executed for an atomic question.  Make that fact explicit in the
        # final model turn: some providers otherwise emit a second textual
        # DSML invocation when they see the workspace summary, which the
        # protocol firewall correctly strips and leaves only the lead sentence
        # ("我来读取…") visible to the user.
        answer_messages.append({
            "role": "system",
            "content": (
                "这是资料问答的最终回答阶段。工作区读取已由系统受控完成；"
                "请仅依据当前上下文中的实际资料和目录摘要回答用户问题。"
                "不要调用工具，不要输出任何 DSML/XML/tool_calls/function_call 协议，"
                "直接输出完整、可读的最终答案；若资料不足，明确说明不足之处。"
            ),
        })
        async for evt in self.stream_answer_from_context(
            user_id=user_id,
            messages=answer_messages,
            content=content,
            conversation_id=conversation_id,
            llm_api_key=llm_api_key,
            thinking_mode=thinking_mode,
            scene="office",
        ):
            yield evt

    async def _run_office_job(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        office_docs: list[dict],
        llm_api_key: str | None,
        user_role: str = "user",
        workspace_id: str | None = None,
        execution_preference: str = "use_workspace_policy",
    ) -> tuple[str, list[dict], list[dict]]:
        # Import the singleton from the concrete module.  ``from package import
        # orchestrator`` can resolve to the submodule object (because a module
        # with that name exists) instead of the package ``__getattr__`` export,
        # which would make ``submit_job`` unavailable at runtime.
        from app.agents.orchestration.orchestrator import orchestrator as agent_orchestrator
        from app.agents.orchestration.models import JobStatus

        job = await agent_orchestrator.submit_job(
            user_id,
            content,
            "office",
            conversation_id,
            llm_api_key=llm_api_key,
            office_docs=office_docs,
            workspace_id=workspace_id,
            user_role=user_role,
            execution_preference=execution_preference,
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
        workspace_id: str | None = None,
        execution_preference: str = "use_workspace_policy",
    ):
        from app.agents.orchestration.orchestrator import orchestrator as agent_orchestrator
        from app.agents.orchestration.models import JobStatus

        job = await agent_orchestrator.submit_job(
            user_id,
            content,
            "office",
            conversation_id,
            llm_api_key=llm_api_key,
            office_docs=office_docs,
            workspace_id=workspace_id,
            user_role=user_role,
            execution_preference=execution_preference,
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
        completion_waits = 0
        output_cursor = 0
        streamed_answer = ""
        try:
            while True:
                routing = getattr(job, "routing", None) or {}
                # 计划优先（step_confirm）：提交只生成计划并置 waiting_run，
                # 不自动派发执行；SSE 在展示计划后收敛，后续由
                # /agents/jobs/{id}/resume（action=run_next）逐步骤驱动。
                if isinstance(routing, dict) and routing.get("execution_state") == "waiting_run":
                    from lumi_orch.run_view import (
                        done_payload,
                        plan_ready_payload,
                        run_view,
                    )

                    view = run_view(job)
                    yield {"type": "plan_ready", **plan_ready_payload(job_id=job.job_id, view=view)}
                    # 计划优先：done.content 回填计划文本，避免前端在没有计划气泡
                    # 组件时渲染成空答复；真正执行由 run_next 驱动。
                    yield done_payload(
                        job_id=job.job_id,
                        view=view,
                        content=str(view.get("plan_text") or ""),
                    )
                    return
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
                    # A completed DAG can briefly be visible before the
                    # execution loop finishes its user-facing answer
                    # synthesis.  Do not terminate SSE on that intermediate
                    # snapshot, otherwise _job_answer falls back to the raw
                    # web_search observation.  Failed/cancelled jobs remain
                    # terminal immediately; only wait for completed jobs
                    # lacking final_answer.
                    if job.status == JobStatus.COMPLETED:
                        result = job.result if isinstance(job.result, dict) else {}
                        if not str(result.get("final_answer") or "").strip():
                            completion_waits += 1
                            # Final-answer synthesis is a separate LLM call
                            # that may legitimately take tens of seconds
                            # after the last tool completes.  Keep the SSE
                            # open long enough for it instead of exposing the
                            # retrieval observation as a premature answer.
                            if completion_waits <= 800:
                                await asyncio.sleep(0.15)
                                current = await agent_orchestrator.get_job(job.job_id)
                                if current is not None:
                                    job = current
                                    continue
                    completion_waits = 0
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
            # 办公模式使用独立、受控的近期任务摘要。它只包含请求摘要、
            # 结果摘要和产物元数据，不把完整工具输出或私有画像注入模型。
            try:
                from app.agents.orchestration.memory_service import OfficeMemoryService

                office_summary = await OfficeMemoryService().load_summaries(conversation_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("读取办公近期摘要失败: {}", exc)
                office_summary = ""
            if office_summary:
                summary = (summary + "\n" if summary else "") + "[近期办公任务]\n" + office_summary
            # 办公模式不读取通用长期身份/事实记忆，避免跨场景污染。
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
        prompt = f"{base}\n\n[角色设定]\n{role}"
        if scene == "office":
            prompt += f"\n\n{OFFICE_DECISION_PROMPT}"
        return prompt

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
        allow_tools: bool = True,
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
            allow_tools
            and scene == "chat"
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
        allow_tools: bool = True,
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
            allow_tools
            and scene == "chat"
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
                async for evt in self._protocol_llm_stream(
                    messages=messages,
                    base_url=override["base_url"],
                    api_key=override["api_key"],
                    model=override["model"],
                    timeout=override["timeout"],
                    scene=scene,
                    usage_user_id=user_id,
                    usage_category=CATEGORY_CHAT,
                ):
                    yield evt
                return
            except Exception as exc:  # noqa: BLE001 - 本地/强模型不可用时回退默认模型
                logger.warning(
                    "普通模式 {} 档模型 {} 流式调用失败，回退默认模型: {}",
                    thinking_mode, override["model"], str(exc)[:160],
                )
        async for evt in self._protocol_llm_stream(
            messages=messages,
            scene=scene,
            usage_user_id=user_id,
            usage_category=CATEGORY_CHAT,
            api_key=llm_api_key,
            reasoning_effort=_chat_reasoning_effort(thinking_mode),
        ):
            yield evt

    async def _protocol_llm_stream(self, messages: list[dict], **stream_kwargs):
        """普通闲聊/直答路径的流式出口（增量即发 + 工具残留剥离）。

        规则：
          - 每条模型增量先经 TextToolStripper 去掉 workspace_read 类 XML/DSML
            残留（跨增量、低滞留），再交 ModelStreamProtocolParser 解析；
          - 解析出的 delta 立即转发（保持真实流式体感）；解析出的
            tool/warning/process 不外发、不执行，也不触发第二次模型调用；
          - 结尾冲刷残留文本。
        """
        from lumi_orch.protocol import (
            ModelStreamProtocolParser,
            TextToolStripper,
            chunk_to_events,
            strip_tool_markup,
        )

        parser = ModelStreamProtocolParser()
        stripper = TextToolStripper()
        try:
            async for raw_delta in self._llm.chat_stream(messages, **stream_kwargs):
                for piece in stripper.feed(str(raw_delta or "")):
                    process_prefix = stripper.drain_process()
                    if process_prefix:
                        yield {"type": "process", "content": process_prefix}
                    for chunk in parser.feed(piece):
                        for event in chunk_to_events(chunk):
                            if event["type"] == "delta":
                                clean = strip_tool_markup(str(event.get("content") or ""))
                                if clean:
                                    yield {"type": "delta", "content": clean}
                            elif event["type"] == "process":
                                yield {"type": "process", "content": str(event.get("content") or "")}
            # 结尾：冲刷剥离器与解析器残留的干净文本。
            for piece in stripper.flush():
                process_prefix = stripper.drain_process()
                if process_prefix:
                    yield {"type": "process", "content": process_prefix}
                for chunk in parser.feed(piece):
                    for event in chunk_to_events(chunk):
                        if event["type"] == "delta":
                            clean = strip_tool_markup(str(event.get("content") or ""))
                            if clean:
                                yield {"type": "delta", "content": clean}
                        elif event["type"] == "process":
                            yield {"type": "process", "content": str(event.get("content") or "")}
            for chunk in parser.finalize():
                for event in chunk_to_events(chunk):
                    if event["type"] == "delta":
                        clean = strip_tool_markup(str(event.get("content") or ""))
                        if clean:
                            yield {"type": "delta", "content": clean}
                    elif event["type"] == "process":
                        yield {"type": "process", "content": str(event.get("content") or "")}
        except asyncio.CancelledError:
            raise

    @staticmethod
    def _protocol_event_to_sse(event: dict) -> dict:
        """把协议解析事件归一化为现有 SSE 载荷。

        delta/process/warning/tool 保持协议名直出（前端按需消费）；其中
        tool 仅做记录，普通闲聊路径不自动执行未请求的工具。
        """
        event_type = str(event.get("type") or "")
        if event_type == "delta":
            return {"type": "delta", "content": str(event.get("content") or "")}
        if event_type == "process":
            return {"type": "process", "content": str(event.get("content") or "")}
        if event_type == "warning":
            return {"type": "warning", "content": str(event.get("content") or "")}
        if event_type == "tool":
            return {"type": "tool", "tool_call": event.get("tool_call") or {}}
        return event

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
