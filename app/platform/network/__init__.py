"""``app/platform.network``：受控网络客户端。

``network`` 是本仓库唯一允许直接建连的地方（与 ``scripts/check_unsafe_calls.py``
的"socket 直连必须走受控 HTTP 客户端"是同一条边界）。业务代码一律用 HTTP 客户端封装的入口。
"""
