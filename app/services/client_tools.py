"""客户端工具请求通道 —— 后端下发指令给用户端（Electron）执行并回收结果.

流程：
  1. LLM 调用 client 环境技能 → 创建待执行请求（Redis，含 skill/参数/高危标记）
  2. 用户端轮询 GET /tools/requests 获取请求 → 高危弹窗确认 → 本地执行
  3. 用户端 POST /tools/requests/{id}/result 回传结果
  4. 技能 execute 轮询 Redis 等待结果（超时取消）

安全性：
  - 请求与结果都按 user_id 隔离（JWT 鉴权 + Redis key 归属校验）
  - 高危操作由用户端弹窗二次确认
  - 用户端只执行白名单内的技能，且路径做校验（见 Electron 主进程实现）
"""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone

from app.core.config import settings
from app.core.redis import get_redis

REQUEST_TTL_SECONDS = 300  # 待执行请求保留 5 分钟


def _key(user_id: str, request_id: str) -> str:
    return f"client_tool:{user_id}:{request_id}"


def _pending_key(user_id: str) -> str:
    return f"client_tool_pending:{user_id}"


async def create_client_tool_request(
    user_id: str,
    skill_name: str,
    params: dict,
    requires_confirmation: bool = False,
) -> dict | None:
    """创建待用户端执行的请求；返回请求数据（含 request_id）."""
    try:
        uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        return None  # 游客/无效用户：本地文件技能需登录
    request_id = str(uuid.uuid4())
    r = get_redis()
    payload = {
        "request_id": request_id,
        "skill": skill_name,
        "params": json.dumps(params, ensure_ascii=False),
        "requires_confirmation": str(bool(requires_confirmation)).lower(),
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    key = _key(user_id, request_id)
    await r.hset(key, mapping=payload)
    await r.expire(key, REQUEST_TTL_SECONDS)
    await r.rpush(_pending_key(user_id), request_id)
    await r.expire(_pending_key(user_id), REQUEST_TTL_SECONDS)
    return payload


async def list_pending_requests(user_id: str) -> list[dict]:
    """列出该用户所有待执行请求（用户端轮询用）."""
    r = get_redis()
    ids = await r.lrange(_pending_key(user_id), 0, -1)
    items: list[dict] = []
    for rid in ids:
        data = await r.hgetall(_key(user_id, rid))
        if not data or data.get("status") != "pending":
            continue
        try:
            data["params"] = json.loads(data.get("params") or "{}")
        except (ValueError, TypeError):
            data["params"] = {}
        data["requires_confirmation"] = data.get("requires_confirmation") == "true"
        items.append(data)
    return items


async def complete_request(
    user_id: str,
    request_id: str,
    *,
    success: bool,
    output: str = "",
    error: str | None = None,
    metadata: dict | None = None,
) -> bool:
    """用户端回传结果；请求不存在/已处理返回 False."""
    r = get_redis()
    key = _key(user_id, request_id)
    data = await r.hgetall(key)
    if not data or data.get("status") != "pending":
        return False
    result = {
        "success": success,
        "output": output or "",
        "error": error,
        "metadata": metadata or {},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    await r.hset(key, mapping={"status": "completed", "result": json.dumps(result, ensure_ascii=False)})
    await r.lrem(_pending_key(user_id), 1, request_id)
    return True


async def cancel_request(user_id: str, request_id: str) -> bool:
    """用户取消（弹窗点了取消）."""
    return await complete_request(
        user_id, request_id, success=False, error="用户取消了操作", metadata={"cancelled": True}
    )


async def await_result(
    user_id: str,
    request_id: str,
    timeout: float | None = None,
) -> dict | None:
    """轮询等待用户端结果；超时/取消/过期返回 None."""
    r = get_redis()
    key = _key(user_id, request_id)
    timeout = timeout or settings.AGENT_CLIENT_TOOL_TIMEOUT_SECONDS
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = await r.hgetall(key)
        if not data:
            # 请求已过期（TTL）或被清理
            return None
        status = data.get("status")
        if status == "completed":
            try:
                return json.loads(data.get("result") or "{}")
            except (ValueError, TypeError):
                return None
        if status and status not in ("pending",):
            return None
        await asyncio.sleep(1)
    await r.hset(key, mapping={"status": "timeout"})
    return None
