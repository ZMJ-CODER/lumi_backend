"""多智能体编排服务 —— 会话上下文管理、记忆注入、智能体路由.

核心职责:
  1. 维护 Redis 中的短期对话上下文（最近 N 轮）
  2. 注入长期记忆关键事实
  3. 路由到对应场景的智能体
  4. 触发异步记忆提取
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone

from loguru import logger

from app.agents.base import AgentContext
from app.agents.loop import maybe_run_skills
from app.agents.registry import AgentRegistry
from app.core.config import settings
from app.core.database import async_session_factory
from app.core.llm import LLMClient
from app.core.redis import get_redis
from app.services.rag.query_rewriter import get_retrieval_query
from app.services.rag.knowledge import search_user_knowledge
from app.services.scene_manager import get_scene_config, get_scene_knowledge_tags

# Redis Key 模板
CONTEXT_KEY = "conv:ctx:{conversation_id}"  # 会话上下文 (list of json)
MEMORY_CACHE_KEY = "mem:user:{user_id}"  # 用户长期记忆缓存
TITLE_KEY = "conv:title:{conversation_id}"  # 会话标题

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
            logger.info("🔌 LLMClient 已启动 (provider={})", self._llm.provider)

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

    async def get_conversation_title(self, conversation_id: str) -> str | None:
        """获取会话标题（Redis，可能为空）."""
        r = get_redis()
        return await r.get(TITLE_KEY.format(conversation_id=conversation_id))

    async def save_conversation_title(self, conversation_id: str, title: str) -> None:
        """保存会话标题（7 天 TTL，与上下文一致）."""
        r = get_redis()
        await r.set(TITLE_KEY.format(conversation_id=conversation_id), title, ex=604800)

    async def _generate_title(self, content: str) -> str:
        """用大模型生成会话标题（轻量调用，首条消息时与回复并行）."""
        try:
            reply = await self._llm.chat(
                [
                    {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                max_tokens=32,
            )
            title = reply.strip().strip('"“”').strip()
            return title[:30]
        except Exception as e:
            logger.warning("会话标题生成失败: {}", e)
            return ""

    # ── 记忆注入 ────────────────────────────────────────

    async def get_memory_context(self, user_id: str, limit: int = 5) -> list[str]:
        """获取用户长期记忆的关键事实列表（优先高重要度、最近访问）."""
        r = get_redis()
        key = MEMORY_CACHE_KEY.format(user_id=user_id)
        cached = await r.get(key)
        if cached:
            return json.loads(cached)
        # TODO: 缓存未命中时从 PostgreSQL 加载
        return []

    async def cache_memories(self, user_id: str, facts: list[str]) -> None:
        """缓存用户长期记忆到 Redis（TTL 1 小时）."""
        r = get_redis()
        key = MEMORY_CACHE_KEY.format(user_id=user_id)
        await r.set(key, json.dumps(facts, ensure_ascii=False), ex=3600)

    # ── 消息处理主流程 ──────────────────────────────────

    async def handle_message(
        self,
        user_id: str,
        conversation_id: str,
        content: str,
        scene: str = "chat",
        local_mode: bool = False,
        retrieval_query: str | None = None,
    ) -> dict:
        """处理用户消息的核心流程.

        Args:
            user_id: 用户 UUID
            conversation_id: 会话 UUID
            content: 用户消息内容
            scene: 场景模式 (chat/office/game)
            local_mode: 是否为本地模式请求（PC端已本地处理，仅同步摘要）

        Returns:
            {message_id, content, citations, scene}
        """
        scene_config = get_scene_config(scene)

        # 本地模式：仅记录，不生成回复（PC端已处理）
        if local_mode:
            return {
                "message_id": str(uuid.uuid4()),
                "content": "",
                "citations": [],
                "scene": scene,
                "local_mode": True,
                "title": "",
            }

        # 1. 保存用户消息到上下文
        is_first = len(await self.get_context(conversation_id)) == 0  # 首条消息 → 生成会话标题
        user_msg = {"role": "user", "content": content, "timestamp": datetime.now(timezone.utc).isoformat()}
        await self.append_context(conversation_id, user_msg)

        # 2. 获取上下文 + 长期记忆
        history = await self.get_context(conversation_id)
        memory_facts = await self.get_memory_context(user_id)

        # 3. 构建消息列表（System Prompt + 记忆 + 历史 + 当前提问）
        messages = self._build_messages(scene, memory_facts, history, content)

        # 4. 知识库检索（按场景过滤空间标签）
        knowledge_tags = get_scene_knowledge_tags(scene)
        # 检索查询（可插拔槽位）：客户端精炼 > 服务端小模型重写 > 原文
        search_query = await get_retrieval_query(content, retrieval_query)
        rag_context, citations = await self._retrieve_knowledge(user_id, search_query, knowledge_tags)

        if rag_context:
            messages[-1]["content"] = f"参考以下知识库内容回答用户问题：\n\n{rag_context}\n\n用户问题：{content}"

        # 5. 调用 LLM 生成回复（按场景动态取模型配置）
        title = await self.get_conversation_title(conversation_id)
        if is_first and not title:
            # 首条消息：回复与标题生成并行（大模型"阅读的同时"总结）
            reply_task = asyncio.create_task(self._call_llm(messages, scene=scene))
            title_task = asyncio.create_task(self._generate_title(content))
            reply, title = await asyncio.gather(reply_task, title_task)
            if title:
                await self.save_conversation_title(conversation_id, title)
        else:
            reply = await self._call_llm(messages, scene=scene)

        # 技能调用循环（预留沙箱槽位）：默认关闭，不影响现有流程；
        # 二期实现后，在此把技能结果回填 LLM 继续对话
        tool_results = await maybe_run_skills(conversation_id, scene, messages, user_id=user_id)
        _ = tool_results  # 预留：二期在此把技能结果回填 LLM 继续对话

        # 6. 保存助手回复到上下文
        assistant_msg = {"role": "assistant", "content": reply, "timestamp": datetime.now(timezone.utc).isoformat()}
        await self.append_context(conversation_id, assistant_msg)

        # 7. 异步触发记忆提取（TODO: Celery 任务）
        # extract_memory.delay(user_id, conversation_id, content, reply)

        return {
            "message_id": str(uuid.uuid4()),
            "content": reply,
            "citations": citations,
            "scene": scene,
            "local_mode": False,
            "title": title or "",
        }

    # ── 内部方法 ────────────────────────────────────────

    def _build_messages(self, scene: str, memory_facts: list[str], history: list[dict], current: str) -> list[dict]:
        """构建 LLM 请求消息列表."""
        system_prompt = get_scene_config(scene)["system_prompt"]

        # 注入长期记忆
        if memory_facts:
            facts_text = "\n".join(f"- {f}" for f in memory_facts)
            system_prompt += f"\n\n[关于用户的长期记忆]\n{facts_text}"

        messages = [{"role": "system", "content": system_prompt}]

        # 注入历史上下文（排除最后一条，因为当前消息已包含）
        for msg in history[:-1]:
            messages.append({"role": msg["role"], "content": msg["content"]})

        messages.append({"role": "user", "content": current})
        return messages

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

    async def _call_llm(self, messages: list[dict], scene: str = "chat") -> str:
        """调用云端 LLM 生成回复（配置动态读取: Redis → .env）."""
        await self._ensure_llm_started()
        reply = await self._llm.chat(messages, scene=scene)
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
