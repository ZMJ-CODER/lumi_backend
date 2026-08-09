"""文档分类：时效档次 + 开放主题标签.

设计要点:
  - 不读全文：只取标题/开头 + 均匀抽样片段（默认 ≤ 6000 字符），
    单文档分类 token 成本 ≈ 3~6K，与文档大小基本无关。
  - category = 时效档次（固定列表，决定半衰期）；tags = 开放主题标签（自由输出，展示/过滤用）。
  - 用户上传时选择的时效档次作为大模型的参考（允许纠正）。
  - LLM 不可用时回退用户选择，再回退本地 bge 零样本分类（0 token 成本）。
"""

import json
import re

from loguru import logger

from app.core.config import settings

CATEGORY_LABELS: dict[str, str] = {
    "news": "新闻（时效性强的时事报道、资讯、公告）",
    "general": "通用/技术文档（教程、说明、技术资料、日常知识）",
    "history": "历史（历史事件、史料、长期有效的经典知识）",
    "other": "其他（无法归入以上类别的内容）",
}

CATEGORY_KEYS = list(CATEGORY_LABELS)

MAX_TAGS = 3


def normalize_category(category: str | None) -> str | None:
    """规范化类别：未知值返回 None（交由兜底）."""
    if not category:
        return None
    cat = category.strip().lower()
    return cat if cat in CATEGORY_KEYS else None


def _sample_excerpt(text: str, max_chars: int = 6000) -> str:
    """抽样：标题/开头 + 正文均匀抽样，控制分类 token 成本."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = text[:2000]
    body = text[2000:]
    seg_len = 2000
    segments = []
    for i in range(2):
        start = i * (len(body) - seg_len) // 2
        segments.append(body[start : start + seg_len])
    return head + "\n\n……（文档中略）……\n\n" + "\n\n……\n\n".join(segments)


async def _llm_classify(excerpt: str, user_category: str | None) -> tuple[str, list[str]]:
    """大模型抽样分类：输出时效档次 + 开放主题标签（JSON）."""
    try:
        from app.core.llm import LLMClient

        category_desc = "\n".join(f"- {k}: {v}" for k, v in CATEGORY_LABELS.items())
        hint = ""
        if user_category:
            hint = f"\n用户上传时选择的时效档次是「{user_category}」，仅供你参考，可以纠正。"
        messages = [
            {
                "role": "system",
                "content": (
                    "你是文档分类助手。根据文档内容输出两部分：\n"
                    "1. category：文档的时效档次，只允许从以下列表选一个："
                    f"{', '.join(CATEGORY_KEYS)}。\n"
                    "2. tags：1~3 个开放主题标签（如 科技、金融、法律、游戏、会议纪要、教程），"
                    "用中文短语描述文档主题。\n"
                    "只输出 JSON，格式：{\"category\": \"档位\", \"tags\": [\"标签1\", \"标签2\"]}，"
                    "不要输出其他任何内容。\n时效档次说明：\n" + category_desc
                ),
            },
            {
                "role": "user",
                "content": f"请判断下面文档的类别：{hint}\n\n文档内容（抽样）：\n{excerpt[:4000]}",
            },
        ]
        client = LLMClient()
        await client.start()
        reply = (await client.chat(messages)).strip().lower()
        # 优先解析 JSON
        try:
            payload = json.loads(re.search(r"\{.*\}", reply, re.DOTALL).group(0))
            category = payload.get("category", "")
            tags = payload.get("tags", [])
        except Exception:
            category, tags = "", []
        category = normalize_category(category) or ""
        tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()][:MAX_TAGS]
        if category:
            return category, tags
        # JSON 解析失败：在回复里找档位标识
        for key in CATEGORY_KEYS:
            if re.search(rf"\b{key}\b", reply):
                return key, tags
        logger.warning("LLM 分类输出无法解析: {}", reply[:120])
    except Exception as e:
        logger.warning("LLM 文档分类失败，使用兜底: {}", e)
    return "", []


async def _embedding_classify(text: str) -> str:
    """本地 bge 零样本分类：文档向量与时效档次描述向量比较余弦相似度."""
    try:
        from app.services.rag.embeddings import embed_query, embed_texts

        doc_vec = await embed_query(text[:2000])
        desc_vecs = await embed_texts(list(CATEGORY_LABELS.values()))
        best = max(
            range(len(desc_vecs)),
            key=lambda i: sum(a * b for a, b in zip(doc_vec, desc_vecs[i])),
        )
        return CATEGORY_KEYS[best]
    except Exception as e:
        logger.warning("嵌入零样本分类失败: {}", e)
        return ""


async def classify_document(
    text: str,
    user_category: str | None = None,
) -> tuple[str, list[str]]:
    """判断文档：返回 (时效档次, 开放标签).

    链路：LLM 抽样判断 → 用户选择兜底 → 嵌入零样本 → 默认档次。
    """
    user_cat = normalize_category(user_category)
    if not text or not text.strip():
        return user_cat or settings.RAG_DEFAULT_CATEGORY, []

    excerpt = _sample_excerpt(text)
    category, tags = await _llm_classify(excerpt, user_cat)
    if category:
        return category, tags
    if user_cat:
        return user_cat, []
    category = await _embedding_classify(excerpt)
    return category or settings.RAG_DEFAULT_CATEGORY, []
