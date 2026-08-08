"""知识库服务 —— 知识空间 / 文档 / 分块入库 / 向量检索（pgvector + bge-small-zh-v1.5嵌入）.

数据流:
  上传文档 → 落盘 + documents 表(pending) → Celery 处理管线
           → 解析 → 分块 → 嵌入 → document_chunks 表 → documents.status=ready
  对话检索 → 查询向量 → pgvector 余弦相似度 → 拼接上下文 + 引用列表
"""

import hashlib
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy import bindparam, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.embeddings import embed_query, embed_texts
from app.models.db_models import Document, DocumentChunk, KnowledgeSpace
from app.services.document_parser import parse_file, split_text

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
) -> tuple[Document, Path]:
    """保存上传文件并创建文档记录（status=pending，等待 Celery 处理）."""
    if len(content) > MAX_UPLOAD_SIZE:
        raise ValueError("文件超过 20MB 限制")
    space = await _ensure_space_owned(session, space_id, user_id)

    file_hash = hashlib.sha256(content).hexdigest()
    # 同空间同内容已处理成功 → 直接返回，避免重复入库
    existing = (
        await session.execute(
            select(Document).where(
                Document.space_id == space.id,
                Document.file_hash == file_hash,
                Document.status == "ready",
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing, _doc_file_path(user_id, existing.id, existing.filename)

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
    )
    session.add(doc)
    await session.flush()
    return doc, path


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
            "created_at": d.created_at.isoformat() if d.created_at else None,
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

async def process_document_pipeline(session: AsyncSession, document_id: str, file_path: str) -> int:
    """文档处理管线: 解析 → 分块 → 嵌入 → 入库.

    Returns:
        入库的分块数量
    """
    doc = await session.get(Document, _uuid(document_id))
    if not doc:
        raise LookupError(f"文档不存在: {document_id}")

    try:
        raw_text = parse_file(file_path, doc.filename)
        chunks = split_text(raw_text, settings.RAG_CHUNK_SIZE, settings.RAG_CHUNK_OVERLAP)
        logger.info("📄 文档 {} 分块 {} 个", doc.filename, len(chunks))

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
        await session.commit()
        logger.info("✅ 文档 {} 处理完成，chunks={}", doc.filename, len(chunks))
        return len(chunks)
    except Exception as e:
        await session.rollback()
        failed = await session.get(Document, _uuid(document_id))
        if failed:
            failed.status = "error"
            await session.commit()
        logger.error("❌ 文档 {} 处理失败: {}", document_id, e)
        raise


# ── 向量检索 ─────────────────────────────────────────

async def search_user_knowledge(
    session: AsyncSession,
    user_id: str,
    query: str,
    space_tags: list[str],
    top_k: int | None = None,
    threshold: float | None = None,
) -> tuple[str, list[dict]]:
    """对话 RAG 检索：个人空间 + 公共空间，按场景标签过滤.

    Returns:
        (拼接后的上下文文本, 引用列表)
    """
    top_k = top_k or settings.RAG_TOP_K
    threshold = settings.RAG_SIMILARITY_THRESHOLD if threshold is None else threshold
    if not query or not query.strip():
        return "", []

    vec = await embed_query(query)
    if not vec:
        return "", []
    qvec = _vector_str(vec)

    uid = _uuid(user_id)
    conds: list[str] = ["c.embedding IS NOT NULL"]
    params: dict = {}
    if uid:
        conds.append("(c.user_id = :uid OR s.is_public = true)")
        params["uid"] = uid
    else:
        conds.append("s.is_public = true")
    if space_tags:
        conds.append("s.scene_tag IN :tags")
        params["tags"] = list(space_tags)

    sql = f"""
        SELECT * FROM (
          SELECT c.id AS chunk_id, c.chunk_text, d.filename AS title,
                 d.id AS document_id, s.is_public,
                 1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity
          FROM document_chunks c
          JOIN documents d ON d.id = c.document_id
          JOIN knowledge_spaces s ON s.id = c.space_id
          WHERE {' AND '.join(conds)}
          ORDER BY c.embedding <=> CAST(:qvec AS vector)
          LIMIT :top_k
        ) t
        WHERE t.similarity >= :threshold
        ORDER BY t.similarity DESC
    """
    stmt = text(sql).bindparams(
        bindparam("qvec", qvec),
        bindparam("top_k", top_k),
        bindparam("threshold", threshold),
    )
    if space_tags:
        stmt = stmt.bindparams(bindparam("tags", expanding=True))

    result = await session.execute(stmt, params)
    rows = result.mappings().all()

    context_parts: list[str] = []
    citations: list[dict] = []
    for i, row in enumerate(rows, 1):
        context_parts.append(f"[{i}] {row['chunk_text']}")
        citations.append(
            {
                "type": "public" if row["is_public"] else "personal",
                "title": row["title"],
                "content": row["chunk_text"],
                "source": row["title"],
                "document_id": str(row["document_id"]),
                "similarity": round(float(row["similarity"]), 4),
            }
        )

    if citations:
        logger.info("🔍 RAG 命中 {} 条引用", len(citations))
    return "\n\n".join(context_parts), citations


async def search_public_vectors(
    session: AsyncSession,
    query_vector: list[float],
    space_tags: list[str] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[dict]:
    """公共知识库向量检索（供 /public-kb/search 使用）."""
    top_k = top_k or settings.RAG_TOP_K
    threshold = settings.RAG_SIMILARITY_THRESHOLD if threshold is None else threshold
    if not query_vector:
        return []
    qvec = _vector_str(query_vector)

    conds = ["c.embedding IS NOT NULL", "s.is_public = true"]
    params: dict = {}
    if space_tags:
        conds.append("s.scene_tag IN :tags")
        params["tags"] = list(space_tags)

    sql = f"""
        SELECT * FROM (
          SELECT c.id AS chunk_id, c.chunk_text, d.filename AS title,
                 d.id AS document_id, s.scene_tag,
                 1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity
          FROM document_chunks c
          JOIN documents d ON d.id = c.document_id
          JOIN knowledge_spaces s ON s.id = c.space_id
          WHERE {' AND '.join(conds)}
          ORDER BY c.embedding <=> CAST(:qvec AS vector)
          LIMIT :top_k
        ) t
        WHERE t.similarity >= :threshold
        ORDER BY t.similarity DESC
    """
    stmt = text(sql).bindparams(
        bindparam("qvec", qvec),
        bindparam("top_k", top_k),
        bindparam("threshold", threshold),
    )
    if space_tags:
        stmt = stmt.bindparams(bindparam("tags", expanding=True))

    result = await session.execute(stmt, params)
    return [
        {
            "chunk_id": str(row["chunk_id"]),
            "content": row["chunk_text"],
            "title": row["title"],
            "document_id": str(row["document_id"]),
            "scene_tag": row["scene_tag"],
            "similarity": round(float(row["similarity"]), 4),
        }
        for row in result.mappings().all()
    ]
