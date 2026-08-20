"""办公技能（office/写作类）：邮件撰写 / 公文撰写 / 多风格改写 / 长文摘要 / 会议纪要."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.services.office_skill_utils import office_llm


def _ok(text: str) -> SkillResult:
    return SkillResult(success=True, output=text)


def _bad(msg: str) -> SkillResult:
    return SkillResult(success=False, error=msg, error_code="INVALID_ARGS", retryable=False)


class ComposeEmailSkill(Skill):
    name = "compose_email"
    description = "撰写商务/工作邮件：根据收件人、目的、语气与要点生成邮件正文（含标题）"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "完整指令（提供时忽略下列结构化字段）"},
            "recipient": {"type": "string", "description": "收件人（称呼/角色/姓名）"},
            "purpose": {"type": "string", "description": "邮件目的，如请假、汇报、催办、邀请"},
            "key_points": {"type": "string", "description": "要点，可多行列出"},
            "tone": {"type": "string", "description": "语气：正式/友好/简洁/严肃（默认正式）"},
        },
        "required": ["recipient", "purpose", "key_points"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        instruction = str(params.get("instruction") or "").strip()
        recipient = str(params.get("recipient") or ("（按指令）" if instruction else "")).strip()
        purpose = str(params.get("purpose") or instruction or "").strip()
        points = str(params.get("key_points") or instruction or "").strip()
        if not instruction and (not recipient or not purpose or not points):
            return _bad("缺少 recipient / purpose / key_points")
        tone = str(params.get("tone") or "正式").strip()
        text = await office_llm(
            context,
            "你是一名资深职场商务写作助手。只输出邮件内容本身：先写标题（用『』包裹），再写正文。",
            f"收件人：{recipient}\n目的：{purpose}\n语气：{tone}\n要点：\n{points}",
            format_response=False,
        )
        return _ok(text)


class ComposeOfficialDocSkill(Skill):
    name = "compose_official_doc"
    description = "撰写公文/正式文书（通知、请示、报告、函、会议纪要、制度、方案等），按公文格式输出"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "完整指令（提供时忽略下列结构化字段）"},
            "doc_type": {"type": "string", "description": "文种：通知/请示/报告/函/纪要/制度/方案等"},
            "title": {"type": "string", "description": "标题（可为空，自动拟）"},
            "requirements": {"type": "string", "description": "内容要求、背景与要点"},
        },
        "required": ["doc_type", "requirements"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        instruction = str(params.get("instruction") or "").strip()
        doc_type = str(params.get("doc_type") or "").strip() or "通知"
        req = str(params.get("requirements") or instruction or "").strip()
        if not instruction and not req:
            return _bad("缺少 doc_type / requirements")
        title = str(params.get("title") or "").strip()
        text = await office_llm(
            context,
            "你是一名党政机关/企事业单位公文写作专家。严格按《党政机关公文格式》GB/T 9704 撰写："
            "标题居中、主送机关、正文（分条列项）、落款（单位+日期留空）。只输出公文正文。",
            f"文种：{doc_type}\n标题：{title or '（自动拟）'}\n内容要求：\n{req}",
            max_tokens=6000,
            format_response=False,
        )
        return _ok(text)


class RewriteTextSkill(Skill):
    name = "rewrite_text"
    description = "对一段文字做多风格改写：正式/口语/简洁/生动/学术/营销/儿童友好等，保留原意"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "完整指令（提供时忽略下列结构化字段）"},
            "text": {"type": "string", "description": "待改写的原文"},
            "style": {"type": "string", "description": "目标风格（默认正式）"},
            "length": {"type": "string", "description": "长度：精简/适中/详细（默认适中）"},
        },
        "required": ["text", "style"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        instruction = str(params.get("instruction") or "").strip()
        text = str(params.get("text") or instruction or "").strip()
        style = str(params.get("style") or "正式").strip()
        if not instruction and not text:
            return _bad("缺少 text / style")
        length = str(params.get("length") or "适中").strip()
        out = await office_llm(
            context,
            "你是一名文字改写专家。忠实保留原意，只改变表达风格，输出改写后的文字本身。",
            f"目标风格：{style}\n长度：{length}\n原文：\n{text}",
            format_response=False,
        )
        return _ok(out)


class SummarizeTextSkill(Skill):
    name = "summarize_text"
    description = "长文摘要：把长文本压缩为要点列表/一段话/结构化摘要，保留关键信息"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "完整指令（提供时忽略下列结构化字段）"},
            "text": {"type": "string", "description": "待摘要的正文"},
            "format": {"type": "string", "description": "输出格式：要点/一段话/结构化（默认要点）"},
            "max_points": {"type": "integer", "description": "要点条数上限（默认 8）"},
        },
        "required": ["text"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        instruction = str(params.get("instruction") or "").strip()
        text = str(params.get("text") or instruction or "").strip()
        if not instruction and not text:
            return _bad("缺少 text")
        fmt = str(params.get("format") or "要点").strip()
        max_points = int(params.get("max_points") or 8)
        out = await office_llm(
            context,
            "你是一名信息摘要助手，忠实提取关键信息，不编造内容。",
            f"输出格式：{fmt}\n要点条数上限：{max_points}\n正文：\n{text[:80000]}",
            max_tokens=6000,
        )
        return _ok(out)


class MeetingMinutesSkill(Skill):
    name = "meeting_minutes"
    description = "会议纪要整理：把会议记录/语音转写文本整理为结构化的议题、决议与待办事项"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "完整指令（提供时忽略下列结构化字段）"},
            "raw_text": {"type": "string", "description": "会议原始记录/转写文本"},
            "participants": {"type": "string", "description": "参会人（可选）"},
        },
        "required": ["raw_text"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        instruction = str(params.get("instruction") or "").strip()
        raw = str(params.get("raw_text") or instruction or "").strip()
        if not instruction and not raw:
            return _bad("缺少 raw_text")
        participants = str(params.get("participants") or "").strip()
        out = await office_llm(
            context,
            "你是一名会议纪要整理专家。输出：会议主题、时间（如原文有）、参会人、"
            "议题与讨论摘要、决议事项、待办（含负责人与截止时间，如原文有）。不要编造。",
            f"参会人：{participants or '（原文提取）'}\n原始记录：\n{raw[:100000]}",
            max_tokens=6000,
        )
        return _ok(out)
