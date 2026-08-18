"""模型目录 —— 办公模式用户可选的模型清单与元数据.

字段说明：
  - context_window:        上下文上限（token）
  - multimodal:            是否支持图片输入
  - supports_reasoning_effort: 是否支持推理强度（low/medium/high）
  - price_input/output:    每百万 token 价格（USD，占位值，按实际单价调整）

新增模型只需在此追加；前端 GET /user/models 动态获取，不写死。
"""

from app.core.config import settings

def _configured_model(
    *, model_id: str, provider: str, description: str, supports_reasoning_effort: bool = True
) -> dict:
    """Build one selector item from a model actually configured on this server."""
    return {
        "id": model_id,
        "name": model_id,
        "provider": provider,
        "context_window": 131072,
        "multimodal": False,
        "supports_reasoning_effort": supports_reasoning_effort,
        "price_input_per_million": 0.0,
        "price_output_per_million": 0.0,
        "description": description,
    }


# Kept as metadata fallbacks for deployments which deliberately expose aliases.
# get_model_catalog() always adds the concrete .env models first.
MODEL_CATALOG: list[dict] = [
    {
        "id": "deepseek-v4-flash",
        "name": "DeepSeek V4 Flash",
        "provider": "deepseek",
        "context_window": 131072,
        "multimodal": False,
        "supports_reasoning_effort": True,
        "price_input_per_million": 0.5,
        "price_output_per_million": 2.0,
        "description": "轻量快速，适合日常对话与办公任务",
    },
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "provider": "deepseek",
        "context_window": 131072,
        "multimodal": False,
        "supports_reasoning_effort": True,
        "price_input_per_million": 4.0,
        "price_output_per_million": 16.0,
        "description": "深度推理，适合复杂任务与代码",
    },
    {
        "id": "qwen3-8-max",
        "name": "Qwen3 8B Max",
        "provider": "qwen",
        "context_window": 131072,
        "multimodal": False,
        "supports_reasoning_effort": True,
        "price_input_per_million": 2.0,
        "price_output_per_million": 8.0,
        "description": "千问大杯模型，综合能力强",
    },
    {
        "id": "qwen3-7-plus",
        "name": "Qwen3 7B Plus",
        "provider": "qwen",
        "context_window": 131072,
        "multimodal": False,
        "supports_reasoning_effort": False,
        "price_input_per_million": 1.0,
        "price_output_per_million": 4.0,
        "description": "千问标准模型，性价比之选",
    },
]


# provider → 默认 OpenAI 兼容 base_url（BYOK 弹窗里 provider 下拉的依据）
PROVIDER_BASE_URLS: dict[str, str] = {
    "deepseek": settings.DEEPSEEK_BASE_URL,
    "qwen": settings.QWEN_BASE_URL,
    "openai": "https://api.openai.com/v1",
}


def get_model_catalog() -> list[dict]:
    """Return models callable by this deployment, without disclosing API keys."""
    configured: list[dict] = []
    if settings.DEEPSEEK_API_KEY and settings.DEEPSEEK_MODEL:
        configured.append(
            _configured_model(
                model_id=settings.DEEPSEEK_MODEL,
                provider="deepseek",
                description="服务端 .env 已配置的 DeepSeek 模型",
            )
        )
    if settings.QWEN_API_KEY and settings.QWEN_MODEL:
        configured.append(
            _configured_model(
                model_id=settings.QWEN_MODEL,
                provider="qwen",
                description="服务端 .env 已配置的通义千问模型",
            )
        )

    # Do not show aliases that cannot be executed with the current provider
    # settings. A BYOK user can still enter an arbitrary model in the UI.
    return configured


def find_model(model_id: str) -> dict | None:
    for m in get_model_catalog():
        if m["id"] == model_id:
            return m
    return None
