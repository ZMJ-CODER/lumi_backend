"""兼容聊天智能体 —— Redis 短期记忆 + LangChain LLM 门面。"""

import json

from loguru import logger

from app.agents.base import AgentBase, AgentContext
from app.agents.registry import AgentRegistry
from app.core.llm import LLMClient
from app.core.redis import get_redis
from app.services.prompts import get_base_system_prompt
from app.services.scene_manager import get_scene_system_prompt


_HISTORY_KEY = "agent:chat:history:{user_id}:{session_id}"
_HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60


class ChatAgent(AgentBase):
    """简单对话智能体.

    这是 ``Orchestrator.route_and_execute`` 的兼容入口。短期记忆存 Redis，
    以用户和会话联合隔离；主对话链路仍由 ``services.orchestrator`` 维护。
    """

    name = "chat_agent"
    description = "简单对话智能体，支持短期记忆与多轮对话"
    supported_scenes = ["chat"]

    # 短期记忆：每个 session 保留的最大轮数（1 轮 = user + assistant）
    MAX_HISTORY_ROUNDS = 10

    def __init__(self) -> None:
        self._llm = LLMClient()
        self._llm_started = False

    @staticmethod
    def _history_key(context: AgentContext) -> str:
        return _HISTORY_KEY.format(
            user_id=context.user_id or "anonymous",
            session_id=context.session_id or "default",
        )

    async def _load_history(self, key: str) -> list[dict]:
        try:
            rows = await get_redis().lrange(key, 0, -1)
            history: list[dict] = []
            for row in rows:
                if isinstance(row, bytes):
                    row = row.decode("utf-8", errors="replace")
                value = json.loads(row)
                if isinstance(value, dict) and value.get("role") in {"user", "assistant"}:
                    history.append(value)
            return history
        except Exception as exc:  # noqa: BLE001 - compatibility path remains stateless on Redis failure
            logger.warning("ChatAgent 读取 Redis 短期记忆失败，本轮按无历史处理: {}", exc)
            return []

    async def _append_turn(self, key: str, message: str, reply: str) -> None:
        try:
            redis = get_redis()
            async with redis.pipeline(transaction=True) as pipe:
                pipe.rpush(
                    key,
                    json.dumps({"role": "user", "content": message}, ensure_ascii=False),
                    json.dumps({"role": "assistant", "content": reply}, ensure_ascii=False),
                )
                pipe.ltrim(key, -(self.MAX_HISTORY_ROUNDS * 2), -1)
                pipe.expire(key, _HISTORY_TTL_SECONDS)
                await pipe.execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChatAgent 保存 Redis 短期记忆失败（不影响回复）: {}", exc)

    async def execute(self, message: str, context: AgentContext) -> str:
        """执行对话，返回模型回复文本.

        Args:
            message: 用户消息
            context: 智能体执行上下文（含 session_id、scene 等）

        Returns:
            模型回复文本
        """
        # 1. 懒启动 LLM 客户端
        if not self._llm_started:
            await self._llm.start()
            self._llm_started = True

        # 2. 读取按 ``user_id + session_id`` 隔离的 Redis 短期记忆。
        session_key = self._history_key(context)
        history = await self._load_history(session_key)

        # 5. 构建消息列表（一级安全规范 + 二级场景角色 + 历史上下文）
        system_prompt = (
            f"{get_base_system_prompt()}\n\n[角色设定]\n{get_scene_system_prompt(context.scene)}"
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": message})

        # 6. 调用当前用户/服务端生效的模型配置。
        try:
            reply = await self._llm.chat(
                messages,
                scene=context.scene,
                usage_user_id=context.user_id,
            )
        except Exception as e:
            logger.error(f"ChatAgent LLM 调用失败: {e}")
            return f"抱歉，我暂时无法回复：{e}"

        # 7. 在成功后原子追加本轮；失败不会污染下一轮上下文。
        await self._append_turn(session_key, message, reply)

        return reply

    async def clear_memory(self, session_id: str, user_id: str = "") -> None:
        """清除指定用户会话的 Redis 短期记忆。"""
        try:
            await get_redis().delete(
                _HISTORY_KEY.format(user_id=user_id or "anonymous", session_id=session_id or "default")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ChatAgent 清除 Redis 短期记忆失败: {}", exc)


# 模块加载时自动注册到注册中心
AgentRegistry.register(ChatAgent())
