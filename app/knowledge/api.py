"""Knowledge 域 · **对外公开接口**（P4 收尾）。

跨域依赖只能走这里（门禁规则 2/3 + ``DOMAIN_PUBLIC["knowledge"] = ("app.knowledge.api",)``）。
"公开"意味着：**这些名字的签名与语义是本域对其他域的承诺**，改名/改语义要按跨域契约处理；
域内实现（``retrieval.knowledge`` 的其余函数、``parsing`` 的内部件、``long_term`` 之类）
一律不是公开面——直接 import 它们会被门禁拦下。

为什么要有这个文件而不是"谁用谁直接 import 深层模块"：**边界要能被执行**。
`app.knowledge.api` 是一处**可审计的清单**：本域对外承诺了什么，一眼可见；
新增跨域依赖时，作者必须先把名字加到这里（也就是先把"这是公开契约"这件事想清楚）。

本模块是**同包聚合**（不是跨包转发壳）：它只 re-export 本包内的名字，因此不受规则 4 约束。
"""

from __future__ import annotations

# ── 文档入库管线（办公域把产物登记进知识库时用）────────────────
from app.knowledge.retrieval.knowledge import (
    create_space,
    process_document_pipeline,
    record_document_enqueue,
    search_user_knowledge,
    upload_document_file,
)
from app.knowledge.retrieval.query_rewriter import get_retrieval_queries

# ── 解析（办公域把文档正文交给知识域解析时用）──────────────────
from app.knowledge.parsing.document_parser import parse_document

# ── 嵌入（模型调用接口）────────────────────────────────────────
from app.knowledge.embedding.embeddings import (
    embed_query,
    embed_texts,
    embedding_model_loaded,
)

# ── 代码索引（本地代码的结构与项目索引；模块对象原样发布，调用方按模块用）──
from app.knowledge.code import code_structure, project_index
from app.knowledge.embedding import code_embedding

# ── 代码结构纯函数（工作区读取的骨架扫描用）────────────────────
from app.knowledge.code.code_structure import (
    find_symbol,
    render_skeleton_lines,
    scan_text,
    slice_lines,
)

# ── 信息源解析（纯决策：该查哪儿、要不要完整读）────────────────
from app.knowledge.information_resolver import (
    InformationResolver,
    requires_complete_read,
    smart_slice,
)

__all__ = [
    "InformationResolver",
    "code_embedding",
    "code_structure",
    "create_space",
    "embed_query",
    "embed_texts",
    "embedding_model_loaded",
    "find_symbol",
    "get_retrieval_queries",
    "parse_document",
    "process_document_pipeline",
    "project_index",
    "record_document_enqueue",
    "render_skeleton_lines",
    "requires_complete_read",
    "scan_text",
    "search_user_knowledge",
    "slice_lines",
    "smart_slice",
    "upload_document_file",
]
