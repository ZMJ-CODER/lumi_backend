"""技能插件（network/网络与web工具）：curl —— 发起 HTTP 请求（服务端执行）."""

from app.agents.skills.base import Skill, SkillContext, SkillResult


class CurlSkill(Skill):
    name = "curl"
    description = (
        "向指定 URL 发起 HTTP 请求（GET/POST/PUT/DELETE 等），返回状态码与响应内容（截断）。"
        "当需要调用 Web API、检查接口连通性、获取网页内容时使用。"
    )
    category = "network"
    environment = "server"
    requires_confirmation = False
    scenes = ["chat", "office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "请求 URL"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"], "description": "HTTP 方法（默认 GET）"},
            "headers": {"type": "object", "description": "可选：请求头（键值对）"},
            "body": {"type": "object", "description": "可选：JSON 请求体"},
            "timeout": {"type": "integer", "description": "超时秒数（默认 20）", "minimum": 1, "maximum": 60},
        },
        "required": ["url"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        url = str(params.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            return SkillResult(
                success=False,
                error="url 必须以 http:// 或 https:// 开头",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        method = str(params.get("method") or "GET").upper()
        timeout = min(int(params.get("timeout") or 20), 60)
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=timeout,
                headers={k: str(v) for k, v in (params.get("headers") or {}).items()},
                follow_redirects=True,
            ) as client:
                resp = await client.request(method, url, json=params.get("body") or None)
            text = resp.text or ""
            if len(text) > 8000:
                text = text[:8000] + "\n…[输出已截断]"
            return SkillResult(
                success=True,
                output=f"HTTP {resp.status_code} {resp.reason_phrase}\n{text}",
                metadata={"status_code": resp.status_code, "content_type": resp.headers.get("content-type", "")},
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"请求失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
