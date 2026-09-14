"""cleanup_job_secrets_activity（activities 的 lifecycle 族）。"""

from temporalio import activity


@activity.defn
async def cleanup_job_secrets_activity(job_id: str) -> None:
    """任务正常结束时删除 BYOK 临时 key（取消/中断路径由 TTL 兜底清理）."""
    if job_id:
        from app.agents.orchestration.temporal.client import delete_byok_key

        await delete_byok_key(job_id)
