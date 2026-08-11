"""API 路由汇总 —— 按设计文档模块组织."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    chat,
    control_logs,
    conversations,
    data_export,
    health,
    knowledge,
    local,
    local_corpus,
    memories,
    prompts,
    public_kb,
    tts,
    uploads,
    user,
)

api_router = APIRouter()

# 认证 & 用户
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(user.router, prefix="/user", tags=["user"])

# 健康检查
api_router.include_router(health.router, prefix="/health", tags=["health"])

# 对话（含场景管理）
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])

# 聊天（流式 SSE）
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])

# 按需文字转语音
api_router.include_router(tts.router, prefix="/tts", tags=["tts"])

# 知识库
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])

# 长期记忆调试（superadmin）
api_router.include_router(memories.router, prefix="/admin/memories", tags=["admin"])

# 角色提示词
api_router.include_router(prompts.router, prefix="/prompts", tags=["prompts"])

# 操控日志
api_router.include_router(control_logs.router, prefix="/control-logs", tags=["control-logs"])

# 管理员
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])

# 公共知识库
api_router.include_router(public_kb.router, prefix="/public-kb", tags=["public-kb"])

# 本地加速协同
api_router.include_router(local_corpus.router, prefix="/local-corpus", tags=["local-corpus"])
api_router.include_router(local.router, prefix="/local", tags=["local"])

# 聊天附件上传
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])

# 数据导出 & 账号删除（隐私保护）
api_router.include_router(data_export.router, prefix="/user", tags=["data-export"])
