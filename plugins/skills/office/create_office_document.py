"""Create new Word, PowerPoint and Excel artifacts from a constrained spec."""

from pathlib import Path

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.services.document_renderer import SUPPORTED_FORMATS, render_document
from app.services.office_docs import generic_outputs_dir


class CreateOfficeDocumentSkill(Skill):
    """Trusted deterministic rendering of a model-produced content specification."""

    name = "create_office_document"
    description = (
        "根据结构化内容生成一个真实的 Word（docx）、PowerPoint（pptx）或 Excel（xlsx）文件。"
        "只接受标题、段落、要点和表格等内容规格；不执行模型代码，不接受服务端路径。"
        "产物仅暂存供用户预览和主动下载，不会自动写入用户电脑。"
    )
    category = "office"
    environment = "server"
    scenes = ["office"]
    write_op = True
    idempotent = False
    cost_estimate = 0.4
    success_rate = 0.98
    requires = ["authenticated_user"]
    produces = ["reviewable_file_artifacts"]
    deterministic = True
    fallback_group = "office_document_creation"
    domain = "document"
    intent_tags = ["生成", "创建", "制作", "ppt", "pptx", "word", "docx", "excel", "xlsx", "演示文稿"]
    resource_templates = ["office-output:{conversation_id}"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "format": {"type": "string", "enum": ["docx", "pptx", "xlsx"], "description": "目标文档格式"},
            "filename": {"type": "string", "description": "仅文件名，例如 项目方案.pptx"},
            "title": {"type": "string", "description": "文档标题"},
            "style": {"type": "string", "enum": ["business", "minimal", "academic", "modern"], "description": "内置视觉风格"},
            "sections": {"type": "array", "description": "DOCX 的章节内容"},
            "slides": {"type": "array", "description": "PPTX 的页面内容"},
            "sheets": {"type": "array", "description": "XLSX 的工作表内容"},
            "template_id": {"type": "string", "description": "预留字段；当前版本不启用自定义模板"},
        },
        "required": ["format", "filename", "title"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后才能生成办公文档", error_code="INVALID_ARGS", retryable=False)
        output_dir = generic_outputs_dir(context.user_id, context.conversation_id or "default")
        try:
            path = render_document(dict(params or {}), Path(output_dir))
        except (OSError, ValueError, ImportError) as exc:
            return SkillResult(success=False, error=f"文档生成失败：{str(exc)[:300]}", error_code="DOCUMENT_RENDER_FAILED", retryable=False)
        return SkillResult(
            success=True,
            output=f"已生成文件：{path.name}",
            metadata={"outputs": [{"name": path.name, "size": path.stat().st_size, "generic": True}]},
        )
