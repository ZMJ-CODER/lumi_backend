"""技能插件（desktop/GUI与桌面控制）：open_app —— 启动用户电脑上的软件."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


# 常见软件的网页版入口（本地未安装时引导用户选择网页版或下载安装）
WEB_APP_URLS: dict[str, str] = {
    "微信": "https://wx.qq.com/",
    "wechat": "https://wx.qq.com/",
    "weixin": "https://wx.qq.com/",
    "企业微信": "https://work.weixin.qq.com/",
    "钉钉": "https://www.dingtalk.com/",
    "dingtalk": "https://www.dingtalk.com/",
    "飞书": "https://www.feishu.cn/",
    "qq": "https://im.qq.com/",
    "腾讯会议": "https://meeting.tencent.com/",
    "网易云音乐": "https://music.163.com/",
    "哔哩哔哩": "https://www.bilibili.com/",
    "b站": "https://www.bilibili.com/",
    "抖音": "https://www.douyin.com/",
    "小红书": "https://www.xiaohongshu.com/",
    "微博": "https://weibo.com/",
    "淘宝": "https://www.taobao.com/",
    "京东": "https://www.jd.com/",
    "拼多多": "https://www.pinduoduo.com/",
    "支付宝": "https://www.alipay.com/",
    "steam": "https://store.steampowered.com/",
    "github": "https://github.com/",
    "gitee": "https://gitee.com/",
    "wps": "https://www.kdocs.cn/",
    "金山文档": "https://www.kdocs.cn/",
    "百度网盘": "https://pan.baidu.com/",
    "网盘": "https://pan.baidu.com/",
    "outlook": "https://outlook.live.com/",
    "邮箱": "https://mail.qq.com/",
    "gmail": "https://mail.google.com/",
}


def _notify(context: SkillContext | None, text: str) -> None:
    if context and context.on_notify:
        context.on_notify(text)


class OpenAppSkill(Skill):
    name = "open_app"
    description = (
        "启动用户电脑上已安装的软件/应用（按名称自动定位，如 微信 / 浏览器 / 记事本 / Chrome / 微信.exe）。"
        "当用户要求打开某个软件时使用；支持中文常用名与可执行文件名。"
    )
    category = "desktop"
    environment = "client"
    scenes = ["chat", "office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "要打开的软件名称（中文名或可执行文件名）"},
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：启动参数（如打开指定文件/网址）",
            },
        },
        "required": ["name"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        name = str(params.get("name") or "").strip()
        if not name:
            return SkillResult(
                success=False, error="缺少软件名称 name", error_code="INVALID_ARGS", retryable=False
            )
        _notify(context, f"（正在启动软件：{name}）")
        result = await run_client_skill_request(
            context.user_id if context else "",
            self.name,
            {"name": name, "args": list(params.get("args") or [])},
            False,
        )
        # 本地未安装 → 提供网页版入口，让 LLM 询问用户"打开网页版 or 下载安装"
        if not result.success:
            web_url = (
                WEB_APP_URLS.get(name.lower())
                or WEB_APP_URLS.get(name)
                or ""
            )
            if web_url:
                return SkillResult(
                    success=False,
                    error=(
                        f"{result.error} 该软件提供网页版：{web_url}。"
                        "请先询问用户：需要打开网页版，还是帮他下载安装？"
                        "用户确认后，用 open_url 打开网页版，或提示下载链接。"
                    ),
                    error_code=result.error_code,
                    retryable=False,
                    metadata={
                        **(result.metadata or {}),
                        "web_url": web_url,
                    },
                )
        return result
