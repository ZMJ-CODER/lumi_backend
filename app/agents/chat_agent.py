"""简单对话智能体 —— 支持短期记忆的基础聊天 Agent."""

from loguru import logger

from app.agents.base import AgentBase, AgentContext
from app.agents.registry import AgentRegistry
from app.core.llm import LLMClient
from app.services.prompts import get_base_system_prompt
from app.services.scene_manager import get_scene_system_prompt


class ChatAgent(AgentBase):
    """简单对话智能体.

    功能:
      - 基础多轮对话
      - 短期记忆（基于 session_id 的内存历史消息，保留最近 N 轮）
      - 调用 DeepSeek 模型（deepseek-v4-flash）

    注意:
      - 短期记忆存储在进程内存中，服务重启后会丢失
      - 并发请求同一 session 时未加锁，简单场景下可接受
    """

    name = "chat_agent"
    description = "简单对话智能体，支持短期记忆与多轮对话"
    supported_scenes = ["chat"]

    # 用户指定的模型名称
    MODEL_NAME = "deepseek-v4-flash"

    # 短期记忆：每个 session 保留的最大轮数（1 轮 = user + assistant）
    MAX_HISTORY_ROUNDS = 10

    def __init__(self) -> None:
        self._llm = LLMClient(provider="deepseek")
        self._llm_started = False
        # 短期记忆：session_id -> [{"role": ..., "content": ...}, ...]
        self._history: dict[str, list[dict]] = {}

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

        # 2. 获取会话标识（优先 session_id，其次 user_id，最后默认）
        session_key = context.session_id or context.user_id or "default"

        # 3. 加载短期记忆
        history = self._history.setdefault(session_key, [])

        # 4. 追加当前用户消息到记忆
        history.append({"role": "user", "content": message})

        # 5. 构建消息列表（一级安全规范 + 二级场景角色 + 历史上下文）
        system_prompt = (
            f"{get_base_system_prompt()}\n\n[角色设定]\n{get_scene_system_prompt(context.scene)}"
        )
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)

        # 6. 调用 LLM（指定 deepseek-v4-flash 模型）
        try:
            reply = await self._llm.chat(messages, model=self.MODEL_NAME)
        except Exception as e:
            logger.error(f"ChatAgent LLM 调用失败: {e}")
            # 调用失败时回滚用户消息，避免污染上下文
            history.pop()
            return f"抱歉，我暂时无法回复：{e}"

        # 7. 保存助手回复到短期记忆
        history.append({"role": "assistant", "content": reply})

        # 8. 裁剪历史（保留最近 N 轮 = 2*N 条消息）
        max_len = self.MAX_HISTORY_ROUNDS * 2
        if len(history) > max_len:
            self._history[session_key] = history[-max_len:]

        return reply

    def clear_memory(self, session_id: str) -> None:
        """清除指定会话的短期记忆."""
        self._history.pop(session_id, None)


# 模块加载时自动注册到注册中心
AgentRegistry.register(ChatAgent())
