"""Lumi Backend Locust 场景。

默认只压健康检查，避免未授权地触发 LLM、Celery 或写入业务数据。
通过 ``LUMI_LOAD_SCENARIO`` 选择 protected / local_message 场景，详见
``docs/LOCUST_LOAD_TESTING.md``。
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from gevent.lock import Semaphore

from locust import HttpUser, between, constant, task


SCENARIO = os.getenv("LUMI_LOAD_SCENARIO", "health").strip().lower()
ACCESS_TOKEN = os.getenv("LUMI_LOAD_ACCESS_TOKEN", "").strip()
TOKENS_FILE = os.getenv("LUMI_LOAD_TOKENS_FILE", "").strip()
CONVERSATION_ID = os.getenv("LUMI_LOAD_CONVERSATION_ID", "").strip()


def _load_tokens() -> list[str]:
    """Read local seed output; invalid files fail before generating load."""
    if not TOKENS_FILE:
        return [ACCESS_TOKEN] if ACCESS_TOKEN else []
    try:
        payload = json.loads(Path(TOKENS_FILE).read_text(encoding="utf-8"))
        users = payload.get("users") if isinstance(payload, dict) else None
        tokens = [str(item.get("access_token") or "").strip() for item in users or [] if isinstance(item, dict)]
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"无法读取 LUMI_LOAD_TOKENS_FILE: {exc}") from exc
    tokens = [token for token in tokens if token]
    if not tokens:
        raise RuntimeError("LUMI_LOAD_TOKENS_FILE 中没有 access_token")
    return tokens


PROTECTED_TOKENS = _load_tokens()
if SCENARIO in {"protected", "max_rps"} and not PROTECTED_TOKENS:
    raise RuntimeError(
        "protected/max_rps 场景需要设置 LUMI_LOAD_ACCESS_TOKEN 或 LUMI_LOAD_TOKENS_FILE；"
        "拒绝静默降级为 health 压测。"
    )
_token_index = 0
_token_lock = Semaphore()


def _next_token() -> str:
    """Assign each Locust user a stable identity, cycling only above seed size."""
    global _token_index
    with _token_lock:
        token = PROTECTED_TOKENS[_token_index % len(PROTECTED_TOKENS)]
        _token_index += 1
        return token


class HealthUser(HttpUser):
    """无副作用探活压测，适合首轮验证 API/Redis/PG 并发。"""

    weight = 1 if SCENARIO == "health" else 0
    wait_time = between(0.1, 0.5)

    @task
    def health(self) -> None:
        self.client.get("/api/v1/health", name="GET /api/v1/health")


class ProtectedReadUser(HttpUser):
    """带现成 JWT 的只读接口压测，不执行写操作。"""

    weight = 1 if SCENARIO == "protected" and PROTECTED_TOKENS else 0
    wait_time = between(0.2, 0.8)

    def on_start(self) -> None:
        self.headers = {"Authorization": f"Bearer {_next_token()}"}

    @task(3)
    def current_user(self) -> None:
        self._get("/api/v1/user/me", "GET /api/v1/user/me")

    @task(2)
    def list_conversations(self) -> None:
        self._get(
            "/api/v1/conversations?scene=chat&limit=20&offset=0",
            "GET /api/v1/conversations",
        )

    @task(1)
    def list_memories(self) -> None:
        self._get("/api/v1/memory?limit=20", "GET /api/v1/memory")

    def _get(self, path: str, name: str) -> None:
        with self.client.get(path, headers=self.headers, name=name, catch_response=True) as response:
            if response.status_code != 200:
                # Locust represents connection resets, empty replies and client-side
                # timeouts as HTTP 0. Preserve the underlying exception; otherwise a
                # transport failure is indistinguishable from an application 500.
                error = getattr(response, "error", None)
                detail = str(error)[:240] if error else response.text[:160]
                response.failure(f"HTTP {response.status_code}: {detail}")


class MaxRpsReadUser(ProtectedReadUser):
    """无等待读压测：用于寻找服务端吞吐拐点，不代表真实用户行为。"""

    weight = 1 if SCENARIO == "max_rps" and PROTECTED_TOKENS else 0
    wait_time = constant(0)


class LocalModeUser(HttpUser):
    """消息链路冒烟：local_mode=true，不调用 LLM，仅验证会话锁/请求处理。"""

    weight = 1 if SCENARIO == "local_message" and ACCESS_TOKEN and CONVERSATION_ID else 0
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.headers = {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }

    @task
    def send_local_message(self) -> None:
        payload = {
            "content": "Locust local-mode concurrency probe",
            "scene": "chat",
            "local_mode": True,
            "message_id": str(uuid.uuid4()),
        }
        with self.client.post(
            f"/api/v1/conversations/{CONVERSATION_ID}/messages",
            data=json.dumps(payload),
            headers=self.headers,
            name="POST /api/v1/conversations/{id}/messages (local_mode)",
            catch_response=True,
        ) as response:
            if response.status_code not in (200, 201):
                error = getattr(response, "error", None)
                detail = str(error)[:240] if error else response.text[:160]
                response.failure(f"HTTP {response.status_code}: {detail}")
