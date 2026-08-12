"""模型目录 —— 办公模式用户可选的模型清单与元数据.

字段说明：
  - context_window:        上下文上限（token）
  - multimodal:            是否支持图片输入
  - supports_reasoning_effort: 是否支持推理强度（low/medium/high）
  - price_input/output:    每百万 token 价格（USD，占位值，按实际单价调整）

新增模型只需在此追加；前端 GET /user/models 动态获取，不写死。
"""

from app.core.config import settings

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
    """返回模型目录（拷贝，避免调用方误改）."""
    return [dict(m) for m in MODEL_CATALOG]


def find_model(model_id: str) -> dict | None:
    for m in MODEL_CATALOG:
        if m["id"] == model_id:
            return m
    return None
