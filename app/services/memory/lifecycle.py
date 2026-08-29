"""长期记忆生命周期的统一计算规则。"""

from datetime import datetime, timedelta, timezone

from app.core.config import settings


def expire_at_for_memory_type(
    memory_type: str,
    *,
    created_at: datetime | None = None,
) -> datetime | None:
    """返回记忆的绝对到期时间；identity 类型永不到期。

    ``MEMORY_HALF_LIFE_DAYS`` 同时驱动检索时的时效衰减和持久化
    生命周期，避免两套规则随着配置演进而分叉。
    """
    half_life_days = settings.MEMORY_HALF_LIFE_DAYS.get(memory_type)
    if half_life_days is None:
        return None

    reference = created_at or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference + timedelta(days=half_life_days)
