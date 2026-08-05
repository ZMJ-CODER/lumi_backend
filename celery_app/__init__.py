"""Celery 应用配置 —— 异步任务队列."""

from celery import Celery

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
