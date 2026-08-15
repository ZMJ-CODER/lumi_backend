"""办公技能（office/语音）：speech_to_text —— 语音转文字（Whisper）+ 简要总结."""

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.registry import SkillRegistry
from app.services.speech import speech_to_text as _transcribe


class SpeechToTextSkill(Skill):
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

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        audio_url = str(params.get("audio_url") or "").strip()
        if not audio_url.startswith(f"/uploads/{context.user_id}/"):
            return SkillResult(
                success=False,
                error="audio_url 无效或不属于当前用户",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        summarize = bool(params.get("summarize", True))
        text = await _transcribe(audio_url)
        if not text:
            return SkillResult(
                success=False,
                error="语音转写失败或音频为空（检查音频格式/时长）",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        output = f"【语音转写】\n{text}"
        if summarize:
            summarize_skill = SkillRegistry.get("summarize_text")
            if summarize_skill:
                r = await summarize_skill.execute({"text": text, "format": "要点"}, context)
                if r.success:
                    output += f"\n\n【简要总结】\n{r.output}"
        return SkillResult(success=True, output=output, metadata={"transcript": text})
