"""办公技能（office/语音）：speech_to_text —— 语音转文字（Whisper）+ 简要总结."""

from app.agents.skills.base import WorkflowSkill, SkillContext, ToolOutput
from app.services.speech import speech_to_text as _transcribe


class SpeechToTextSkill(WorkflowSkill):
    name = "speech_to_text"
    description = (
        "语音转文字：把上传的语音/音频附件转成文字（Whisper 转写 + 纠错），"
        "并默认做简要总结。audio_url 为用户上传音频后返回的 /uploads 地址。"
    )
    category = "office"
    environment = "server"
    scenes = ["office", "chat"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "audio_url": {"type": "string", "description": "音频附件 URL（/uploads/ 开头）"},
            "summarize": {"type": "boolean", "description": "是否做简要总结（默认 true）"},
        },
        "required": ["audio_url"],
    }

    async def run(self, params: dict, context: SkillContext, invoke_tool) -> ToolOutput:
        if not context.user_id:
            return ToolOutput(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        audio_url = str(params.get("audio_url") or "").strip()
        if not audio_url.startswith(f"/uploads/{context.user_id}/"):
            return ToolOutput(
                success=False,
                error="audio_url 无效或不属于当前用户",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        text = await _transcribe(audio_url)
        if not text:
            return ToolOutput(
                success=False,
                error="语音转写失败或音频为空（检查音频格式/时长）",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        output = f"【语音转写】\n{text}"
        # 摘要属于另一个 WorkflowSkill，不能在这里越过工作流边界直接调用。
        # 调用方可以将其作为后续 DAG 节点编排，转写节点只交付原始转写结果。
        return ToolOutput(success=True, output=output, metadata={"transcript": text})

