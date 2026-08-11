"""RAG 知识库服务 —— 知识空间 / 文档 / 分块入库 / 向量检索（pgvector + bge-small-zh-v1.5嵌入）.

数据流:
  上传文档 → 落盘 + documents 表(pending) → Celery 处理管线
           → 数据清洗→ 解析 → 分块 → 嵌入 → document_chunks 表 → documents.status=ready
  对话检索 → 查询向量 → pgvector 余弦相似度 → 拼接上下文 + 引用列表
"""

import hashlib
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.services.rag.chunker import chunk_document
from app.services.rag.classifier import classify_document, normalize_category
from app.services.rag.embeddings import embed_query, embed_texts
from app.models.db_models import Document, DocumentChunk, KnowledgeSpace
from app.services.rag.cleaner import HARD_FAIL_CODES, DocumentQualityError, assess_document, clean_document
from app.services.rag.document_parser import parse_document

MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 MB


# ── 工具 ─────────────────────────────────────────────

def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _vector_str(vec: list[float]) -> str:
    """pgvector 字符串字面量: [0.1,0.2,...]."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _doc_file_path(user_id: str, doc_id: uuid.UUID, filename: str) -> Path:
    ext = Path(filename).suffix.lower()
    return Path(settings.UPLOAD_DIR) / str(user_id) / f"{doc_id}{ext}"


async def _ensure_space_owned(session: AsyncSession, space_id: str, user_id: str) -> KnowledgeSpace:
    """校验知识空间存在且属于当前用户."""
    space = await session.get(KnowledgeSpace, _uuid(space_id))
    if not space:
        raise LookupError("知识空间不存在")
    if str(space.user_id) != str(user_id) and not space.is_public:
        raise PermissionError("无权操作该知识空间")
    return space


# ── 知识空间 ─────────────────────────────────────────

async def create_space(
    session: AsyncSession,
    user_id: str,
    name: str,
    description: str = "",
    scene_tag: str | None = None,
    is_public: bool = False,
) -> KnowledgeSpace:
    """创建知识空间."""
    uid = _uuid(user_id)
    if not uid:
        raise ValueError("无效的用户 ID")
    space = KnowledgeSpace(
        user_id=uid,
        name=name,
        description=description,
        scene_tag=scene_tag or None,
        is_public=is_public,
    )
    session.add(space)
    await session.flush()
    return space


async def list_spaces(session: AsyncSession, user_id: str, include_public: bool = True) -> list[dict]:
    """列出我的知识空间（含公共空间）."""
    uid = _uuid(user_id)
    stmt = select(KnowledgeSpace).order_by(KnowledgeSpace.created_at.desc())
    if uid:
        stmt = stmt.where((KnowledgeSpace.user_id == uid) | (KnowledgeSpace.is_public.is_(True)))
    elif include_public:
        stmt = stmt.where(KnowledgeSpace.is_public.is_(True))
    else:
        return []
    spaces = (await session.execute(stmt)).scalars().all()
    return [
        {
            "space_id": str(s.id),
            "name": s.name,
            "description": s.description or "",
            "scene_tag": s.scene_tag,
            "is_public": s.is_public,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in spaces
    ]


async def update_space(
    session: AsyncSession,
    space_id: str,
    user_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    scene_tag: str | None = None,
    is_public: bool | None = None,
    is_admin: bool = False,
) -> KnowledgeSpace:
    """更新知识空间（公共标记仅管理员可设置）."""
    space = await _ensure_space_owned(session, space_id, user_id)
    if name is not None:
        space.name = name
    if description is not None:
        space.description = description
    if scene_tag is not None:
        space.scene_tag = scene_tag or None
    if is_public is not None:
        if not is_admin and is_public:
            raise PermissionError("仅管理员可设置公共空间")
        space.is_public = is_public
    await session.flush()
    return space


async def delete_space(session: AsyncSession, space_id: str, user_id: str) -> bool:
    """删除知识空间（级联删除文档、分块与磁盘文件）."""
    space = await _ensure_space_owned(session, space_id, user_id)
    docs = (await session.execute(select(Document).where(Document.space_id == space.id))).scalars().all()
    doc_ids = [d.id for d in docs]
    if doc_ids:
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id.in_(doc_ids)))
    await session.execute(delete(Document).where(Document.space_id == space.id))
    await session.delete(space)
    await session.flush()
    # 清理磁盘文件
    for doc in docs:
        path = _doc_file_path(str(doc.user_id), doc.id, doc.filename)
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除文件失败 {}: {}", path, e)
    return True


# ── 文档管理 ─────────────────────────────────────────

async def upload_document_file(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    filename: str,
    content: bytes,
    category: str | None = None,
) -> tuple[Document, Path, bool]:
    """保存上传文件并创建文档记录（status=pending，等待 Celery 处理）.

    Returns:
        (doc, file_path, is_new)：is_new=False 表示命中去重，直接返回已有文档，
        调用方不应重复入队。
    """
    if len(content) > MAX_UPLOAD_SIZE:
        raise ValueError("文件超过 20MB 限制")
    space = await _ensure_space_owned(session, space_id, user_id)

    file_hash = hashlib.sha256(content).hexdigest()
    # 同空间同内容已有文档（pending/processing/ready）→ 直接返回，
    # 避免上传超时重试等场景产生重复文档占用队列资源
    existing = (
        await session.execute(
            select(Document).where(
                Document.space_id == space.id,
                Document.file_hash == file_hash,
                Document.status.in_(("pending", "processing", "ready")),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing, _doc_file_path(user_id, existing.id, existing.filename), False

    doc_id = uuid.uuid4()
    path = _doc_file_path(user_id, doc_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)

    doc = Document(
        id=doc_id,
        space_id=space.id,
        user_id=space.user_id,
        filename=filename,
        file_hash=file_hash,
        file_size=len(content),
        status="pending",
        chunk_count=0,
        category=normalize_category(category) or settings.RAG_DEFAULT_CATEGORY,
    )
    session.add(doc)
    await session.flush()
    return doc, path, True


async def list_documents(
    session: AsyncSession,
    user_id: str,
    space_id: str,
    status: str = "ready",
    limit: int = 20,
) -> list[dict]:
    """列出空间内的文档."""
    await _ensure_space_owned(session, space_id, user_id)
    stmt = (
        select(Document)
        .where(Document.space_id == _uuid(space_id))
        .order_by(Document.created_at.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(Document.status == status)
    docs = (await session.execute(stmt)).scalars().all()
    return [
        {
            "document_id": str(d.id),
            "filename": d.filename,
            "file_size": d.file_size,
            "status": d.status,
            "chunk_count": d.chunk_count,
            "category": d.category,
            "tags": d.tags,
            "error_message": d.error_message,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
        }
        for d in docs
    ]


async def delete_document(session: AsyncSession, document_id: str, user_id: str) -> bool:
    """删除文档（分块 + 记录 + 磁盘文件）."""
    doc = await session.get(Document, _uuid(document_id))
    if not doc:
        raise LookupError("文档不存在")
    if str(doc.user_id) != str(user_id):
        raise PermissionError("无权删除该文档")
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    await session.delete(doc)
    await session.flush()
    path = _doc_file_path(str(doc.user_id), doc.id, doc.filename)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        logger.warning("删除文件失败 {}: {}", path, e)
    return True


# ── 文档处理管线（Celery 调用） ───────────────────────

async def process_document_pipeline(
    session: AsyncSession,
    document_id: str,
    file_path: str,
    user_category: str | None = None,
) -> int:
    """文档处理管线: 解析 → 清洗 → 质量门 → 分类 → 分块 → 嵌入 → 入库.

    Returns:
        入库的分块数量
    """
    doc = await session.get(Document, _uuid(document_id))
    if not doc:
        raise LookupError(f"文档不存在: {document_id}")

    try:
        try:
            raw_text = parse_document(file_path, doc.filename)
        except Exception as e:
            # 解析失败归类为"无法解析"（如严重损坏的 PDF），终止处理不重试
            raise DocumentQualityError(
                f"unparsable：无法解析文档（{type(e).__name__}: {str(e)[:120]}）"
            ) from e
        # 清洗 + 质量门：低质量文档不入库
        cleaned_text = clean_document(raw_text, doc.filename)
        score, issues = assess_document(cleaned_text, filename=doc.filename, file_size=doc.file_size)
        if score < settings.RAG_MIN_QUALITY_SCORE or any(i["code"] in HARD_FAIL_CODES for i in issues):
            messages = [i["message"] for i in issues] or ["无法识别有效内容"]
            raise DocumentQualityError(
                f"文档质量不达标（{score:.2f}）：{'；'.join(messages)}"
            )
        # 文档分类：时效档次 + 开放标签（大模型抽样判断，用户选择的档次作为参考）
        category, tags = await classify_document(cleaned_text, user_category or doc.category)
        doc.category = category
        doc.tags = ", ".join(tags) if tags else None
        chunks = chunk_document(cleaned_text, doc.filename, settings.RAG_CHUNK_SIZE, settings.RAG_CHUNK_OVERLAP)

        # 重处理时清掉旧分块
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))

        if chunks:
            embeddings = await embed_texts(chunks)
            if len(embeddings) != len(chunks):
                raise RuntimeError("嵌入数量与分块数量不一致")
            for i, (chunk_text, vec) in enumerate(zip(chunks, embeddings)):
                session.add(
                    DocumentChunk(
                        document_id=doc.id,
                        space_id=doc.space_id,
                        user_id=doc.user_id,
                        chunk_index=i,
                        chunk_text=chunk_text,
                        embedding=vec,
                    )
                )

        doc.status = "ready"
        doc.chunk_count = len(chunks)
        doc.error_message = None
        await session.commit()
        logger.debug("✅ 文档 {} 处理完成，chunks={}", doc.filename, len(chunks))
        return len(chunks)
    except Exception as e:
        await session.rollback()
        failed = await session.get(Document, _uuid(document_id))
        if failed:
            failed.status = "error"
            failed.error_message = str(e)[:500]
            await session.commit()
        logger.error("❌ 文档 {} 处理失败: {}", document_id, e)
        raise


# ── 混合检索 ─────────────────────────────────────────

_STOPWORDS = {"什么", "怎么", "如何", "为什么", "一个", "这个", "那个", "一下", "the", "and", "for", "with"}

# 时间意图：查询里出现这些词时提高时效性权重
_TIME_INTENT_RE = re.compile(
    r"最新|最近|近期|近\s*\d+\s*(天|周|月|年)|今年|本月|上周|昨天|刚刚|最新的"
)


def _recency_score(created_at, half_life_days: int | None = None) -> float:
    """文档时效性分数：按指数衰减，越新越接近 1.

    recency = exp(-age_days / half_life_days)
    """
    if created_at is None:
        return 0.0
    half_life = half_life_days or settings.RAG_RECENCY_HALF_LIFE_DAYS
    try:
        age_days = (datetime.now(timezone.utc) - created_at).total_seconds() / 86400
    except TypeError:
        return 0.0
    if age_days <= 0:
        return 1.0
    return math.exp(-age_days / half_life)


def _category_half_life(category: str | None) -> int:
    """按文档类别取半衰期，未知类别回落默认类别."""
    mapping = settings.RAG_CATEGORY_HALF_LIFE_DAYS
    if category and category in mapping:
        return mapping[category]
    return mapping.get(settings.RAG_DEFAULT_CATEGORY, 180)


def _time_intent_weight(query: str) -> float:
    """查询含时间意图时返回更高权重，否则默认权重."""
    if _TIME_INTENT_RE.search(query or ""):
        return settings.RAG_RECENCY_QUERY_WEIGHT
    return settings.RAG_RECENCY_WEIGHT


def _apply_recency(rows: list[dict], weight: float) -> list[dict]:
    """把时效性融入最终排序：final = (1-w)*相关性归一 + w*时效性."""
    if not rows:
        return rows
    max_score = max((r.get("score") or r.get("similarity") or 0.0) for r in rows)
    if max_score <= 0:
        return rows
    for r in rows:
        relevance = (r.get("score") or r.get("similarity") or 0.0) / max_score
        recency = _recency_score(r.get("created_at"), _category_half_life(r.get("category")))
        r["recency"] = round(recency, 4)
        r["score"] = round((1 - weight) * relevance + weight * recency, 4)
    return sorted(rows, key=lambda x: x["score"], reverse=True)


def _extract_keywords(query: str, top: int = 5) -> list[str]:
    """提取查询关键字：jieba 中文分词为主，拉丁词 / 中文二元组兜底."""
    keywords: list[str] = []
    try:
        import jieba.analyse
        tags = jieba.analyse.extract_tags(query, topK=top, withWeight=False)
        keywords.extend(t.strip() for t in tags if t.strip() and len(t.strip()) > 1)
    except Exception:
        pass

    seen = {k.lower() for k in keywords}
    # 拉丁词兜底（代码/英文术语）
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{1,}", query):
        tl = token.lower()
        if tl not in seen and tl not in _STOPWORDS:
            keywords.append(token)
            seen.add(tl)
    # 中文二元组兜底（jieba 不可用时）
    if not keywords:
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query):
            for i in range(len(seg) - 1):
                bigram = seg[i : i + 2]
                if bigram not in seen:
                    keywords.append(bigram)
                    seen.add(bigram)
                if len(keywords) >= top:
                    break
            if len(keywords) >= top:
                break
    return keywords[:top]


def _hybrid_fuse(vector_rows, keyword_rows, top_k: int) -> list[dict]:
    """Reciprocal Rank Fusion：融合向量与关键词两路召回结果.

    RRF 分数 = Σ 1/(k + rank)，对排序位置敏感、对相似度绝对值不敏感，
    天然适合混合不同度量（余弦相似度 vs 关键词命中数）的两路结果。
    """
    rrf_k = 60
    merged: dict[str, dict] = {}
    for rank, row in enumerate(vector_rows, 1):
        cid = str(row["chunk_id"])
        entry = merged.setdefault(
            cid,
            {
                "chunk_id": cid,
                "chunk_text": row["chunk_text"],
                "title": row["title"],
                "document_id": str(row["document_id"]),
                "is_public": bool(row["is_public"]),
                "created_at": row["created_at"],
                "category": row["category"],
                "similarity": round(float(row["similarity"]), 4),
                "score": 0.0,
            },
        )
        entry["score"] += 1.0 / (rrf_k + rank)
    for rank, row in enumerate(keyword_rows, 1):
        cid = str(row["chunk_id"])
        entry = merged.get(cid)
        if entry:
            entry["score"] += 1.0 / (rrf_k + rank)
        else:
            merged[cid] = {
                "chunk_id": cid,
                "chunk_text": row["chunk_text"],
                "title": row["title"],
                "document_id": str(row["document_id"]),
                "is_public": bool(row["is_public"]),
                "created_at": row["created_at"],
                "category": row["category"],
                "similarity": None,
                "score": 1.0 / (rrf_k + rank),
            }
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]


def _scope_conditions(user_id: str, space_tags: list[str], need_embedding: bool) -> tuple[list[str], dict]:
    """构造检索范围条件（个人空间 + 公共空间，按场景标签过滤）."""
    conds: list[str] = []
    params: dict = {}
    if need_embedding:
        conds.append("c.embedding IS NOT NULL")
    uid = _uuid(user_id)
    if uid:
        conds.append("(c.user_id = :uid OR s.is_public = true)")
        params["uid"] = uid
    else:
        conds.append("s.is_public = true")
    if space_tags:
        conds.append("s.scene_tag IN :tags")
        params["tags"] = list(space_tags)
    if settings.RAG_TIME_FILTER_DAYS:
        conds.append("d.created_at >= :min_time")
        params["min_time"] = datetime.now(timezone.utc) - timedelta(days=settings.RAG_TIME_FILTER_DAYS)
    return conds, params


async def search_user_knowledge(
    session: AsyncSession,
    user_id: str,
    query: str,
    space_tags: list[str],
    top_k: int | None = None,
    threshold: float | None = None,
) -> tuple[str, list[dict]]:
    """对话 RAG 混合检索：向量相似度 top-N + 关键词检索，RRF 融合.

    Returns:
        (拼接后的上下文文本, 引用列表)
    """
    top_k = top_k or settings.RAG_TOP_K
    if not query or not query.strip():
        return "", []

    conds, params = _scope_conditions(user_id, space_tags, need_embedding=True)
    fused: list[dict] = []

    # ── 第一路：向量相似度 top-N ──
    vec = await embed_query(query)
    if vec:
        vec_top = settings.RAG_HYBRID_VECTOR_TOP_K
        sql = f"""
            SELECT c.id AS chunk_id, c.chunk_text, d.filename AS title,
                   d.id AS document_id, s.is_public, d.created_at AS created_at, d.category AS category,
                   1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            JOIN knowledge_spaces s ON s.id = c.space_id
            WHERE {' AND '.join(conds)}
            ORDER BY c.embedding <=> CAST(:qvec AS vector)
            LIMIT :top_k
        """
        stmt = text(sql).bindparams(
            bindparam("qvec", _vector_str(vec)),
            bindparam("top_k", vec_top),
        )
        if space_tags:
            stmt = stmt.bindparams(bindparam("tags", expanding=True))
        rows = (await session.execute(stmt, params)).mappings().all()
        fused = [dict(r) for r in rows]

    # ── 第二路：关键词检索 top-N ──
    keywords = _extract_keywords(query)
    if keywords:
        kw_top = settings.RAG_HYBRID_KEYWORD_TOP_K
        kw_conds, kw_params = _scope_conditions(user_id, space_tags, need_embedding=False)
        kw_conds.append(
            "(" + " OR ".join(f"c.chunk_text ILIKE :kw{i}" for i in range(len(keywords))) + ")"
        )
        match_expr = " + ".join(
            f"(CASE WHEN c.chunk_text ILIKE :kw{i} THEN 1 ELSE 0 END)" for i in range(len(keywords))
        )
        kw_params.update({f"kw{i}": f"%{kw}%" for i, kw in enumerate(keywords)})
        sql = f"""
            SELECT c.id AS chunk_id, c.chunk_text, d.filename AS title,
                   d.id AS document_id, s.is_public, d.created_at AS created_at, d.category AS category,
                   ({match_expr}) AS kw_score
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            JOIN knowledge_spaces s ON s.id = c.space_id
            WHERE {' AND '.join(kw_conds)}
            ORDER BY kw_score DESC, c.created_at DESC
            LIMIT :top_k
        """
        stmt = text(sql).bindparams(bindparam("top_k", kw_top))
        if space_tags:
            stmt = stmt.bindparams(bindparam("tags", expanding=True))
        kw_rows = (await session.execute(stmt, kw_params)).mappings().all()
        fused = _hybrid_fuse(fused, kw_rows, top_k)
    elif fused:
        fused = fused[:top_k]

    # 时效性加权：相关性为主，新文档占优（时间意图查询权重更高）
    fused = _apply_recency(fused, _time_intent_weight(query))

    context_parts: list[str] = []
    citations: list[dict] = []
    for i, row in enumerate(fused, 1):
        context_parts.append(f"[{i}] {row['chunk_text']}")
        citations.append(
            {
                "type": "public" if row["is_public"] else "personal",
                "title": row["title"],
                "content": row["chunk_text"],
                "source": row["title"],
                "document_id": row["document_id"],
                "similarity": row.get("similarity"),
                "created_at": row.get("created_at"),
                "category": row.get("category"),
                "recency": row.get("recency"),
                "score": round(float(row.get("score") or 0.0), 4),
            }
        )

    if citations:
        logger.debug("🔍 RAG 命中 {} 条引用", len(citations))
    return "\n\n".join(context_parts), citations


async def search_public_vectors(
    session: AsyncSession,
    query_vector: list[float],
    space_tags: list[str] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
    query: str = "",
) -> list[dict]:
    """公共知识库混合检索（供 /public-kb/search 使用）.

    传 query 文本时走"向量 + 关键词 + RRF 融合"；只传向量时退化为纯向量检索。
    """
    top_k = top_k or settings.RAG_TOP_K
    threshold = settings.RAG_SIMILARITY_THRESHOLD if threshold is None else threshold
    if not query_vector:
        return []

    def _public_conds(with_embedding: bool) -> tuple[list[str], dict]:
        conds = ["s.is_public = true"]
        if with_embedding:
            conds.append("c.embedding IS NOT NULL")
        params: dict = {}
        if space_tags:
            conds.append("s.scene_tag IN :tags")
            params["tags"] = list(space_tags)
        if settings.RAG_TIME_FILTER_DAYS:
            conds.append("d.created_at >= :min_time")
            params["min_time"] = datetime.now(timezone.utc) - timedelta(days=settings.RAG_TIME_FILTER_DAYS)
        return conds, params

    fused: list[dict] = []

    # ── 第一路：向量相似度 top-N ──
    conds, params = _public_conds(with_embedding=True)
    sql = f"""
        SELECT c.id AS chunk_id, c.chunk_text, d.filename AS title,
               d.id AS document_id, s.scene_tag, d.created_at AS created_at, d.category AS category,
               1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN knowledge_spaces s ON s.id = c.space_id
        WHERE {' AND '.join(conds)}
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
    """
    stmt = text(sql).bindparams(
        bindparam("qvec", _vector_str(query_vector)),
        bindparam("top_k", settings.RAG_HYBRID_VECTOR_TOP_K),
    )
    if space_tags:
        stmt = stmt.bindparams(bindparam("tags", expanding=True))
    rows = (await session.execute(stmt, params)).mappings().all()
    fused = [dict(r) for r in rows]

    # ── 第二路：关键词检索 top-N（有 query 文本才走） ──
    keywords = _extract_keywords(query) if query else []
    if keywords:
        kw_conds, kw_params = _public_conds(with_embedding=False)
        kw_conds.append(
            "(" + " OR ".join(f"c.chunk_text ILIKE :kw{i}" for i in range(len(keywords))) + ")"
        )
        match_expr = " + ".join(
            f"(CASE WHEN c.chunk_text ILIKE :kw{i} THEN 1 ELSE 0 END)" for i in range(len(keywords))
        )
        kw_params.update({f"kw{i}": f"%{kw}%" for i, kw in enumerate(keywords)})
        sql = f"""
            SELECT c.id AS chunk_id, c.chunk_text, d.filename AS title,
                   d.id AS document_id, s.scene_tag, d.created_at AS created_at, d.category AS category,
                   ({match_expr}) AS kw_score
            FROM document_chunks c
            JOIN documents d ON d.id = c.document_id
            JOIN knowledge_spaces s ON s.id = c.space_id
            WHERE {' AND '.join(kw_conds)}
            ORDER BY kw_score DESC, c.created_at DESC
            LIMIT :top_k
        """
        stmt = text(sql).bindparams(bindparam("top_k", settings.RAG_HYBRID_KEYWORD_TOP_K))
        if space_tags:
            stmt = stmt.bindparams(bindparam("tags", expanding=True))
        kw_rows = (await session.execute(stmt, kw_params)).mappings().all()
        fused = _hybrid_fuse(fused, kw_rows, top_k)
    else:
        # 纯向量路径：保留相似度阈值过滤（兼容旧行为）
        fused = [r for r in fused if r["similarity"] >= threshold][:top_k]

    # 时效性加权：相关性为主，新文档占优（时间意图查询权重更高）
    fused = _apply_recency(fused, _time_intent_weight(query))

    return [
        {
            "chunk_id": row["chunk_id"],
            "content": row["chunk_text"],
            "title": row["title"],
            "document_id": row["document_id"],
            "scene_tag": row["scene_tag"],
            "similarity": row.get("similarity"),
            "created_at": row.get("created_at"),
            "category": row.get("category"),
            "recency": row.get("recency"),
            "score": round(float(row.get("score") or 0.0), 4),
        }
        for row in fused
    ]
