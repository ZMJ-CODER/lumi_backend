"""Knowledge 域：文档解析、嵌入、知识空间与向量检索。

这是**应用内一等公民域**，不是可复用包——判据（方案 §三）它一条都不满足：
强依赖 pgvector / Redis / settings / 本地嵌入模型 / LLM，因此不做成 ``packages/``。

目录切分按"数据流"而不是按文件类型堆在一个平层里::

    config                    全局 RAG 配置（Redis 动态覆盖 + .env 兜底）
    parsing/                  文档 → 文本 → 分块 → 分类/质检
    embedding/                文本 → 向量（含代码向量与稀疏向量）
    retrieval/                查询 → 改写 → 召回 → 重排（知识空间与文档管理）
    code/                     本地代码结构抽取与项目索引
    information_resolver      信息源解析（"这个问题该查哪儿"）

**公开面**（``__all__``）与搬迁前的 ``app.knowledge`` 逐字相同，因此
``app/services/rag/__init__.py``（兼容层，P7 删除）可以逐字再导出；新代码直接 import 本包。
"""

from app.knowledge.embedding.embeddings import embed_query, embed_texts
from app.knowledge.parsing.chunker import chunk_document
from app.knowledge.parsing.classifier import CATEGORY_LABELS, classify_document, normalize_category
from app.knowledge.parsing.cleaner import (
    HARD_FAIL_CODES,
    QUALITY_ISSUES,
    DocumentQualityError,
    assess_document,
    clean_document,
    quality_score,
)
from app.knowledge.parsing.document_parser import parse_document, parse_file, split_text
from app.knowledge.retrieval.knowledge import (
    create_space,
    delete_document,
    delete_space,
    list_documents,
    list_spaces,
    process_document_pipeline,
    record_document_enqueue,
    search_public_vectors,
    search_user_knowledge,
    update_space,
    upload_document_file,
)
from app.knowledge.retrieval.query_rewriter import (
    get_retrieval_queries,
    get_retrieval_query,
    rewrite_query,
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
    "get_retrieval_query",
    "get_retrieval_queries",
    "rewrite_query",
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
    "record_document_enqueue",
    "search_user_knowledge",
    "search_public_vectors",
]
