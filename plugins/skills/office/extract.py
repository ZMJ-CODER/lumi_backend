"""办公技能（office/抽取与合规）：信息抽取 / 发票解析 / 敏感词合规审查."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.services.office_skill_utils import SENSITIVE_WORDS, office_llm


def _bad(msg: str) -> SkillResult:
    return SkillResult(success=False, error=msg, error_code="INVALID_ARGS", retryable=False)


class ExtractInfoSkill(Skill):
    name = "extract_info"
    description = "信息抽取：从文本中抽取指定字段（人名/时间/金额/合同号/邮箱/电话等），输出 JSON"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "源文本"},
            "fields": {"type": "string", "description": "要抽取的字段，逗号分隔"},
        },
        "required": ["text", "fields"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        text = str(params.get("text") or "").strip()
        fields = str(params.get("fields") or "").strip()
        if not text or not fields:
            return _bad("缺少 text / fields")
        out = await office_llm(
            context,
            "你是信息抽取助手。只输出 JSON（不要 Markdown 围栏），字段缺失时值为 null，不要编造。",
            f"需要抽取的字段：{fields}\n源文本：\n{text[:80000]}",
        )
        return SkillResult(success=True, output=out, metadata={"format": "json"})


class InvoiceParseSkill(Skill):
    name = "invoice_parse"
    description = "发票/报销单处理：从发票文字/图片描述中提取发票号码、开票日期、金额、税额、购买方、销售方、项目等字段"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "发票内容文字（OCR/描述/人工抄录）"},
        },
        "required": ["text"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        text = str(params.get("text") or "").strip()
        if not text:
            return _bad("缺少发票文字 text（可先用图片上传，再由模型转成文字）")
        out = await office_llm(
            context,
            "你是发票信息提取专家。输出 JSON：发票号码、发票代码、开票日期、购买方名称、"
            "销售方名称、项目/货物名称、金额（不含税）、税率、税额、价税合计。缺失为 null，不要编造。",
            f"发票文字：\n{text[:40000]}",
        )
        return SkillResult(success=True, output=out, metadata={"format": "json"})


class ComplianceCheckSkill(Skill):
    name = "compliance_check"
    description = "敏感词/合规审查：检查文本是否包含敏感词或违规内容（广告法、平台规范、涉密等），给出命中项与修改建议"
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "待审查文本"},
            "domain": {"type": "string", "description": "审查场景：通用/广告/新闻/客服/内部文档（默认通用）"},
        },
        "required": ["text"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        text = str(params.get("text") or "").strip()
        if not text:
            return _bad("缺少 text")
        domain = str(params.get("domain") or "通用").strip()
        hits = [w for w in SENSITIVE_WORDS if w in text]
        hit_line = f"命中的基础敏感词：{', '.join(hits)}" if hits else "基础敏感词未命中。"
        out = await office_llm(
            context,
            "你是内容合规审查专家。结合上下文判断是否违规（基础词命中但语义正常时可放行），"
            "输出：风险等级（低/中/高）、命中项与理由、修改建议。",
            f"审查场景：{domain}\n{hit_line}\n待审查文本：\n{text[:40000]}",
        )
        return SkillResult(
            success=True,
            output=out,
            metadata={"sensitive_hits": hits},
        )
