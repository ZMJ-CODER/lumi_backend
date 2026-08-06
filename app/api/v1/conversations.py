"""对话模块 API —— 增强场景模式联动."""

from fastapi import APIRouter, Depends, Query
from loguru import logger

from app.core.deps import require_auth
from app.models.conversation import (
    CreateConversationRequest,
    SendMessageRequest,
    UpdateConversationRequest,
)
from app.services.orchestrator import orchestrator

router = APIRouter()


@router.post("")
async def create_conversation(req: CreateConversationRequest, payload: dict = Depends(require_auth)):
    """创建新会话.

    请求体: {"scene": "chat"}
    场景决定: System Prompt、知识库检索范围、电脑操控权限边界
    """
    # TODO: 写入 conversations 表
    return {"code": 0, "data": {"conversation_id": "placeholder-conv-id", "scene": req.scene}}


@router.get("")
async def list_conversations(
    scene: str = Query(default="chat"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    payload: dict = Depends(require_auth),
):
    """获取会话列表，可按场景筛选."""
    # TODO: 从数据库分页查询，WHERE user_id = ? AND scene = ? AND is_deleted = false
    return {"code": 0, "data": {"items": [], "total": 0, "limit": limit, "offset": offset}}


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    req: SendMessageRequest,
    payload: dict = Depends(require_auth),
):
    """发送消息（云端对话）.

    处理流程:
      1. 加载场景配置 (System Prompt + 知识库标签)
      2. 从 Redis 加载会话上下文 + 长期记忆
      3. RAG 检索知识库（按场景过滤空间标签）
      4. 调用 LLM 生成回复
      5. 保存消息到 Redis 上下文 + PostgreSQL
      6. 异步触发记忆提取
    """
    user_id = payload.get("sub", "")

    # ── 联调日志：确认前端消息是否到达后端 ──
    logger.info(
        "📥 [收到前端消息] "
        f"user_id={user_id} | conversation_id={conversation_id} | "
        f"scene={req.scene} | local_mode={req.local_mode} | "
        f"content={req.content!r} | content_len={len(req.content)}"
    )

    result = await orchestrator.handle_message(
        user_id=user_id,
        conversation_id=conversation_id,
        content=req.content,
        scene=req.scene,
        local_mode=req.local_mode,
    )

    logger.info(
        "📤 [返回AI回复给前端] "
        f"user_id={user_id} | conversation_id={conversation_id} | "
        f"message_id={result.get('message_id')} | "
        f"content={result.get('content')!r} | citations_count={len(result.get('citations') or [])}"
    )

    return {"code": 0, "data": result}


@router.get("/{conversation_id}/messages")
async def get_messages(
    conversation_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    before_message_id: str | None = Query(default=None),
    payload: dict = Depends(require_auth),
):
    """获取会话消息历史（从 PostgreSQL 分页查询）."""
    # TODO: 分页查询 messages 表
    return {"code": 0, "data": {"items": [], "has_more": False}}


@router.delete("/{conversation_id}")
async def delete_conversation(conversation_id: str, payload: dict = Depends(require_auth)):
    """软删除会话."""
    # TODO: UPDATE conversations SET is_deleted = true WHERE id = ?
    # 同时清除 Redis 上下文
    await orchestrator.clear_context(conversation_id)
    return {"code": 0, "message": "已删除"}


@router.patch("/{conversation_id}")
async def update_conversation_title(
    conversation_id: str,
    req: UpdateConversationRequest,
    payload: dict = Depends(require_auth),
):
    """更新会话标题."""
    # TODO: UPDATE conversations SET title = ? WHERE id = ?
    return {"code": 0, "message": "已更新"}


@router.get("/scenes")
async def list_scenes():
    """获取所有可用场景模式."""
    scenes = await orchestrator.list_scenes()
    return {"code": 0, "data": {"scenes": scenes}}
