"""代码向量服务 —— 本地嵌入（Electron）→ 上传向量 + file_key 混淆元数据.

服务器只存：向量 + file_key（路径哈希）+ 函数名 + 行号 + 摘要；
不存真实路径、不存代码正文（隐私：方案 A）。检索时用同一模型（bge-m3）
嵌入用户问题做语义定位，code agent 据此让客户端按 file_key 读取真实代码。
"""

import uuid

from loguru import logger
from sqlalchemy import bindparam, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db_models import CodeEmbedding, Project
from app.services.rag.embeddings import embed_query, embed_texts


def _uid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _vector_str(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


async def _ensure_owned(session: AsyncSession, user_id: str, project_id: str) -> Project:
    pid = _uid(project_id)
    if pid is None:
        raise ValueError("项目 ID 无效")
    project = await session.get(Project, pid)
    uid = _uid(user_id)
    if not project or project.user_id != uid:
        raise PermissionError("无权操作该项目")
    return project


async def upload_code_embeddings(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    items: list[dict],
    mode: str = "full",
) -> int:
    """全量重建项目代码向量（先删旧再批量插入，幂等）."""
    project = await _ensure_owned(session, user_id, project_id)
    if mode == "full":
        await session.execute(delete(CodeEmbedding).where(CodeEmbedding.project_id == project.id))
    rows = [
        CodeEmbedding(
            project_id=project.id,
            file_key=str(item["file_key"])[:64],
            function_name=(item.get("function_name") or "")[:200] or None,
            line_start=item.get("line_start"),
            line_end=item.get("line_end"),
            summary=(item.get("summary") or "")[:1000] or None,
            embedding=item.get("embedding"),
        )
        for item in items
        if item.get("embedding")
    ]
    if rows:
        session.add_all(rows)
    logger.info(
        "[CodeEmbedding] 代码向量入库 project={} mode={} items={} rows={}",
        str(project.id)[:8],
        mode,
        len(items),
        len(rows),
    )
    return len(rows)


async def upload_code_chunks(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    items: list[dict],
    mode: str = "full",
) -> int:
    """客户端分块文本 → 服务端嵌入 → 存向量 → 文本即用即弃（不落库）.

    客户端不需要嵌入模型；服务端用 bge-m3 统一嵌入，保证查询/文档向量完全一致。
    """
    project = await _ensure_owned(session, user_id, project_id)
    if not project.vector_enabled:
        logger.info("[CodeEmbedding] 项目已关闭向量化，跳过 project={}", str(project.id)[:8])
        return 0  # 涉密项目关闭向量化（仍保留结构索引 + 关键词定位）
    texts = [str(item["text"]) for item in items if item.get("text")]
    if not texts:
        return 0
    logger.info(
        "[CodeEmbedding] 开始嵌入代码块 project={} items={} mode={}",
        str(project.id)[:8],
        len(items),
        mode,
    )
    vectors = await embed_texts(texts)
    if not vectors or len(vectors) != len(items):
        logger.error(
            "[CodeEmbedding] 嵌入模型未就绪/数量不匹配 project={} texts={} vectors={}",
            str(project.id)[:8],
            len(texts),
            len(vectors) if vectors else 0,
        )
        raise RuntimeError("服务端嵌入模型未就绪（bge-m3），请先完成模型部署")
    # full=全量重建（删旧再插）；incremental=增量追加（保留已有向量）
    if mode == "full":
        await session.execute(delete(CodeEmbedding).where(CodeEmbedding.project_id == project.id))
    rows = [
        CodeEmbedding(
            project_id=project.id,
            file_key=str(item["file_key"])[:64],
            function_name=(item.get("function_name") or "")[:200] or None,
            line_start=item.get("line_start"),
            line_end=item.get("line_end"),
            summary=(item.get("summary") or "")[:1000] or None,
            embedding=vectors[i],
        )
        for i, item in enumerate(items)
        if vectors[i]
    ]
    if rows:
        session.add_all(rows)
    logger.info(
        "[CodeEmbedding] 代码块已入库 project={} rows={} dim={}",
        str(project.id)[:8],
        len(rows),
        len(vectors[0]) if vectors else 0,
    )
    return len(rows)


async def set_vector_enabled(
    session: AsyncSession, user_id: str, project_id: str, enabled: bool
) -> bool:
    """项目级向量化开关（涉密项目可关闭，只保留结构索引）."""
    project = await _ensure_owned(session, user_id, project_id)
    project.vector_enabled = bool(enabled)
    if not enabled:
        await session.execute(delete(CodeEmbedding).where(CodeEmbedding.project_id == project.id))
    return project.vector_enabled


async def search_code_vectors(
    session: AsyncSession,
    user_id: str,
    project_id: str,
    query: str,
    top_k: int = 10,
) -> list[dict]:
    """语义检索：bge-m3 嵌入查询 → pgvector 余弦相似度 → 返回 file_key/函数/行号."""
    await _ensure_owned(session, user_id, project_id)
    if not query.strip():
        return []
    vec = await embed_query(query)
    if not vec:
        logger.warning("[CodeEmbedding] 查询嵌入失败（模型未就绪？），跳过语义检索")
        return []
    sql = text(
        """
        SELECT file_key, function_name, line_start, line_end, summary,
               1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
        FROM code_embeddings
        WHERE project_id = :project_id AND embedding IS NOT NULL
        ORDER BY embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
        """
    ).bindparams(
        bindparam("qvec", _vector_str(vec)),
        bindparam("project_id", _uid(project_id)),
        bindparam("top_k", max(1, min(top_k, 50))),
    )
    rows = (await session.execute(sql)).mappings().all()
    return [
        {
            "file_key": r["file_key"],
            "function_name": r["function_name"] or "",
            "line_start": r["line_start"],
            "line_end": r["line_end"],
            "summary": r["summary"] or "",
            "similarity": round(float(r["similarity"]), 4),
        }
        for r in rows
    ]
