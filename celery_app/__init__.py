"""Celery 应用配置 —— 异步任务队列."""

from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from app.core.config import settings

celery_app = Celery(
    "lumi_tasks",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["celery_app.tasks"],
)

QUEUE_DURABLE = "durable"
QUEUE_BEST_EFFORT = "best_effort"
QUEUE_MAINTENANCE = "maintenance"
_default_exchange = Exchange("lumi", type="direct")

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT_SECONDS,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT_SECONDS,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    broker_transport_options={
        "visibility_timeout": settings.CELERY_REDIS_VISIBILITY_TIMEOUT_SECONDS,
    },
    task_default_queue=QUEUE_DURABLE,
    task_default_exchange="lumi",
    task_default_routing_key=QUEUE_DURABLE,
    task_queues=(
        Queue(QUEUE_DURABLE, _default_exchange, routing_key=QUEUE_DURABLE),
        Queue(QUEUE_BEST_EFFORT, _default_exchange, routing_key=QUEUE_BEST_EFFORT),
        Queue(QUEUE_MAINTENANCE, _default_exchange, routing_key=QUEUE_MAINTENANCE),
    ),
    task_routes={
        "celery_app.tasks.process_document": {"queue": QUEUE_DURABLE},
        "celery_app.tasks.rebuild_index": {"queue": QUEUE_DURABLE},
        "celery_app.tasks.cleanup_vectors": {"queue": QUEUE_DURABLE},
        "celery_app.tasks.delete_user_data": {"queue": QUEUE_DURABLE},
        "celery_app.tasks.extract_memories": {"queue": QUEUE_BEST_EFFORT},
        "celery_app.tasks.maintain_conversation_memory_task": {"queue": QUEUE_BEST_EFFORT},
        "celery_app.tasks.build_user_profile": {"queue": QUEUE_BEST_EFFORT},
        "celery_app.tasks.touch_memories": {"queue": QUEUE_BEST_EFFORT},
        "celery_app.tasks.trim_conversation_messages": {"queue": QUEUE_MAINTENANCE},
        "celery_app.tasks.build_all_user_profiles": {"queue": QUEUE_MAINTENANCE},
        "celery_app.tasks.cleanup_memories": {"queue": QUEUE_MAINTENANCE},
        "celery_app.tasks.cleanup_conversations": {"queue": QUEUE_MAINTENANCE},
        "celery_app.tasks.cleanup_generated_files": {"queue": QUEUE_MAINTENANCE},
        "celery_app.tasks.aggregate_token_stats": {"queue": QUEUE_MAINTENANCE},
        "celery_app.tasks.recover_stale_documents": {"queue": QUEUE_MAINTENANCE},
    },
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
    "recover-stale-documents": {
        "task": "celery_app.tasks.recover_stale_documents",
        "schedule": crontab(minute="*/10"),
    },
}
