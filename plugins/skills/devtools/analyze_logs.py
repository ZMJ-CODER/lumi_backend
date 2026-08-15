"""技能插件（devtools/开发工具链）：analyze_logs —— 分析错误日志，提取关键错误与根因."""

from app.agents.skills.base import Skill, SkillContext, SkillResult

_ANALYZE_LOGS_PROMPT = (
    "你是运维/调试专家。分析下面的日志，输出：\n"
    "1. 关键错误（时间、错误类型、出现次数）\n"
    "2. 每类错误的可能根因\n"
    "3. 修复建议（按优先级）\n"
    "保持简洁，用中文回答。"
)


class AnalyzeLogsSkill(Skill):
    name = "analyze_logs"
    description = (
        "分析错误日志（应用/服务端/控制台日志），提取关键错误信息、可能根因与修复建议。"
        "当用户给出报错日志或让排查问题时使用。"
    )
    category = "devtools"
    environment = "server"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "日志内容（错误日志/堆栈）"},
            "context": {"type": "string", "description": "可选：补充背景（什么操作时报错、环境等）"},
        },
        "required": ["content"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        content = str(params.get("content") or "")
        if not content.strip():
            return SkillResult(success=False, error="缺少日志内容 content", error_code="INVALID_ARGS", retryable=False)
        context_text = str(params.get("context") or "")
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_CHAT

            llm = LLMClient()
            reply = await llm.chat(
                [
                    {"role": "system", "content": _ANALYZE_LOGS_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"背景：{context_text}\n" if context_text else ""
                            f"日志（最多 30000 字）：\n{content[:30000]}"
                        ),
                    },
                ],
                temperature=0.2,
                max_tokens=4096,
                usage_user_id=context.user_id,
                usage_category=CATEGORY_CHAT,
                reasoning_effort="low",
                api_key=context.llm_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"日志分析失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        if not (reply or "").strip():
            return SkillResult(success=False, error="模型未返回分析结果", error_code="EXEC_ERROR", retryable=True)
        return SkillResult(success=True, output=(reply or "").strip())
