"""认证限流服务 —— 登录失败锁定、全局认证限流.

安全策略（设计文档 6.2 暴力破解防护）:
  - 单账号连续登录失败 5 次，锁定 15 分钟（Redis 计数）
  - 全局限流：单 IP 每分钟最多 20 次认证请求（注册+登录合计）
"""

from loguru import logger

from app.core.config import settings
from app.core.redis import get_redis

# Redis Key 前缀
LOGIN_FAIL_KEY = "auth:login_fail:{account}"   # 账号登录失败计数
LOGIN_LOCK_KEY = "auth:login_lock:{account}"   # 账号锁定标记
AUTH_RATE_KEY = "auth:rate:{ip}"               # IP 认证请求频率（计数）


async def check_account_locked(account: str) -> bool:
    """检查账号是否因连续登录失败被锁定."""
    r = get_redis()
    return bool(await r.exists(LOGIN_LOCK_KEY.format(account=account)))


async def check_and_incr_auth_rate(ip: str) -> bool:
    """检查并递增 IP 认证请求频率，返回是否允许（True=允许，False=超限）."""
    r = get_redis()
    key = AUTH_RATE_KEY.format(ip=ip)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 60)  # 1 分钟窗口
    return count <= settings.AUTH_RATE_LIMIT_PER_MINUTE


async def incr_login_fail(account: str) -> None:
    """递增账号登录失败计数，达到阈值则锁定."""
    r = get_redis()
    key = LOGIN_FAIL_KEY.format(account=account)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, settings.LOGIN_LOCK_MINUTES * 60)
    if count >= settings.LOGIN_MAX_FAIL_COUNT:
        lock_key = LOGIN_LOCK_KEY.format(account=account)
        await r.set(lock_key, "1", ex=settings.LOGIN_LOCK_MINUTES * 60)
        logger.warning(f"账号 {account} 连续登录失败 {count} 次，锁定 {settings.LOGIN_LOCK_MINUTES} 分钟")


async def reset_login_fail(account: str) -> None:
    """登录成功后重置失败计数."""
    r = get_redis()
    await r.delete(LOGIN_FAIL_KEY.format(account=account))