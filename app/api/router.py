"""API 路由汇总 —— 按设计文档模块组织."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    auth,
    control_logs,
    conversations,
    data_export,
    knowledge,
    local,
    local_corpus,
    memories,
    public_kb,
    user,
)

api_router = APIRouter()

# 认证 & 用户
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(user.router, prefix="/user", tags=["user"])

# 对话（含场景管理）
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])

# 知识库
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])

# 记忆
api_router.include_router(memories.router, prefix="/memories", tags=["memories"])

# 操控日志
api_router.include_router(control_logs.router, prefix="/control-logs", tags=["control-logs"])

# 管理员
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])

# 公共知识库
api_router.include_router(public_kb.router, prefix="/public-kb", tags=["public-kb"])

# 本地加速协同
api_router.include_router(local_corpus.router, prefix="/local-corpus", tags=["local-corpus"])
api_router.include_router(local.router, prefix="/local", tags=["local"])

# 数据导出 & 账号删除（隐私保护）
api_router.include_router(data_export.router, prefix="/user", tags=["data-export"])
