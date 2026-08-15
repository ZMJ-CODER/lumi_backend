"""技能插件（devtools/开发工具链）：generate_tests —— 为指定代码生成测试用例."""

from app.agents.skills.base import Skill, SkillContext, SkillResult

_GENERATE_TESTS_PROMPT = (
    "你是测试工程师。根据代码内容和语言，生成完整的测试文件。"
    "只输出测试代码本身，不要解释、不要 Markdown 代码块围栏。"
)


class GenerateTestsSkill(Skill):
    name = "generate_tests"
    description = (
        "为指定代码生成测试用例（按语言选框架：pytest / Jest / Go testing 等），"
        "返回可直接保存的测试文件内容。需要补测试时使用。"
    )
    category = "devtools"
    environment = "server"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "被测代码内容"},
            "path": {"type": "string", "description": "可选：代码文件路径（用于判断语言/测试文件命名）"},
            "instruction": {"type": "string", "description": "可选：补充测试要求（如覆盖边界条件）"},
        },
        "required": ["content"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        content = str(params.get("content") or "")
        if not content.strip():
            return SkillResult(success=False, error="缺少被测代码 content", error_code="INVALID_ARGS", retryable=False)
        path = str(params.get("path") or "")
        instruction = str(params.get("instruction") or "")
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_CODE

            llm = LLMClient()
            prompt_parts = []
            if path:
                prompt_parts.append(f"文件路径：{path}")
            if instruction:
                prompt_parts.append(f"额外要求：{instruction}")
            prompt_parts.append(f"被测代码：\n{content[:30000]}")
            reply = await llm.chat(
                [
                    {"role": "system", "content": _GENERATE_TESTS_PROMPT},
                    {
                        "role": "user",
                        "content": "\n".join(prompt_parts),
                    },
                ],
                temperature=0.2,
                max_tokens=16000,
                usage_user_id=context.user_id,
                usage_category=CATEGORY_CODE,
                reasoning_effort="low",
                api_key=context.llm_api_key,
            )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"生成测试失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        reply = (reply or "").strip()
        if not reply:
            return SkillResult(success=False, error="模型未生成测试代码", error_code="EXEC_ERROR", retryable=True)
        if reply.startswith("```"):
            import re

            reply = re.sub(r"^```\w*\n?", "", reply)
            reply = re.sub(r"\n?```$", "", reply)
        return SkillResult(success=True, output=reply, metadata={"path": path})
