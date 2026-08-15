"""技能插件（devtools/开发工具链）：review_code —— 对代码变更进行逻辑审查."""

import json
import re

from app.agents.skills.base import Skill, SkillContext, SkillResult

_REVIEW_PROMPT = (
    "你是资深代码审查员。审查代码是否满足用户指令、是否存在明显 bug 或安全隐患。"
    "只输出 JSON：{\"approved\": true 或 false, \"issues\": [\"问题1\", \"问题2\"], \"feedback\": \"一句话总结\"}"
)


def _extract_json(text: str | None) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


class ReviewCodeSkill(Skill):
    name = "review_code"
    description = (
        "对代码变更进行逻辑审查：检查是否满足需求、有无 bug/安全隐患，"
        "返回通过/不通过 + 问题清单 + 总结。修改代码后或需要把关时使用。"
    )
    category = "devtools"
    environment = "server"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "instruction": {"type": "string", "description": "用户指令/需求（审查依据）"},
            "path": {"type": "string", "description": "可选：文件路径"},
            "content": {"type": "string", "description": "要审查的代码内容"},
        },
        "required": ["content"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        content = str(params.get("content") or "")
        if not content.strip():
            return SkillResult(success=False, error="缺少要审查的代码 content", error_code="INVALID_ARGS", retryable=False)
        instruction = str(params.get("instruction") or "")
        path = str(params.get("path") or "")
        try:
            from app.core.llm import LLMClient
            from app.services.usage import CATEGORY_REVIEW

            llm = LLMClient()
            reply = await llm.chat(
                [
                    {"role": "system", "content": _REVIEW_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"用户指令：{instruction}\n文件路径：{path}\n代码内容：\n{content[:12000]}"
                        ),
                    },
                ],
                temperature=0.1,
                max_tokens=4096,
                usage_user_id=context.user_id,
                usage_category=CATEGORY_REVIEW,
                reasoning_effort="low",
                api_key=context.llm_api_key,
            )
            data = _extract_json(reply)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"代码审查失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        if not data:
            return SkillResult(
                success=False,
                error="模型未返回有效审查结果",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        issues = data.get("issues") or []
        if not isinstance(issues, list):
            issues = []
        feedback = str(data.get("feedback") or "")
        approved = bool(data.get("approved"))
        verdict = "通过" if approved else "不通过"
        output = f"审查结论：{verdict}\n"
        if issues:
            output += "问题清单：\n" + "\n".join(f"- {i}" for i in issues[:20])
        if feedback:
            output += f"\n总结：{feedback}"
        return SkillResult(
            success=True,
            output=output,
            metadata={
                "approved": approved,
                "issues": [str(i) for i in issues[:20]],
                "feedback": feedback,
            },
        )
