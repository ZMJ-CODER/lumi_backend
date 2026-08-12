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
import uuid
from datetime import datetime, timezone
from pathlib import Path

from httpx import AsyncClient
from loguru import logger

from app.agents.base import AgentContext
from app.agents.registry import AgentRegistry
from app.agents.skills.executor import get_skills_for_scene, run_skill_loop
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.llm import LLMClient
from app.core.llm_config import get_llm_config
from app.core.redis import get_redis
from app.services.speech import speech_to_text
from app.services.rag.query_rewriter import get_retrieval_query
from app.services.rag.knowledge import search_user_knowledge
from app.services.scene_manager import get_scene_config, get_scene_knowledge_tags
from app.services.memory.retrieval import search_user_memories
from app.services.memory.privacy import resolve_decrypt_candidates
from app.services.prompts import get_base_system_prompt, get_prompt_content
from app.services.usage import (
    CATEGORY_CHAT,
    CATEGORY_SUMMARY,
    CATEGORY_TITLE,
    CATEGORY_TOOL_DECISION,
    record_usage,
)
from app.services.web_search import WEB_SEARCH_TOOL, web_search

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
    "你是联网搜索决策助手。判断用户问题是否需要调用 web_search 工具。\n"
    "只要用户明确要求搜索（如搜一下/搜索/查一下/查查/帮我查）、询问最新新闻、实时数据、"
    "当前事件，或问题可能需要模型知识之外的最新信息，就必须调用 web_search 工具。\n"
    "只有纯闲聊、寒暄、情感倾诉这类不需要任何外部信息的请求才不调用。\n"
    "不确定时倾向调用工具。"
)

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
        """从 Redis 获取会话上下文（最近 N 轮）."""
        r = get_redis()
        raw = await r.lrange(CONTEXT_KEY.format(conversation_id=conversation_id), 0, -1)
        return [json.loads(msg) for msg in raw]

    async def append_context(self, conversation_id: str, message: dict) -> None:
        """追加一条消息到 Redis 上下文，并裁剪至 N 轮."""
        r = get_redis()
        key = CONTEXT_KEY.format(conversation_id=conversation_id)
        await r.rpush(key, json.dumps(message, ensure_ascii=False))

        # 裁剪：每轮 = user + assistant，保留最近 N 轮 = 2*N 条消息
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
        """获取会话摘要（Redis，可能为空）."""
        r = get_redis()
        return await r.get(SUMMARY_KEY.format(conversation_id=conversation_id))

    async def save_conversation_summary(self, conversation_id: str, summary: str) -> None:
        """保存会话摘要（7 天 TTL，与上下文一致）."""
        r = get_redis()
        if summary:
            await r.set(SUMMARY_KEY.format(conversation_id=conversation_id), summary, ex=604800)

    async def _generate_summary(
        self, prev_summary: str | None, messages: list[dict], user_id: str
    ) -> str:
        """用 qwen-turbo 生成/接力对话摘要（轻量低成本）. """
        parts: list[str] = []
        if prev_summary:
            parts.append(f"[之前的对话摘要]\n{prev_summary}")
        for m in messages:
            speaker = "用户" if m.get("role") == "user" else "助手"
            parts.append(f"{speaker}: {m.get('content', '')}")
        dialog = "\n".join(parts)

        system_prompt = (
            "你是对话摘要助手。把对话浓缩成简洁的中文摘要，"
            "保留关键信息：用户的偏好与重要事实、双方达成的结论/约定、未完成的任务。"
            "若提供了之前的摘要，新摘要要继承其中仍然重要的信息。只输出摘要本身。"
        )
        try:
            async with AsyncClient(
                base_url=settings.QWEN_BASE_URL,
                headers={"Authorization": f"Bearer {settings.QWEN_API_KEY}"},
                timeout=120,
            ) as client:
                resp = await client.post(
                    "/chat/completions",
                    json={
                        "model": settings.QWEN_TURBO_MODEL,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"对话内容：\n\n{dialog}"},
                        ],
                        "max_tokens": 512,
                        "temperature": 0.3,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage") or {}
                await record_usage(
                    user_id,
                    CATEGORY_SUMMARY,
                    settings.QWEN_TURBO_MODEL,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                )
                summary = (data["choices"][0]["message"]["content"] or "").strip()
                return summary[: settings.CONVERSATION_SUMMARY_MAX_CHARS]
        except Exception as e:
            # 摘要失败不阻塞对话：保留旧摘要，下次触发再试
            logger.warning("对话摘要生成失败: {}", e)
            return prev_summary or ""

    async def _maybe_summarize_context(self, conversation_id: str, user_id: str) -> None:
        """上下文接近 token 预算时：把旧消息压缩成摘要，保留最近 N 轮."""
        history = await self.get_context(conversation_id)
        total = sum(self._estimate_tokens(str(m.get("content") or "")) for m in history)
        if total < settings.CONVERSATION_SUMMARY_TRIGGER_TOKENS:
            return

        keep = settings.CONVERSATION_SUMMARY_KEEP_ROUNDS * 2
        if len(history) <= keep:
            return
        old_msgs = history[:-keep]

        # 被压缩掉的旧消息若尚未做过记忆抽取 → 先异步抽取（批量）
        await self._submit_unextracted(conversation_id, user_id, history, stop=len(history) - keep)

        prev_summary = await self.get_conversation_summary(conversation_id)
        summary = await self._generate_summary(prev_summary, old_msgs, user_id)
        if not summary:
            # 摘要生成失败（且无旧摘要可继承）：不裁剪上下文，下次触发再试
            return
        await self.save_conversation_summary(conversation_id, summary)

        # 裁剪 Redis 上下文：只保留最近 keep 条消息
        r = get_redis()
        key = CONTEXT_KEY.format(conversation_id=conversation_id)
        await r.ltrim(key, -keep, -1)
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

    async def retrieve_memory_facts(self, user_id: str, query: str) -> list[dict]:
        """按当前问题混合检索用户记忆事实（L1 只含占位符）；命中后异步强化."""
        if not query or not query.strip():
            return []
        try:
            async with async_session_factory() as session:
                facts = await search_user_memories(
                    session, user_id, query, top_k=settings.MEMORY_FACT_TOP_K
                )
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
        self, user_id: str, query: str = "", retrieval_query: str | None = None
    ) -> tuple[dict | None, list[dict]]:
        """注入内容 = 画像（常驻） + 与当前问题相关的事实（按需召回）."""
        profile = await self.get_user_profile(user_id)
        facts = await self.retrieve_memory_facts(user_id, retrieval_query or query)
        return profile, facts

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
        web_search_enabled: bool = False,
        llm_api_key: str | None = None,
    ) -> dict:
        """处理用户消息的核心流程（阻塞版，供旧接口/降级路径使用）."""
        transcript = await self._resolve_transcript(content, attachments)
        content = transcript or content

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

        prep = await self._prepare_chat(user_id, conversation_id, content, scene, retrieval_query, attachments)
        image_uris = await self._load_image_data_uris(user_id, attachments)

        # 调用 LLM：工具调用（模型自主决定联网）+ 最终回复
        title = await self.get_conversation_title(conversation_id)
        if prep["is_first"] and not title:
            # 首条消息：回复与标题生成并行（大模型"阅读的同时"总结）
            reply_task = asyncio.create_task(
                self._call_llm_auto(
                    user_id, prep["messages"], scene, image_uris, content, prep["citations"], conversation_id, llm_api_key
                )
            )
            title_task = asyncio.create_task(self._generate_title(content, user_id, llm_api_key))
            reply, title = await asyncio.gather(reply_task, title_task)
            if title:
                await self.save_conversation_title(conversation_id, title)
        else:
            reply = await self._call_llm_auto(
                user_id, prep["messages"], scene, image_uris, content, prep["citations"], conversation_id, llm_api_key
            )

        # 保存助手回复 + 摘要 + 记忆抽取
        await self._finalize_reply(conversation_id, user_id, reply)

        return {
            "message_id": str(uuid.uuid4()),
            "content": reply,
            "citations": prep["citations"],
            "scene": scene,
            "local_mode": False,
            "title": title or "",
            "transcript": transcript,
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
        llm_api_key: str | None = None,
    ):
        """流式处理用户消息：准备流程同 handle_message，LLM 走工具调用 + SSE 流式.

        Yields: {"type": "delta", "content": ...} / {"type": "done", ...}
        """
        transcript = await self._resolve_transcript(content, attachments)
        content = transcript or content

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

        prep = await self._prepare_chat(user_id, conversation_id, content, scene, retrieval_query, attachments)
        image_uris = await self._load_image_data_uris(user_id, attachments)
        message_id = str(uuid.uuid4())
        title = await self.get_conversation_title(conversation_id)

        # 首条消息：标题生成与回复流并行
        title_task = None
        if prep["is_first"] and not title:
            title_task = asyncio.create_task(self._generate_title(content, user_id, llm_api_key))

        full_text = ""
        async for evt in self._stream_llm_auto(
            user_id, prep["messages"], scene, image_uris, content, prep["citations"], conversation_id, llm_api_key
        ):
            if evt["type"] == "delta":
                full_text += evt["content"]
            yield evt

        if title_task is not None:
            title = await title_task
            if title:
                await self.save_conversation_title(conversation_id, title)

        # 保存助手回复 + 摘要 + 记忆抽取
        await self._finalize_reply(conversation_id, user_id, full_text)

        yield {
            "type": "done",
            "message_id": message_id,
            "content": full_text,
            "citations": prep["citations"],
            "scene": scene,
            "title": title or "",
        }

    # ── 消息处理公共流程 ────────────────────────────────

    async def _resolve_transcript(self, content: str, attachments: list | None) -> str:
        """语音附件 → Whisper 转写 + 纠错；无音频返回原内容."""
        transcript = content or ""
        if not transcript.strip():
            for att in attachments or []:
                if isinstance(att, dict) and att.get("type") == "audio" and att.get("url"):
                    transcript = await speech_to_text(str(att["url"]))
                    break
        return transcript

    async def _prepare_chat(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        scene: str,
        retrieval_query: str | None,
        attachments: list | None,
    ) -> dict:
        """LLM 调用前的公共准备：上下文、长期记忆、消息构建、RAG 检索、隐私解密门."""
        # 1. 保存用户消息到上下文
        is_first = len(await self.get_context(conversation_id)) == 0
        user_msg = {"role": "user", "content": content, "timestamp": datetime.now(timezone.utc).isoformat()}
        await self.append_context(conversation_id, user_msg)
        # 新会话首条消息：立即异步抽取（身份类事实（如名字/职业）往往出现在开场白，不等攒批）
        if is_first:
            await self._submit_unextracted(conversation_id, user_id, [user_msg])

        # 2. 上下文 + 长期记忆（画像常驻 + 事实按需召回）
        history = await self.get_context(conversation_id)
        summary = await self.get_conversation_summary(conversation_id)
        profile, memory_facts = await self.get_memory_context(
            user_id, query=content, retrieval_query=retrieval_query
        )

        # 3. 消息列表（System Prompt + 画像 + 记忆 + 摘要 + 历史 + 当前提问）
        system_prompt = await self._get_system_prompt(user_id, scene)
        messages = self._build_messages(
            scene, profile, memory_facts, history, content, summary, system_prompt=system_prompt
        )

        # 4. RAG 知识库检索（按场景过滤空间标签）
        knowledge_tags = get_scene_knowledge_tags(scene)
        search_query = await get_retrieval_query(content, retrieval_query, scene, user_id)
        rag_context, citations = await self._retrieve_knowledge(user_id, search_query, knowledge_tags)
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

    async def _finalize_reply(self, conversation_id: str, user_id: str, reply: str) -> None:
        """保存助手回复到上下文，触发摘要与长期记忆抽取."""
        assistant_msg = {
            "role": "assistant",
            "content": reply,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        await self.append_context(conversation_id, assistant_msg)
        await self._maybe_summarize_context(conversation_id, user_id)
        await self._maybe_extract_memories(conversation_id, user_id)

    # ── 工具调用与流式回复 ─────────────────────────────

    async def _apply_tool_calls(
        self,
        messages: list[dict],
        user_content: str,
        citations: list[dict],
        tool_calls: list[dict],
    ) -> list[dict]:
        """执行模型请求的工具（当前仅 web_search），把结果回填消息列表并追加引用."""
        messages = list(messages) + [{"role": "assistant", "content": None, "tool_calls": tool_calls}]
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name")
            if name != "web_search":
                continue
            try:
                args = (
                    json.loads(fn.get("arguments") or "{}")
                    if isinstance(fn.get("arguments"), str)
                    else (fn.get("arguments") or {})
                )
            except (ValueError, TypeError):
                args = {}
            query = str(args.get("query") or user_content)
            results = await self._retrieve_web(query)
            block = "\n".join(
                f"[{i + 1}] {r['title']}\n{r['url']}\n{r['content'][:800]}"
                for i, r in enumerate(results)
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": block or "未检索到相关结果",
                }
            )
            citations.extend(
                {
                    "type": "web",
                    "title": r["title"] or r["url"],
                    "content": r["content"][:500],
                    "source": r["url"],
                }
                for r in results
            )
        return messages

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
    ) -> str:
        """阻塞版：技能循环（开启时）或 模型自主联网 + 场景模型回复."""
        if image_uris:
            cfg = await get_llm_config(scene, self._llm.provider)
            if self._is_multimodal_model(str(cfg.get("model") or "")):
                messages = self._attach_images(messages, image_uris)

        # 技能模式：LLM function calling 决定调用技能，循环到最终回复
        if settings.AGENT_SKILLS_ENABLED and get_skills_for_scene(scene):
            final_text, records, skill_citations = await run_skill_loop(
                self._llm, user_id, messages, scene, conversation_id=conversation_id, llm_api_key=llm_api_key
            )
            citations.extend(skill_citations)
            return final_text or "（技能调用完成，未能生成回复，请稍后重试）"

        messages = await self._maybe_decide_web(user_id, messages, user_content, citations)
        return await self._llm.chat(
            messages, scene=scene, usage_user_id=user_id, usage_category=CATEGORY_CHAT, api_key=llm_api_key
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
    ):
        """流式版：技能循环（开启时）或 模型自主联网，最终回复流式产出."""
        if image_uris:
            cfg = await get_llm_config(scene, self._llm.provider)
            if self._is_multimodal_model(str(cfg.get("model") or "")):
                messages = self._attach_images(messages, image_uris)

        # 技能模式：循环内逐轮文本产出（工具调用中的"我来搜索…" + 最终回复）
        if settings.AGENT_SKILLS_ENABLED and get_skills_for_scene(scene):
            deltas: list[str] = []
            final_text, records, skill_citations = await run_skill_loop(
                self._llm,
                user_id,
                messages,
                scene,
                conversation_id=conversation_id,
                llm_api_key=llm_api_key,
                on_text=deltas.append,
            )
            citations.extend(skill_citations)
            for piece in deltas:
                yield {"type": "delta", "content": piece}
            return

        messages = await self._maybe_decide_web(user_id, messages, user_content, citations)
        async for delta in self._llm.chat_stream(
            messages, scene=scene, usage_user_id=user_id, usage_category=CATEGORY_CHAT, api_key=llm_api_key
        ):
            yield {"type": "delta", "content": delta}

    async def _maybe_decide_web(
        self, user_id: str, messages: list[dict], user_content: str, citations: list[dict]
    ) -> list[dict]:
        """模型自主决策是否联网：qwen-plus 工具调用；未调用则原样返回."""
        if not settings.WEB_SEARCH_TOOL_ENABLED:
            return messages
        question = self._extract_user_text(messages)
        if not question:
            return messages
        # 决策用独立的轻量提示，避免被对话人格/上下文带偏（只判断是否需要联网）
        decision_messages = [
            {"role": "system", "content": _WEB_DECISION_PROMPT},
            {"role": "user", "content": question},
        ]
        try:
            _, tool_calls = await self._llm.chat_with_tools_qwen(
                decision_messages,
                [WEB_SEARCH_TOOL],
                max_tokens=64,
                usage_user_id=user_id,
                usage_category=CATEGORY_TOOL_DECISION,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("联网工具决策调用失败，跳过联网: {}", exc)
            return messages
        if tool_calls:
            return await self._apply_tool_calls(messages, user_content, citations, tool_calls)
        return messages

    @staticmethod
    def _extract_user_text(messages: list[dict]) -> str:
        """取最后一条用户消息的纯文本（多模态 content 列表时取 text 分片）."""
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "\n".join(t for t in texts if t)
        return ""

    # ── 内部方法 ────────────────────────────────────────

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """粗略估算 token 数：中文按 1 字符 ≈ 1 token，其他按 3 字符 ≈ 1 token."""
        if not text:
            return 0
        cjk = sum(
            1 for ch in text
            if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef"
        )
        other = len(text) - cjk
        return cjk + other // 3 + 2

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

        # 隐私规则（恒常附加）
        system_prompt += _PRIVACY_RULES

        messages = [{"role": "system", "content": system_prompt}]

        # 注入最近历史（token 预算内），排除最后一条（当前消息已包含）
        for msg in self._trim_history(history, settings.LLM_HISTORY_MAX_TOKENS):
            messages.append({"role": msg["role"], "content": msg["content"]})

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
            lines.append(f"- [{t}] {text}{suffix}")
        return "\n".join(lines)

    async def _retrieve_knowledge(self, user_id: str, query: str, space_tags: list[str]) -> tuple[str, list[dict]]:
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
                )
        except Exception as e:
            # 检索失败不阻塞对话主流程，仅记录并跳过知识库
            logger.warning("RAG 检索失败，跳过知识库: {}", e)
            return "", []

    async def _retrieve_web(self, query: str) -> list[dict]:
        """联网搜索（Tavily）；失败不阻塞对话."""
        try:
            return await web_search(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning("联网搜索失败: {}", exc)
            return []

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
