"""LLM 客户端封装 —— 配置动态化（Redis 优先，.env 兜底）.

配置来源优先级:
  1. 调用时显式传入的 model
  2. Redis 动态配置（场景级 → 全局默认，进程内缓存 5 秒）
  3. .env 兜底配置

每次调用按当前配置创建短连接，避免配置更新后旧连接仍携带旧 base_url/api_key。
"""

from httpx import AsyncClient

from app.core.config import settings
from app.core.llm_config import get_llm_config


class LLMClient:
    """异步 LLM 客户端，配置每次调用时动态读取."""

    def __init__(self, provider: str | None = None) -> None:
        # provider 仅用于 .env 兜底时的选择（兼容旧代码），实际配置以运行时为准
        self.provider = provider or settings.LLM_PROVIDER
        self._client: AsyncClient | None = None

    async def start(self) -> None:
        """兼容保留：配置已改为每次调用动态获取，无需预建连接."""

    async def close(self) -> None:
        """兼容保留."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[dict],
        *,
        scene: str | None = None,
        model: str | None = None,
        **kwargs,
    ) -> str:
        """发送对话请求，返回模型响应文本."""
        cfg = await get_llm_config(scene, self.provider)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = cfg.get("api_key") or ""
        model_name = model or cfg.get("model") or settings.DEEPSEEK_MODEL
        timeout = float(cfg.get("timeout") or 120.0)

        payload = {"model": model_name, "messages": messages, **kwargs}
        async with AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        ) as client:
            resp = await client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def embed(
        self,
        texts: list[str],
        *,
        scene: str | None = None,
        model: str | None = None,
    ) -> list[list[float]]:
        """生成文本嵌入向量（模型名默认取 settings.EMBEDDING_MODEL）."""
        cfg = await get_llm_config(scene, self.provider)
        base_url = (cfg.get("base_url") or "").rstrip("/")
        api_key = cfg.get("api_key") or ""
        model_name = model or settings.EMBEDDING_MODEL
        timeout = float(cfg.get("timeout") or 120.0)

        payload = {"model": model_name, "input": texts}
        async with AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        ) as client:
            resp = await client.post("/embeddings", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return [item["embedding"] for item in data["data"]]


# 全局单例（默认 Provider）
llm_client = LLMClient()
