"""用户数据导出 API —— 设计文档 4.1「用户可随时导出自己的完整数据」."""

from fastapi import APIRouter, Depends

from app.core.deps import require_auth

router = APIRouter()


@router.post("/export")
async def export_my_data(payload: dict = Depends(require_auth)):
    """导出当前用户的完整云端数据.

    返回:
      - 对话历史（所有会话的消息列表）
      - 长期记忆列表
      - 知识库文档列表（文件名、大小、上传时间）
      - 操控日志摘要

    不包含：文档原始内容、向量数据
    """
    user_id = payload.get("sub", "")
    # TODO: 查询所有关联数据并打包
    return {
        "code": 0,
        "data": {
            "user_id": user_id,
            "exported_at": "",
            "conversations": [],  # [{conversation_id, title, scene, messages: [...]}]
            "memories": [],  # [{memory_id, fact, importance, created_at}]
            "documents": [],  # [{document_id, filename, file_size, status, created_at}]
            "control_logs_count": 0,
        },
    }


@router.delete("/account")
async def delete_my_data(payload: dict = Depends(require_auth)):
    """删除当前用户的所有云端数据（需二次确认）.

    设计文档承诺：服务端 24 小时内执行并确认。
    实际实现：立即软删除，后台任务物理清理。
    """
    user_id = payload.get("sub", "")
    # TODO:
    # 1. 软删除用户 (is_active = false)
    # 2. Celery 任务：24h 内物理删除 conversations, messages, memories,
    #    document_chunks, documents, knowledge_spaces (非公共), control_logs
    # 3. 保留操作审计日志（不含内容）
    return {"code": 0, "message": "数据删除请求已提交，将在 24 小时内完成清理"}
