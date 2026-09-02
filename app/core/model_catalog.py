"""模型目录 —— 办公模式用户可选的模型清单与元数据.

字段说明：
  - context_window:        上下文上限（token）
  - multimodal:            是否支持图片输入
  - supports_reasoning_effort: 是否支持推理强度（low/medium/high）
  - price_input/output:    每百万 token 价格（USD，占位值，按实际单价调整）

新增模型只需在此追加；前端 GET /user/models 动态获取，不写死。
"""

from app.core.config import settings


# 办公模式固定内置三种服务端模型。用户仍可通过 BYOK 选择任意兼容模型。
# 不按 .env 的 MODEL 字段动态构建，避免默认模型为空时前端列表消失。
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
        "id": "qwen-turbo",
        "name": "通义千问 Turbo",
        "provider": "qwen",
        "context_window": 131072,
        "multimodal": False,
        "supports_reasoning_effort": False,
        "price_input_per_million": 0.0,
        "price_output_per_million": 0.0,
        "description": "快速稳定，适合常规办公和轻量任务",
    },
]


# provider → 默认 OpenAI Chat Completions 兼容 base_url。
#
# 清单仅代表接口形状兼容 ``/chat/completions``，并不意味着每个模型支持
# 函数调用、JSON mode 或图片输入；自定义服务使用 ``custom``。
PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": settings.DEEPSEEK_BASE_URL,
    "qwen": settings.QWEN_BASE_URL,
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "baichuan": "https://api.baichuan-ai.com/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "together": "https://api.together.xyz/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1",
    "perplexity": "https://api.perplexity.ai",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "nvidia": "https://integrate.api.nvidia.com/v1",
}


def normalize_byok_base_url(value: str, *, allow_private: bool = False) -> str:
    """校验并规范化 BYOK OpenAI-compatible 地址，避免自定义端点造成 SSRF。"""
    import ipaddress
    from urllib.parse import urlparse

    raw = (value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("兼容 API 地址不能为空")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("兼容 API 地址必须是完整的 http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("兼容 API 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("兼容 API 地址不能包含查询参数或片段")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("兼容 API 地址缺少主机名")
    if not allow_private:
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("不允许使用 localhost；如为受信任自托管服务，请由管理员开启私网 BYOK")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
            raise ValueError("不允许使用内网或回环地址；如为受信任自托管服务，请由管理员开启私网 BYOK")
    return normalize_provider_base_url(raw)


def normalize_provider_base_url(value: str) -> str:
    """规范化已知官方端点，保留其他兼容网关的原始路径。

    DeepSeek V4 官方 SDK 的 ``base_url`` 是 ``https://api.deepseek.com``，并
    不带 ``/v1``。历史前端曾把所有兼容接口套用 ``/v1``，会导致官方端点
    拼出错误请求路径；仅对该精确官方主机做向后兼容修正，第三方网关不受影响。
    """
    from urllib.parse import urlparse, urlunparse

    raw = (value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.hostname and parsed.hostname.casefold() == "api.deepseek.com" and parsed.path.rstrip("/") == "/v1":
        return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return raw


def get_model_catalog() -> list[dict]:
    """返回固定内置模型目录；不暴露 API key。"""
    return [dict(item) for item in MODEL_CATALOG]


def find_model(model_id: str) -> dict | None:
    for m in get_model_catalog():
        if m["id"] == model_id:
            return m
    return None


def normalize_model_id(model_id: str | None) -> str:
    """将历史 UI 显示名规范为服务商实际接收的模型 ID。

    正常前端始终保存 ``id``，但早期版本可能将 ``name`` 写进用户配置。
    只转换内置目录的精确显示名，其他 BYOK 模型名保持原样。
    """
    value = str(model_id or "").strip()
    if not value:
        return ""
    for item in MODEL_CATALOG:
        if value.casefold() in {str(item["id"]).casefold(), str(item["name"]).casefold()}:
            return str(item["id"])
    return value
