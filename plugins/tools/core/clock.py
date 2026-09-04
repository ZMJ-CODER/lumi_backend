"""系统时钟基础工具的服务端实现。"""

from datetime import datetime, timedelta, timezone

from app.agents.skills.base import SkillContext, ToolOutput


async def execute_datetime(params: dict, context: SkillContext | None = None) -> ToolOutput:
    """返回东八区当前时间；仅服务端注入时钟，不读取客户端敏感信息。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    fmt = str(params.get("format") or "datetime")
    if fmt == "date":
        text = now.strftime("%Y年%m月%d日 %A")
    elif fmt == "time":
        text = now.strftime("%H:%M:%S")
    else:
        text = now.strftime("%Y年%m月%d日 %A %H:%M:%S")
    return ToolOutput(success=True, output=text, metadata={"datetime": now.isoformat()})
