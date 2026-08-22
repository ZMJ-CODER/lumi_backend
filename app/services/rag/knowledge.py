"""RAG 知识库服务 —— 知识空间 / 文档 / 分块入库 / 向量检索（pgvector + bge-m3 嵌入）.

数据流:
  上传文档 → 落盘 + documents 表(pending) → Celery 处理管线
           → 数据清洗→ 解析 → 分块 → 嵌入 → document_chunks 表 → documents.status=ready
  对话检索 → 查询向量 → pgvector 余弦相似度 → 拼接上下文 + 引用列表
"""

import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.rag_config import effective_threshold, effective_top_k
from app.services.rag.chunker import chunk_document
from app.services.rag.classifier import classify_document, normalize_category
from app.services.rag.embeddings import embed_query, embed_texts
from app.models.db_models import Document, DocumentChunk, KnowledgeSpace
from app.services.rag.cleaner import HARD_FAIL_CODES, DocumentQualityError, assess_document, clean_document
from app.services.rag.document_parser import parse_document_with_metadata

MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 MB

# 编程语言扩展名：入库时直接归类为 code（聊天/办公 RAG 检索会排除，代码检索走 code 索引）
CODE_FILE_EXTS = {
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".go", ".java", ".c", ".h",
    ".cpp", ".hpp", ".cs", ".rs", ".php", ".rb", ".sh", ".bash", ".sql", ".kt", ".kts",
    ".swift", ".dart", ".scala", ".lua", ".pl", ".r", ".vue", ".svelte",
}


def is_code_filename(filename: str | None) -> bool:
    """按文件名判断是否为代码文件（用于归类 code，避免代码污染普通聊天检索）."""
    name = (filename or "").lower()
    return Path(name).suffix in CODE_FILE_EXTS


def _chunk_metadata(
    filename: str,
    chunk_text: str,
    chunk_index: int,
    locator: dict | None = None,
) -> str:
    """Persist only verifiable, parser-derived locator data for citations.

    Page and table-cell coordinates are deliberately absent until the parser
    returns them.  Guessing those locations would make citations look precise
    while being wrong.  The existing JSON column keeps this forward compatible
    with richer Docling output later.
    """
    ext = Path(filename or "").suffix.lower()
    lines = [line.strip() for line in chunk_text.splitlines() if line.strip()]
    headings = [line.lstrip("#").strip() for line in lines if line.startswith("#")]
    data: dict[str, object] = {
        "source": Path(filename or "").name,
        "doc_title": Path(filename or "").name,
        "file_extension": ext,
        "chunk_index": chunk_index,
    }
    if headings:
        data["heading_path"] = " > ".join(headings[:6])[:500]
    if ext in {".csv", ".tsv"} and len(lines) > 1:
        # The chunker repeats the header for every row group.  This is a local
        # range inside the chunk, not a fabricated source-file row number.
        data["table_rows_in_chunk"] = max(0, len(lines) - 1)
    for key in ("page_start", "page_end", "heading_path", "table_id", "row_range"):
        value = (locator or {}).get(key)
        if value is not None and value != "":
            data[key] = value
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _read_chunk_metadata(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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
        queued_at=datetime.now(timezone.utc),
        chunk_count=0,
        category=normalize_category(category) or settings.RAG_DEFAULT_CATEGORY,
    )
    session.add(doc)
    await session.flush()
    return doc, path, True


