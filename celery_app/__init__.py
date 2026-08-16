"""Celery 应用配置 —— 异步任务队列."""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "lumi_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["celery_app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 单任务最长 30 分钟
    task_soft_time_limit=25 * 60,
)

# 定时任务（Celery beat）
celery_app.conf.beat_schedule = {
    # 每日凌晨重建所有用户画像（事实库 → memory_profile）
    "build-all-user-profiles": {
        "task": "celery_app.tasks.build_all_user_profiles",
        "schedule": crontab(hour=4, minute=0),
    },
    # 每日清理：过期低重要度记忆 / superseded / 重要度微调
    "cleanup-memories": {
        "task": "celery_app.tasks.cleanup_memories",
        "schedule": crontab(hour=3, minute=30),
    },
    # 每日兜底：聊天记录超过硬上限的会话裁剪（消息 + 附件）
    "cleanup-conversations": {
        "task": "celery_app.tasks.cleanup_conversations",
        "schedule": crontab(hour=3, minute=0),
    },
    # 每日清理后端生成的临时/产物文件（办公会话、脚本产物、沙箱残留）
    "cleanup-generated-files": {
        "task": "celery_app.tasks.cleanup_generated_files",
        "schedule": crontab(hour=1, minute=0),
    },
    # 每日聚合 LLM token 用量（低频率，避免统计查询压力）
    "aggregate-token-stats": {
        "task": "celery_app.tasks.aggregate_token_stats",
        "schedule": crontab(hour=2, minute=0),
    },
}
