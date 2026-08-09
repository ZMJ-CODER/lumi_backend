"""RAG 服务包 —— 文档解析分块、本地嵌入、知识库管理与向量检索.

模块:
  - document_parser: 纯文本解析与分块
  - embeddings:      本地 bge 模型嵌入（sentence-transformers）
  - knowledge:       知识空间 / 文档管理 / pgvector 检索
"""

from app.services.rag.document_parser import parse_document, parse_file, split_text
from app.services.rag.cleaner import (
    HARD_FAIL_CODES,
    QUALITY_ISSUES,
    DocumentQualityError,
    assess_document,
    clean_document,
    quality_score,
)
from app.services.rag.classifier import CATEGORY_LABELS, classify_document, normalize_category
from app.services.rag.chunker import chunk_document
from app.services.rag.embeddings import embed_query, embed_texts
from app.services.rag.knowledge import (
    create_space,
    delete_document,
    delete_space,
    list_documents,
    list_spaces,
    process_document_pipeline,
    search_public_vectors,
    search_user_knowledge,
    update_space,
    upload_document_file,
)

__all__ = [
    "parse_file",
    "parse_document",
    "clean_document",
    "assess_document",
    "quality_score",
    "DocumentQualityError",
    "QUALITY_ISSUES",
    "HARD_FAIL_CODES",
    "CATEGORY_LABELS",
    "classify_document",
    "normalize_category",
    "chunk_document",
    "split_text",
    "embed_query",
    "embed_texts",
    "create_space",
    "list_spaces",
    "update_space",
    "delete_space",
    "upload_document_file",
    "list_documents",
    "delete_document",
    "process_document_pipeline",
    "search_user_knowledge",
    "search_public_vectors",
]
