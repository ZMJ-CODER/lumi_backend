"""技能插件（devtools/开发工具链）：search_codebase —— 语义搜索代码库（RAG）."""

import hashlib

from app.agents.skills.base import Skill, SkillContext, SkillResult


def _file_key(rel_path: str) -> str:
    """与服务端代码索引 file_key 相同的算法：sha256(相对路径) 前 32 位 hex."""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:32]


class SearchCodebaseSkill(Skill):
    name = "search_codebase"
    description = (
        "基于语义搜索代码库（调用服务端 RAG 向量检索），找到与描述最相关的代码文件/函数及行号。"
        "当需要定位「某个功能/逻辑写在哪个文件」、按语义查找代码时使用，比关键词更准。"
    )
    category = "devtools"
    environment = "server"
    requires_confirmation = False
    scenes = ["office"]
    parameters_schema = {
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "本地项目 ID"},
            "query": {"type": "string", "description": "语义描述，如：登录鉴权逻辑 / 订单状态机"},
            "top_k": {"type": "integer", "description": "返回结果数（默认 5）", "minimum": 1, "maximum": 20},
        },
        "required": ["project_id", "query"],
    }

    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        if not context or not context.user_id:
            return SkillResult(success=False, error="需要登录后使用", error_code="INVALID_ARGS", retryable=False)
        project_id = str(params.get("project_id") or "")
        query = str(params.get("query") or "").strip()
        if not project_id or not query:
            return SkillResult(
                success=False,
                error="缺少 project_id 或 query",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        top_k = min(int(params.get("top_k") or 5), 20)
        try:
            from app.core.database import async_session_factory
            from app.services import code_embedding, project_index

            async with async_session_factory() as session:
                hits = await code_embedding.search_code_vectors(
                    session, context.user_id, project_id, query, top_k=top_k
                )
                files = await project_index.list_project_files(
                    session, context.user_id, project_id, limit=2000
                )
            path_map = {_file_key(fp): fp for fp in files}
            results = []
            for h in hits:
                results.append(
                    {
                        "path": path_map.get(h.get("file_key")) or h.get("file_key"),
                        "function_name": h.get("function_name") or "",
                        "line_start": h.get("line_start"),
                        "line_end": h.get("line_end"),
                        "similarity": h.get("similarity"),
                        "summary": h.get("summary") or "",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return SkillResult(
                success=False,
                error=f"语义搜索失败: {exc}",
                error_code="EXEC_ERROR",
                retryable=True,
            )
        if not results:
            return SkillResult(
                success=False,
                error="代码库中未检索到相关内容（嵌入模型未就绪或项目无向量）",
                error_code="EXEC_ERROR",
                retryable=False,
            )
        lines = []
        for r in results:
            loc = f"{r['line_start']}-{r['line_end']}" if r.get("line_start") else "?"
            fn = f"（{r['function_name']}）" if r.get("function_name") else ""
            sim = f"相似度 {r.get('similarity')}" if r.get("similarity") is not None else ""
            lines.append(f"{r['path']} 行{loc} {fn} {sim}")
        return SkillResult(
            success=True,
            output="\n".join(lines),
            metadata={"results": results},
        )