async def record_document_enqueue(
    session: AsyncSession, document_id: str, celery_task_id: str | None
) -> None:
    """Persist the broker task reference after the document transaction commits."""
    doc = await session.get(Document, _uuid(document_id), with_for_update=True)
    if not doc or doc.status not in {"pending", "processing"}:
        return
    doc.celery_task_id = celery_task_id or None
    doc.queued_at = datetime.now(timezone.utc)
    await session.commit()


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
    celery_task_id: str | None = None,
) -> int:
    """文档处理管线: 解析 → 清洗 → 质量门 → 分类 → 分块 → 嵌入 → 入库.

    Returns:
        入库的分块数量
    """
    doc = await session.get(Document, _uuid(document_id), with_for_update=True)
    if not doc:
        raise LookupError(f"文档不存在: {document_id}")

    # The broker provides at-least-once delivery.  Claim the document in the
    # database before doing CPU/LLM work so a redelivered task cannot run the
    # pipeline concurrently.  A worker dying after this commit is recovered by
    # the scheduled stale-document watchdog.
    if doc.status == "ready":
        return int(doc.chunk_count or 0)
    # Redis redelivery keeps Celery's task id. With a visibility timeout above
    # the hard limit, the same id may reclaim work after a former worker died;
    # a different id still belongs to another worker and must not be raced.
    if doc.status == "processing":
        if not celery_task_id or doc.celery_task_id != celery_task_id:
            logger.info("文档任务已被其他 worker 领取，跳过重复执行: {}", document_id)
            return int(doc.chunk_count or 0)
    doc.status = "processing"
    doc.celery_task_id = celery_task_id or doc.celery_task_id
    doc.processing_started_at = datetime.now(timezone.utc)
    doc.attempt_count = int(doc.attempt_count or 0) + 1
    doc.error_message = None
    await session.commit()

    try:
        try:
            parsed = parse_document_with_metadata(file_path, doc.filename)
            raw_text = parsed.text
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
        # 代码文件直接归类 code（不进 LLM 分类；聊天/办公 RAG 检索会排除 code）
        if is_code_filename(doc.filename):
            doc.category = "code"
            doc.tags = None
        else:
            # 文档分类：时效档次 + 开放标签（大模型抽样判断，用户选择的档次作为参考）
            category, tags = await classify_document(cleaned_text, user_category or doc.category)
            doc.category = category
            doc.tags = ", ".join(tags) if tags else None
        # 按解析器 segment 分块，页码等 provenance 随 chunk 一起进入 metadata。
        # 纯文本只有一个无页码 segment，行为与旧版完全一致。
        chunk_records: list[tuple[str, dict]] = []
        segments = parsed.segments if parsed.segments else []
        for segment in segments:
            segment_text = clean_document(segment.text, doc.filename).strip()
            if not segment_text:
                continue
            for chunk_text in chunk_document(
                segment_text, doc.filename, settings.RAG_CHUNK_SIZE, settings.RAG_CHUNK_OVERLAP
            ):
                locator = {
                    "page_start": segment.page_start,
                    "page_end": segment.page_end,
                    "heading_path": segment.heading_path,
                }
                chunk_records.append((chunk_text, locator))
        chunks = [text for text, _ in chunk_records]

        # 重处理时清掉旧分块
        await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))

        if chunks:
            embeddings = await embed_texts(chunks)
            if len(embeddings) != len(chunks):
                raise RuntimeError("嵌入数量与分块数量不一致")
            for i, (chunk_text, vec) in enumerate(zip(chunks, embeddings, strict=False)):
                session.add(
                    DocumentChunk(
                        document_id=doc.id,
                        space_id=doc.space_id,
                        user_id=doc.user_id,
                        chunk_index=i,
                        chunk_text=chunk_text,
                        embedding=vec,
                        metadata_=_chunk_metadata(doc.filename, chunk_text, i, chunk_records[i][1]),
                    )
                )

        doc.status = "ready"
        doc.chunk_count = len(chunks)
        doc.error_message = None
        doc.processing_started_at = None
        await session.commit()
        logger.debug("✅ 文档 {} 处理完成，chunks={}", doc.filename, len(chunks))
        return len(chunks)
    except Exception as e:
        await session.rollback()
        failed = await session.get(Document, _uuid(document_id))
        if failed:
            failed.status = "error"
            failed.error_message = str(e)[:500]
            failed.processing_started_at = None
            await session.commit()
        logger.error("❌ 文档 {} 处理失败: {}", document_id, e)
        raise


async def mark_document_retryable(session: AsyncSession, document_id: str) -> None:
    """Return a transiently failed document to the durable queue state."""
    doc = await session.get(Document, _uuid(document_id), with_for_update=True)
    if not doc or doc.status == "ready":
        return
    doc.status = "pending"
    doc.processing_started_at = None
    doc.queued_at = datetime.now(timezone.utc)
    await session.commit()


