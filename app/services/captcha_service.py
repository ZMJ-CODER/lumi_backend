"""图形验证码服务 —— 算式验证码生成与校验.

使用 captcha 库生成 PNG 格式的算式验证码图片，Base64 编码返回给前端.
验证码答案存入 Redis（5 分钟有效），注册/登录时校验.

安全策略（设计文档 5.2）:
  - 同一 IP 每分钟最多获取 10 次验证码
  - 连续输错验证码 5 次，锁定该 IP 30 分钟
"""

import base64
import secrets
import uuid

from captcha.image import ImageCaptcha
from loguru import logger

from app.core.config import settings
from app.core.redis import get_redis

# 验证码图片生成器
_image_captcha = ImageCaptcha(width=200, height=70, fonts=None, font_sizes=(40, 36, 32))

# Redis Key 前缀
CAPTCHA_KEY = "captcha:{captcha_id}"          # 验证码答案
CAPTCHA_RATE_KEY = "captcha:rate:{ip}"        # IP 获取频率（计数）
CAPTCHA_FAIL_KEY = "captcha:fail:{ip}"        # IP 连续错误计数
CAPTCHA_LOCK_KEY = "captcha:lock:{ip}"        # IP 锁定标记

# 验证码有效期（秒）
CAPTCHA_TTL = 300  # 5 分钟

# 算式范围 —— 加减乘法，数字控制在 1-20 以内
OPERANDS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "×": lambda a, b: a * b,
}


def _generate_expression() -> tuple[str, int]:
    """生成随机加减乘法算式.

    规则（设计文档 5.1：两个 1-20 的整数和一个运算符 +/-/×）:
      - 加法: a + b, a 和 b 在 1-20 范围内
      - 减法: a - b, a 在 5-20 范围内, b 在 1-a 范围内, 确保结果 ≥ 1
      - 乘法: a × b, a 和 b 在 1-9 范围内, 避免结果过大难以心算

    Returns:
        (表达式字符串, 计算结果)
        例: ("13 + 7", 20)
    """
    op = secrets.choice(list(OPERANDS.keys()))
    if op == "-":
        a = secrets.randbelow(16) + 5   # 5-20
        b = secrets.randbelow(a) + 1    # 1-a，确保结果 ≥ 1
    elif op == "×":
        a = secrets.randbelow(9) + 1    # 1-9
        b = secrets.randbelow(9) + 1    # 1-9
    else:
        a = secrets.randbelow(20) + 1   # 1-20
        b = secrets.randbelow(20) + 1   # 1-20

    expr = f"{a} {op} {b}"
    result = OPERANDS[op](a, b)
    return expr, result


async def _check_ip_locked(ip: str) -> bool:
    """检查 IP 是否因连续输错验证码被锁定."""
    r = get_redis()
    return bool(await r.exists(CAPTCHA_LOCK_KEY.format(ip=ip)))


async def _check_and_incr_rate(ip: str) -> bool:
    """检查并递增 IP 获取频率，返回是否允许（True=允许，False=超限）."""
    r = get_redis()
    key = CAPTCHA_RATE_KEY.format(ip=ip)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 60)  # 1 分钟窗口
    return count <= settings.CAPTCHA_RATE_LIMIT_PER_MINUTE


async def _incr_fail_count(ip: str) -> None:
    """递增 IP 验证码错误计数，达到阈值则锁定."""
    r = get_redis()
    key = CAPTCHA_FAIL_KEY.format(ip=ip)
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, settings.CAPTCHA_LOCK_MINUTES * 60)
    if count >= settings.CAPTCHA_MAX_FAIL_COUNT:
        lock_key = CAPTCHA_LOCK_KEY.format(ip=ip)
        await r.set(lock_key, "1", ex=settings.CAPTCHA_LOCK_MINUTES * 60)
        logger.warning(f"IP {ip} 连续输错验证码 {count} 次，锁定 {settings.CAPTCHA_LOCK_MINUTES} 分钟")


async def _reset_fail_count(ip: str) -> None:
    """验证码校验成功后重置错误计数."""
    r = get_redis()
    await r.delete(CAPTCHA_FAIL_KEY.format(ip=ip))


async def generate_captcha(ip: str | None = None) -> dict:
    """生成新的图形验证码.

    Args:
        ip: 客户端 IP（用于限流/锁定，可选）

    Returns:
        {
            "captcha_id": "uuid",
            "image_base64": "data:image/png;base64,..."
        }

    Raises:
        ValueError: IP 被锁定或获取频率超限
    """
    # 安全策略：IP 锁定检查
    if ip:
        if await _check_ip_locked(ip):
            raise ValueError(f"IP {ip} 因连续输错验证码已被锁定 {settings.CAPTCHA_LOCK_MINUTES} 分钟")
        if not await _check_and_incr_rate(ip):
            raise ValueError(f"IP {ip} 获取验证码过于频繁，每分钟限 {settings.CAPTCHA_RATE_LIMIT_PER_MINUTE} 次")

    captcha_id = str(uuid.uuid4())
    expr, answer = _generate_expression()

    # 生成图片（captcha 库的 generate() 直接返回 BytesIO）
    img_io = _image_captcha.generate(expr)
    img_base64 = base64.b64encode(img_io.getvalue()).decode("utf-8")

    # 答案存入 Redis（5 分钟有效）
    r = get_redis()
    key = CAPTCHA_KEY.format(captcha_id=captcha_id)
    await r.set(key, str(answer), ex=CAPTCHA_TTL)

    logger.debug(f"验证码生成: id={captcha_id[:8]}... expr={expr} answer={answer} ip={ip}")

    return {
        "captcha_id": captcha_id,
        "image_base64": f"data:image/png;base64,{img_base64}",
    }


async def verify_captcha(captcha_id: str, captcha_result: str, ip: str | None = None) -> bool:
    """校验验证码.

    Args:
        captcha_id:     验证码 ID
        captcha_result: 用户输入的计算结果
        ip:             客户端 IP（用于错误计数/锁定，可选）

    Returns:
        True 如果验证通过
    """
    r = get_redis()
    key = CAPTCHA_KEY.format(captcha_id=captcha_id)
    expected = await r.get(key)

    if not expected:
        logger.warning(f"验证码校验失败: id={captcha_id[:8]}... 已过期或不存在")
        return False

    # 校验后立即删除（一次性使用，防重放）
    await r.delete(key)

    if captcha_result.strip() != expected:
        logger.warning(f"验证码校验失败: id={captcha_id[:8]}... 期望={expected} 实际={captcha_result}")
        if ip:
            await _incr_fail_count(ip)
        return False

    # 校验成功，重置错误计数
    if ip:
        await _reset_fail_count(ip)

    return True