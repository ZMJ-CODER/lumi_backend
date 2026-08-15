"""技能插件（devtools/开发工具链）：search_codebase —— 语义搜索代码库（本地）.

代码全本地方案后，服务端不再持有代码向量，因此本技能改为：
  1. 从用户语义描述中提取候选关键词（英文标识符 + 中文片段）；
  2. 调用客户端 grep_code 在本地文件系统搜索代码内容（正则）；
  3. 去重后按命中顺序返回。
"""

import re

from app.agents.skills.base import Skill, SkillContext, SkillResult
from app.agents.skills.executor import run_client_skill_request


# 常见英文虚词/指令词，不作为代码搜索关键词
_LATIN_STOP = {
    "the", "and", "for", "with", "that", "this", "how", "why", "what",
    "where", "which", "when", "code", "file", "files", "function", "class",
    "project", "search", "find", "locate", "show", "list", "get", "set",
    "please", "write", "edit", "create", "delete", "implement", "need",
    "want", "can", "you", "me", "my", "into", "from", "have", "are", "was",
    "has", "its", "not", "all", "any", "api", "app", "main", "index",
}

# 中文泛化词/语气词：不作为代码搜索关键词
_CJK_STOP = (
    "的", "了", "吗", "呢", "啊", "吧", "哦", "在", "是", "把", "被", "让",
    "请", "帮", "帮我", "一下", "什么", "怎么", "为什么", "如何", "这个", "那个",
    "项目", "代码", "文件", "功能", "逻辑", "函数", "实现", "修改", "创建", "添加",
    "删除", "优化", "重构", "看看", "读取", "写入", "里面", "关于", "找到",
    "需要", "要求", "内容", "部分", "相关", "支持", "使用", "进行", "一个",
)


def _extract_keywords(query: str) -> list[str]:
    """从语义描述中提取候选关键词：英文标识符 + 中文连续片段."""
    tokens = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", query) if t.lower() not in _LATIN_STOP]
    # 中文：先剔除泛化词，再把剩余连续片段拆成 2 字词元（注释/字符串里通常直接出现）
    cleaned = query
    for w in _CJK_STOP:
        cleaned = cleaned.replace(w, " ")
    cjk = []
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", cleaned):
        if len(run) <= 2:
            cjk.append(run)
        else:
            cjk.extend(run[i : i + 2] for i in range(0, len(run), 2))
    cjk = [c for c in cjk if len(c) == 2]
    keywords = tokens[:4] + cjk[:2]
    seen = set()
    out = []
    for k in keywords:
        key = k.lower()
        if key not in seen:
            seen.add(key)
            out.append(k)
    return out


class SearchCodebaseSkill(Skill):
    name = "search_codebase"
    description = (
        "在本地代码项目中按语义描述搜索相关代码（提取关键词后直接 grep 本地文件系统，"
        "不依赖服务端向量/索引），返回 文件:行号:代码片段。"
        "当需要定位「某个功能/逻辑写在哪个文件」时使用。"
    )
    category = "devtools"
    environment = "client"
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

        keywords = _extract_keywords(query)
        if not keywords:
            return SkillResult(
                success=False,
                error="无法从描述中提取有效关键词，建议改用 explore_project / get_project_context 查看项目结构后再定位",
                error_code="INVALID_ARGS",
                retryable=False,
            )
        pattern = "|".join(re.escape(k) for k in keywords)
        if context.on_notify:
            context.on_notify(f"（正在本地搜索代码：{' / '.join(keywords[:3])}）")

        results: list[dict] = []
        try:
            grep_res = await run_client_skill_request(
                context.user_id,
                "grep_code",
                {
                    "project_id": project_id,
                    "pattern": pattern,
                    "max_results": min(top_k * 3, 100),
                },
                False,
            )
            for item in (grep_res.metadata or {}).get("results") or []:
                results.append(
                    {
                        "path": str(item.get("path") or ""),
                        "line": item.get("line"),
                        "snippet": str(item.get("snippet") or ""),
                        "source": "content",
                    }
                )
        except Exception:  # noqa: BLE001
            pass

        # 去重（path + line）
        seen = set()
        merged: list[dict] = []
        for r in results:
            key = (r["path"], r.get("line"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(r)
        if not merged:
            return SkillResult(
                success=False,
                error="本地代码中未检索到相关内容，建议先用 explore_project 看目录结构或描述更精确的功能名",
                error_code="EXEC_ERROR",
                retryable=False,
            )
        merged = merged[:top_k]
        lines = []
        for r in merged:
            if r.get("line"):
                lines.append(f"{r['path']}:{r['line']}  {r.get('snippet') or ''}".rstrip())
            else:
                lines.append(f"{r['path']}（文件名匹配）")
        return SkillResult(
            success=True,
            output="\n".join(lines),
            metadata={"results": merged},
        )
