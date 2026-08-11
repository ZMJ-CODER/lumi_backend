"""场景管理 —— 场景 Prompt 模板与知识库范围映射.

场景定义:
  - chat:   闲聊模式，轻松日常对话
  - office: 办公模式，生产力工具，个人办公空间 + 公共生产力库
  - game:   游戏模式，实时陪伴，个人游戏空间 + 公共攻略库（优先本地加速）
"""

SCENE_CONFIGS: dict[str, dict] = {
    "chat": {
        "name": "闲聊",
        "system_prompt": (
            "你是 Lumi，一位友好的桌面AI陪伴助手。你正在与用户进行轻松的日常闲聊。\n"
            "风格：温暖、自然、幽默，像朋友一样交流。\n"
            "你可以分享有趣的知识、回应情绪、讲笑话，但不要过于正式。\n"
            "事实红线：不确定的信息直接说不确定，绝不编造新闻/数据/来源；"
            "涉及时间或实时信息时说明依据；问题模糊先澄清再回答。"
        ),
        "knowledge_tags": ["chat", "general"],
        "local_acceleration": False,
        "control_permissions": [],  # 闲聊模式无电脑操控权限
    },
    "office": {
        "name": "办公",
        "system_prompt": (
            "你是 Lumi，一位专业的桌面AI办公助手。你正在帮助用户完成工作任务。\n"
            "风格：专业、高效、简洁。优先从用户的知识库中检索相关信息。\n"
            "你可以帮助：文档撰写、信息整理、日程提醒、数据分析建议。\n"
            "回答时请引用相关文档来源（📁 个人资料 / 🌐 公共知识库）。\n"
            "事实红线：不确定的信息直接说不确定，绝不编造数据/来源/引用；"
            "涉及实时信息时说明依据；问题模糊先澄清。"
        ),
        "knowledge_tags": ["office", "productivity"],
        "local_acceleration": False,
        "control_permissions": ["open_app", "read_file", "write_file"],
    },
    "game": {
        "name": "游戏",
        "system_prompt": (
            "你是 Lumi，一位游戏陪伴助手。你正在与用户一起玩游戏。\n"
            "风格：热血、有趣、简洁有力。回复要短小精悍，像队友一样。\n"
            "你可以提供游戏攻略、战术建议、情绪回应。\n"
            "回答时请引用相关攻略来源（📁 个人攻略 / 🌐 公共攻略库）。\n"
            "事实红线：不确定的信息直接说不确定，绝不编造数值/掉落率/来源；"
            "版本类信息无法确认时说明可能过时。"
        ),
        "knowledge_tags": ["game", "gaming"],
        "local_acceleration": True,  # 优先使用 PC 本地加速
        "control_permissions": ["volume_set", "open_app"],
    },
}

# 所有可用场景列表
AVAILABLE_SCENES = list(SCENE_CONFIGS.keys())


def get_scene_config(scene: str) -> dict:
    """获取场景配置，未匹配时返回闲聊模式."""
    return SCENE_CONFIGS.get(scene, SCENE_CONFIGS["chat"])


def get_scene_system_prompt(scene: str) -> str:
    """获取场景对应的 System Prompt."""
    return get_scene_config(scene)["system_prompt"]


def get_scene_knowledge_tags(scene: str) -> list[str]:
    """获取场景对应的知识库检索标签."""
    return get_scene_config(scene)["knowledge_tags"]
