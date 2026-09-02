"""外部 HTTP 服务的网络配置解析。"""

from __future__ import annotations

import os
from urllib.request import getproxies


def resolve_http_proxy(explicit: str | None = None) -> str | None:
    """解析显式配置、环境变量与 Windows 系统代理。

    ``urllib`` 在 Windows 上会读取 Internet Settings；这弥补 httpx/OpenAI
    只读取 HTTP(S)_PROXY 环境变量的差异。显式配置优先，空值表示继续探测。
    """
    value = str(explicit or "").strip()
    if value:
        return value
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    proxies = getproxies()
    return next(
        (str(proxies.get(key) or "").strip() for key in ("https", "http", "all") if proxies.get(key)),
        None,
    )
