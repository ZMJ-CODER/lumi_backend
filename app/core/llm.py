"""LLM 客户端封装 —— 多 Provider 统一入口.

支持的 Provider:
  - qwen     (千问 / DashScope)
  - deepseek (DeepSeek)
"""

from httpx import AsyncClient

from app.core.config import settings

PROVIDER_CONFIGS = {
    "qwen": {
        "api_key": settings.QWEN_API_KEY,
        "base_url": settings.QWEN_BASE_URL,
        "model": settings.QWEN_MODEL,
    },
    "deepseek": {
        "api_key": settings.DEEPSEEK_API_KEY,
        "base_url": settings.DEEPSEEK_BASE_URL,
        "model": settings.DEEPSEEK_MODEL,
    },
}


class LLMClient:
    """异步 LLM 客户端，支持多 Provider 切换."""

    def __init__(self, provider: str | None = None) -> None:
        provider = provider or settings.LLM_PROVIDER
        cfg = PROVIDER_CONFIGS.get(provider)
        if not cfg:
            raise ValueError(f"不支持的 LLM Provider: {provider}，可选: {list(PROVIDER_CONFIGS)}")
        self.provider = provider
        self.api_key = cfg["api_key"]
        self.base_url = cfg["base_url"]
        self.model = cfg["model"]
        self._client: AsyncClient | None = None

    async def start(self) -> None:
        self._client = AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=120.0,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def chat(self, messages: list[dict], **kwargs) -> str:
        """发送对话请求，返回模型响应文本."""
        if not self._client:
            raise RuntimeError("LLMClient not started")
        payload = {
            "model": kwargs.pop("model", self.model),
            "messages": messages,
            **kwargs,
        }
        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本嵌入向量."""
        if not self._client:
            raise RuntimeError("LLMClient not started")
        # 嵌入模型走千问的 text-embedding 或 DeepSeek 暂不支持
        # 这里先用千问 embedding 端点
        payload = {
            "model": settings.EMBEDDING_MODEL,
            "input": texts,
        }
        resp = await self._client.post("/embeddings", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return [item["embedding"] for item in data["data"]]


# 全局单例（默认 Provider）
llm_client = LLMClient()
