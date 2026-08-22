"""API 路由汇总 —— 按设计文档模块组织."""

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    admin_mcp,
    admin_system,
    agents,
    auth,
    call,
    chat,
    control_logs,
    conversations,
    data_export,
    health,
    knowledge,
    local,
    local_corpus,
    mcp,
    memory_manage,
    memories,
    office_docs,
    preferences,
    prompts,
    projects,
    public_kb,
    tts,
    tools,
    uploads,
    usage,
    user,
)

api_router = APIRouter()

# 认证 & 用户
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(user.router, prefix="/user", tags=["user"])
api_router.include_router(usage.router, prefix="/usage", tags=["usage"])

# 多智能体协作（办公模式：任务编排 / 状态管理）
api_router.include_router(agents.router, prefix="/agents", tags=["agents"])

# 健康检查
api_router.include_router(health.router, prefix="/health", tags=["health"])

# 对话（含场景管理）
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])

# 聊天（流式 SSE）
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])

# 按需文字转语音
api_router.include_router(tts.router, prefix="/tts", tags=["tts"])

# 语音通话（Whisper + DS Flash + 流式 TTS）
api_router.include_router(call.router, prefix="/call", tags=["call"])

# 用户个性化偏好（多端同步）
api_router.include_router(preferences.router, prefix="/preferences", tags=["preferences"])

# 用户记忆管理（查看/检索/删除自己的记忆与画像）
api_router.include_router(memory_manage.router, prefix="/memory", tags=["memory"])

# 客户端工具（本地文件操作等：用户端轮询 + 结果回传）
api_router.include_router(tools.router, prefix="/tools", tags=["tools"])

# 用户显式绑定的第三方 MCP 工具（配置白名单 + 用户授权后才进入候选池）
api_router.include_router(mcp.router, prefix="/mcp", tags=["mcp"])

# 知识库
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])

# 长期记忆调试（superadmin）
api_router.include_router(memories.router, prefix="/admin/memories", tags=["admin"])
api_router.include_router(office_docs.router, prefix="/office/docs", tags=["office"])

# 角色提示词
api_router.include_router(prompts.router, prefix="/prompts", tags=["prompts"])

# 本地项目（方案 A：结构索引，代码留本地）
api_router.include_router(projects.router, prefix="/projects", tags=["projects"])

# 操控日志
api_router.include_router(control_logs.router, prefix="/control-logs", tags=["control-logs"])

# 管理员
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(admin_mcp.router, prefix="/admin/mcp", tags=["admin"])
api_router.include_router(admin_system.router, prefix="/admin/system", tags=["admin"])

# 公共知识库
api_router.include_router(public_kb.router, prefix="/public-kb", tags=["public-kb"])

# 本地加速协同
api_router.include_router(local_corpus.router, prefix="/local-corpus", tags=["local-corpus"])
api_router.include_router(local.router, prefix="/local", tags=["local"])

# 聊天附件上传
api_router.include_router(uploads.router, prefix="/uploads", tags=["uploads"])

# 数据导出 & 账号删除（隐私保护）
api_router.include_router(data_export.router, prefix="/user", tags=["data-export"])