async def recover_stale_document_jobs(session: AsyncSession, stale_after_seconds: int) -> list[dict]:
    """Release documents whose worker likely died before acknowledging.

    This intentionally performs no broker operation inside the transaction.
    The caller commits the state change first, then publishes fresh tasks.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(1, stale_after_seconds))
    rows = (
        await session.execute(
            select(Document)
            .where(
                (
                    (Document.status == "processing")
                    & (Document.processing_started_at.is_not(None))
                    & (Document.processing_started_at < cutoff)
                )
                | (
                    (Document.status == "pending")
                    & (Document.queued_at.is_not(None))
                    & (Document.queued_at < cutoff)
                )
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    recovered: list[dict] = []
    now = datetime.now(timezone.utc)
    for doc in rows:
        doc.status = "pending"
        doc.processing_started_at = None
        doc.queued_at = now
        doc.celery_task_id = None
        doc.error_message = "任务执行超时，已自动重新入队"
        recovered.append(
            {
                "document_id": str(doc.id),
                "file_path": str(_doc_file_path(str(doc.user_id), doc.id, doc.filename)),
                "user_id": str(doc.user_id),
                "space_id": str(doc.space_id),
                "category": doc.category,
            }
        )
    if recovered:
        await session.commit()
    return recovered


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
                "chunk_metadata": row.get("chunk_metadata"),
                "title": row["title"],
                "document_id": str(row["document_id"]),
                "is_public": bool(row.get("is_public", True)),
                "created_at": row.get("created_at"),
                "category": row.get("category"),
                "similarity": round(float(row["similarity"]), 4),
                "score": 0.0,
                "kw_hit": False,
            },
        )
        entry["score"] += 1.0 / (rrf_k + rank)
    for rank, row in enumerate(keyword_rows, 1):
        cid = str(row["chunk_id"])
        entry = merged.get(cid)
        if entry:
            entry["kw_hit"] = True
            entry["score"] += 1.0 / (rrf_k + rank)
        else:
            merged[cid] = {
                "chunk_id": cid,
                "chunk_text": row["chunk_text"],
                "chunk_metadata": row.get("chunk_metadata"),
                "title": row["title"],
                "document_id": str(row["document_id"]),
                "is_public": bool(row.get("is_public", True)),
                "created_at": row.get("created_at"),
                "category": row.get("category"),
                "similarity": None,
                "score": 1.0 / (rrf_k + rank),
                "kw_hit": True,
            }
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:top_k]


def _multi_query_fuse(result_sets: list[list[dict]], top_k: int) -> list[dict]:
    """对原 query 与扩写 query 的候选做第二层 RRF。

    原 query 排名作为第一路，扩写仅提供补充候选；相同 chunk 不重复，且保留原有
    keyword/similarity/metadata 字段供后续门槛、citation 和 reranker 使用。
    """
    merged: dict[str, dict] = {}
    rrf_k = 60
    for result_set in result_sets:
        for rank, row in enumerate(result_set, 1):
            chunk_id = str(row["chunk_id"])
            entry = merged.get(chunk_id)
            if entry is None:
                entry = dict(row)
                entry["score"] = 0.0
                merged[chunk_id] = entry
            entry["score"] += 1.0 / (rrf_k + rank)
            entry["kw_hit"] = bool(entry.get("kw_hit") or row.get("kw_hit"))
            if entry.get("similarity") is None and row.get("similarity") is not None:
                entry["similarity"] = row["similarity"]
    return sorted(merged.values(), key=lambda row: row["score"], reverse=True)[:top_k]


def _passes_similarity_gate(row: dict, threshold: float) -> bool:
    """相关性硬门槛：关键词精确命中（kw_hit）或关键词路独有（无向量相似度）放行；
    向量路相似度低于阈值的引用丢弃."""
    if row.get("kw_hit"):
        return True
    if row.get("similarity") is None:
        return True
    return float(row.get("similarity") or 0.0) >= threshold


def _scope_conditions(
    user_id: str,
    space_tags: list[str],
    need_embedding: bool,
    exclude_categories: list[str] | None = None,
    own_space_override: bool = True,
) -> tuple[list[str], dict]:
    """构造检索范围条件（个人空间 + 公共空间，按场景标签过滤；可排除类别如 code）.

    个人检索规则：用户自己的空间（含办公文档会话 officedoc_* 空间）始终可检索，
    场景标签只约束公共空间 —— 否则用户在办公模式上传的文档在聊天场景会完全检索不到。
    own_space_override=False 时恢复"标签精确匹配"（文档分析等场景只搜目标空间，不混入其他文档）。
    """
    conds: list[str] = []
    params: dict = {}
    if need_embedding:
        conds.append("c.embedding IS NOT NULL")
    uid = _uuid(user_id)
    if uid:
        if settings.RAG_INCLUDE_PUBLIC_IN_PERSONAL:
            # 个人检索包含公共空间（共享知识库场景；默认关闭，避免他人上传内容混入）
            conds.append("(c.user_id = :uid OR s.is_public = true)")
        else:
            conds.append("c.user_id = :uid")
        params["uid"] = uid
    else:
        conds.append("s.is_public = true")
    if space_tags:
        if uid and own_space_override:
            # 自己上传的空间（无论标签）始终参与检索；标签过滤仅用于公共空间
            conds.append("(s.is_public = false OR s.scene_tag IN :tags)")
        else:
            conds.append("s.scene_tag IN :tags")
        params["tags"] = list(space_tags)
    if exclude_categories:
        conds.append("d.category NOT IN :excl_cats")
        params["excl_cats"] = list(exclude_categories)
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
    exclude_categories: list[str] | None = None,
    own_space_override: bool = True,
    rerank_enabled: bool = False,
    query_variants: list[str] | None = None,
) -> tuple[str, list[dict]]:
    """对话 RAG 混合检索：向量相似度 top-N + 关键词检索，RRF 融合.

    Returns:
        (拼接后的上下文文本, 引用列表)
    """
    top_k = await effective_top_k(top_k or settings.RAG_TOP_K)
    threshold = await effective_threshold(
        settings.RAG_SIMILARITY_THRESHOLD if threshold is None else threshold
    )
    primary_query = query.strip()
    variants = [primary_query]
    for variant in query_variants or []:
        value = str(variant or "").strip()
        if value and value.casefold() not in {item.casefold() for item in variants}:
            variants.append(value)
    variants = variants[: settings.RAG_QUERY_REWRITE_MAX_VARIANTS]
    if not primary_query:
        return "", []

    conds, params = _scope_conditions(
        user_id,
        space_tags,
        need_embedding=True,
        exclude_categories=exclude_categories,
        own_space_override=own_space_override,
    )
    candidate_k = max(top_k, settings.RAG_RERANK_TOP_K) if rerank_enabled and settings.RAG_RERANK_ENABLED else top_k
    vec_top = max(settings.RAG_HYBRID_VECTOR_TOP_K, candidate_k)
    kw_top = max(settings.RAG_HYBRID_KEYWORD_TOP_K, candidate_k)
    all_fused: list[list[dict]] = []

    for retrieval_query in variants:
        vector_rows: list[dict] = []
        # ── 第一路：向量相似度 top-N ──
        vec = await embed_query(retrieval_query)
        if vec:
            sql = f"""
            SELECT c.id AS chunk_id, c.chunk_text, c.metadata AS chunk_metadata, d.filename AS title,
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
            if exclude_categories:
                stmt = stmt.bindparams(bindparam("excl_cats", expanding=True))
            vector_rows = [dict(row) for row in (await session.execute(stmt, params)).mappings().all()]

        # ── 第二路：关键词检索 top-N ──
        keyword_rows: list[dict] = []
        keywords = _extract_keywords(retrieval_query)
        if keywords:
            kw_conds, kw_params = _scope_conditions(
                user_id,
                space_tags,
                need_embedding=False,
                exclude_categories=exclude_categories,
                own_space_override=own_space_override,
            )
            kw_conds.append(
                "(" + " OR ".join(f"c.chunk_text ILIKE :kw{i}" for i in range(len(keywords))) + ")"
            )
            match_expr = " + ".join(
                f"(CASE WHEN c.chunk_text ILIKE :kw{i} THEN 1 ELSE 0 END)" for i in range(len(keywords))
            )
            kw_params.update({f"kw{i}": f"%{kw}%" for i, kw in enumerate(keywords)})
            sql = f"""
            SELECT c.id AS chunk_id, c.chunk_text, c.metadata AS chunk_metadata, d.filename AS title,
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
            if exclude_categories:
                stmt = stmt.bindparams(bindparam("excl_cats", expanding=True))
            keyword_rows = [dict(row) for row in (await session.execute(stmt, kw_params)).mappings().all()]
        if keyword_rows:
            all_fused.append(_hybrid_fuse(vector_rows, keyword_rows, candidate_k))
        elif vector_rows:
            all_fused.append(vector_rows[:candidate_k])

    fused = _multi_query_fuse(all_fused, candidate_k) if len(all_fused) > 1 else (all_fused[0] if all_fused else [])

    # cross-encoder 只重排混合候选，不参与快速路径；失败自动回退 RRF。
    if rerank_enabled and settings.RAG_RERANK_ENABLED and fused:
        from app.services.rag.reranker import rerank

        fused = rerank(primary_query, fused, min(settings.RAG_RERANK_FINAL_K, top_k))
    # 时效性加权：相关性为主，新文档占优（时间意图查询权重更高）
    fused = _apply_recency(fused, _time_intent_weight(primary_query))

    # 相关性硬门槛（宁缺毋滥）：向量路相似度低于阈值的引用完全不可信，直接丢弃；
    # 关键词路精确命中（子串 ILIKE 匹配）是强证据，无论其向量相似度高低都保留。
    fused = [r for r in fused if _passes_similarity_gate(r, threshold)]

    context_parts: list[str] = []
    citations: list[dict] = []
    for i, row in enumerate(fused, 1):
        metadata = _read_chunk_metadata(row.get("chunk_metadata"))
        locator = str(metadata.get("heading_path") or "").strip()
        prefix = f"[{i}] {row['title']}" + (f" | {locator}" if locator else "")
        context_parts.append(f"{prefix}\n{row['chunk_text']}")
        created_at = row.get("created_at")
        citations.append(
            {
                "type": "public" if row["is_public"] else "personal",
                "title": row["title"],
                "content": row["chunk_text"],
                "source": row["title"],
                "document_id": row["document_id"],
                "similarity": row.get("similarity"),
                # datetime 必须转字符串：SSE 事件 json.dumps 无法序列化 datetime，
                # 否则整条流式响应在 done 事件处抛错（引用永远传不到前端）
                "created_at": created_at.isoformat() if created_at else None,
                "category": row.get("category"),
                "recency": row.get("recency"),
                "score": round(float(row.get("score") or 0.0), 4),
                "locator": metadata,
            }
        )

    if citations:
        logger.debug("🔍 RAG 命中 {} 条引用", len(citations))
    try:
        from app.core.observability import inc_rag_search

        inc_rag_search(len(citations))
    except Exception:  # noqa: BLE001
        pass
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
    top_k = await effective_top_k(top_k or settings.RAG_TOP_K)
    threshold = await effective_threshold(
        settings.RAG_SIMILARITY_THRESHOLD if threshold is None else threshold
    )
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

    # 相关性硬门槛（宁缺毋滥）：向量路相似度低于阈值的引用完全不可信，直接丢弃；
    # 关键词路精确命中（子串 ILIKE 匹配）是强证据，无论其向量相似度高低都保留。
    fused = [r for r in fused if _passes_similarity_gate(r, threshold)]

    return [
        {
            "chunk_id": row["chunk_id"],
            "content": row["chunk_text"],
            "title": row["title"],
            "document_id": row["document_id"],
            "scene_tag": row["scene_tag"],
            "similarity": row.get("similarity"),
            "created_at": row.get("created_at").isoformat() if row.get("created_at") else None,
            "category": row.get("category"),
            "recency": row.get("recency"),
            "score": round(float(row.get("score") or 0.0), 4),
        }
        for row in fused
    ]
