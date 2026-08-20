"""办公技能公共工具：LLM 调用封装 + 合规敏感词表.

放在 app/services 下保证插件文件（按独立模块加载）可正常导入。
"""

from app.agents.skills.base import SkillContext
from app.core.agent_security import UNTRUSTED_CONTENT_RULES
from app.core.llm import LLMClient
from app.services.response_format import OFFICE_RESPONSE_FORMAT_COMPACT
from app.services.usage import CATEGORY_SKILL


async def office_llm(
    context: SkillContext | None,
    system: str,
    user: str,
    *,
    max_tokens: int = 4000,
    temperature: float = 0.4,
    format_response: bool = True,
) -> str:
    """调用用户配置的办公模型生成文本（技能用途）.

    默认约束为桌面气泡友好的 Markdown，使单步骤任务不经最终汇总时也具备可读结构。
    需要严格原文/JSON 的 Skill 可显式关闭。
    """
    llm = LLMClient()
    if format_response:
        system = f"{system}\n\n{OFFICE_RESPONSE_FORMAT_COMPACT}"
    system = f"{system}\n\n{UNTRUSTED_CONTENT_RULES}"
    return await llm.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        scene=(context.scene if context else "office"),
        max_tokens=max_tokens,
        temperature=temperature,
        usage_user_id=context.user_id if context else None,
        usage_category=CATEGORY_SKILL,
        disable_reasoning_effort=True,
        api_key=context.llm_api_key if context else None,
    )


# 合规审查：基础敏感词表（命中即提示，交由 LLM 结合上下文判定）
SENSITIVE_WORDS = [
    "赌博", "博彩", "色情", "裸聊", "毒品", "海洛因", "冰毒", "枪支", "弹药",
    "诈骗", "洗钱", "传销", "非法集资", "刷单", "代开发票", "假证", "黑客攻击",
    "木马", "病毒制作", "破解", "外挂", "翻墙", "境内外勾结", "颠覆", "邪教",
    "传谣", "造谣", "泄露国家秘密", "间谍", "恐怖", "自杀", "自残",
]
