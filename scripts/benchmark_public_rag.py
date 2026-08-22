"""公共 RAG 检索基准：SciFact (BEIR) + CRUD-RAG。

示例：
    python scripts/benchmark_public_rag.py --dataset scifact --data-dir data/eval/scifact
    python scripts/benchmark_public_rag.py --dataset crud-rag --max-queries 200

该脚本只做离线内存评测，不写 Lumi 数据库、不改变线上 RAG 配置。
SciFact 使用 BEIR qrels。CRUD-RAG 官方仓库是生成式 RAG 基准，并没有与其 8 万新闻
库直接对齐的检索 qrels；本脚本的本地模式从官方 ``1doc_QA.json`` 构造闭集检索代理：
问题为 query，对应 ``news1`` 为 gold 文档。该模式用于补充中文趋势，不等同于全库检索评测。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
import zipfile

import numpy as np


def _metrics(ranks: list[int | None], total: int) -> dict[str, float | int]:
    values = [rank for rank in ranks if rank is not None]
    return {
        "queries": total,
        "hit@1": round(sum(rank <= 1 for rank in values) / max(1, total), 4),
        "hit@5": round(sum(rank <= 5 for rank in values) / max(1, total), 4),
        "recall@10": round(sum(rank <= 10 for rank in values) / max(1, total), 4),
        "mrr": round(sum(1.0 / rank for rank in values) / max(1, total), 4),
    }


def _rank_scores(scores: np.ndarray, doc_ids: list[str], relevant: set[str], top_k: int) -> int | None:
    order = np.argsort(-scores)[:top_k]
    for rank, index in enumerate(order, 1):
        if doc_ids[int(index)] in relevant:
            return rank
    return None


def _top_ranked_doc_ids(
    scores: np.ndarray,
    doc_ids: list[str],
    depth: int,
    *,
    positive_only: bool = False,
) -> list[str]:
    """返回一个召回通道的候选列表，供 RRF 融合使用。

    稀疏/词法通道的零分文档不是真实召回命中，不能因为排序稳定性而进入
    RRF 候选池；dense 余弦分数则保留完整排序。
    """
    order = np.argsort(-scores)
    if positive_only:
        order = order[scores[order] > 0]
    return [doc_ids[int(index)] for index in order[:depth]]


def _rrf_scores(*rankings: list[str], rrf_k: int) -> dict[str, float]:
    """Reciprocal Rank Fusion；只融合每路真实返回的候选。"""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, 1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
    return fused


def _rank_fused(scores: dict[str, float], relevant: set[str], top_k: int) -> int | None:
    for rank, (doc_id, _) in enumerate(
        sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k],
        1,
    ):
        if doc_id in relevant:
            return rank
    return None


def _dense_encode(model: Any, texts: list[str], batch_size: int) -> np.ndarray:
    return np.asarray(
        model.encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=True),
        dtype=np.float32,
    )


def _sparse_encode(model: Any, texts: list[str]) -> list[dict[str, float]]:
    output = model.encode(texts, return_dense=False, return_sparse=True, return_colbert_vecs=False)
    weights = output.get("lexical_weights") if isinstance(output, dict) else None
    if weights is None:
        raise RuntimeError("FlagEmbedding 未返回 lexical_weights，不能进行 sparse 评测")
    return [{str(key): float(value) for key, value in (item or {}).items()} for item in weights]


def _sparse_scores(query: dict[str, float], docs: list[dict[str, float]]) -> np.ndarray:
    return np.asarray(
        [sum(float(weight) * float(item.get(key, 0.0)) for key, weight in query.items()) for item in docs],
        dtype=np.float32,
    )


def _load_scifact(data_dir: Path):
    from beir.datasets.data_loader import GenericDataLoader

    corpus, queries, qrels = GenericDataLoader(str(data_dir)).load(split="test")
    doc_ids = list(corpus)
    doc_texts = [f"{corpus[doc_id].get('title', '')}\n{corpus[doc_id].get('text', '')}" for doc_id in doc_ids]
    examples = [(str(query_id), query, {str(doc_id) for doc_id, score in qrels.get(query_id, {}).items() if score > 0})
                for query_id, query in queries.items()]
    return doc_ids, doc_texts, examples


def _first(row: dict, names: list[str], default=None):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _load_crud(args):
    from datasets import load_dataset

    dataset = load_dataset("IAAR-Shanghai/CRUD-RAG", split=args.split)
    rows = [dict(row) for row in dataset]
    query_field = args.query_field
    doc_field = args.doc_field
    relevant_field = args.relevant_field
    if not rows:
        raise RuntimeError("CRUD-RAG 数据集为空")
    query_field = query_field or next((key for key in ("query", "question", "instruction") if key in rows[0]), None)
    doc_field = doc_field or next((key for key in ("documents", "contexts", "corpus", "passages") if key in rows[0]), None)
    relevant_field = relevant_field or next((key for key in ("relevant", "positive", "answers", "gold") if key in rows[0]), None)
    if not query_field or not doc_field:
        raise RuntimeError(f"无法识别 CRUD-RAG 字段，请用参数指定。字段={sorted(rows[0])}")
    docs: dict[str, str] = {}
    examples: list[tuple[str, str, set[str]]] = []
    for index, row in enumerate(rows):
        query = str(row[query_field])
        raw_docs = row[doc_field] if isinstance(row[doc_field], list) else [row[doc_field]]
        ids = []
        for inner, value in enumerate(raw_docs):
            if isinstance(value, dict):
                text = str(_first(value, ["text", "content", "document"], ""))
                doc_id = str(_first(value, ["id", "doc_id", "index"], f"{index}-{inner}"))
            else:
                text, doc_id = str(value), f"{index}-{inner}"
            docs.setdefault(doc_id, text)
            ids.append(doc_id)
        relevant = row.get(relevant_field) if relevant_field else None
        relevant_ids = {str(item) for item in relevant} if isinstance(relevant, list) else set(ids if relevant is None else [str(relevant)])
        examples.append((str(index), query, relevant_ids))
    doc_ids = list(docs)
    return doc_ids, [docs[doc_id] for doc_id in doc_ids], examples


def _load_local_crud(args):
    """从 CRUD_RAG 官方仓库构建有明确 qrels 的闭集中文检索代理。

    官方 ``80000_docs`` 与 QA 样本 ``news1`` 并非同一正文，不能伪造全库 qrels。这里
    只使用 1doc QA：每条问题对应一个官方给出的支撑新闻；相同正文合并为同一文档，避免
    重复文档在排序里制造无意义的并列项。
    """
    root = Path(args.crud_path)
    archive = root / "data" / "crud" / "CRUD_Data.zip"
    if not archive.is_file():
        raise RuntimeError(f"未找到 CRUD-RAG 数据包：{archive}")
    with zipfile.ZipFile(archive) as zf:
        try:
            rows = json.loads(zf.read("1doc_QA.json"))
        except KeyError as exc:
            raise RuntimeError("CRUD_Data.zip 缺少 1doc_QA.json") from exc
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("CRUD-RAG 1doc_QA.json 为空或格式错误")

    text_to_id: dict[str, str] = {}
    doc_texts: list[str] = []
    examples: list[tuple[str, str, set[str]]] = []
    for index, row in enumerate(rows):
        query = str(row.get("questions", "")).strip()
        document = str(row.get("news1", "")).strip()
        if not query or not document:
            continue
        # 完全相同的正文在闭集中共享一个 canonical ID。
        doc_id = text_to_id.get(document)
        if doc_id is None:
            doc_id = f"crud-1doc-{len(text_to_id)}"
            text_to_id[document] = doc_id
            doc_texts.append(document)
        examples.append((str(row.get("id", index)) + f"-{index}", query, {doc_id}))
    if not examples:
        raise RuntimeError("CRUD-RAG 中没有可用的 question/news1 对")
    return list(text_to_id.values()), doc_texts, examples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=("scifact", "crud-rag"), required=True)
    parser.add_argument("--data-dir", type=Path, default=Path("data/eval/scifact"))
    parser.add_argument("--split", default="test")
    parser.add_argument(
        "--crud-path",
        type=Path,
        help="本地 CRUD_RAG 仓库根目录；指定后不访问 Hugging Face，使用官方 1doc_QA 闭集代理",
    )
    parser.add_argument("--model", default="BAAI/bge-m3")
    parser.add_argument("--sparse-model", default="BAAI/bge-m3")
    parser.add_argument(
        "--rerank-model",
        help="可选 cross-encoder 重排序模型；开启后对 RRF 候选做离线重排",
    )
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="编码设备；auto 在 CUDA 可用时使用 cuda，否则使用 cpu",
    )
    parser.add_argument(
        "--use-system-ca",
        action="store_true",
        help="使用操作系统证书库访问 Hugging Face（适用于企业代理/本机证书链未写入 certifi 的环境）",
    )
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument(
        "--rrf-candidate-depth",
        type=int,
        default=10,
        help="每一路放入 RRF 的候选数量（默认 10，与线上检索 Top 10 一致）",
    )
    parser.add_argument(
        "--rrf-k",
        type=int,
        default=60,
        help="RRF rank constant（默认 60）",
    )
    parser.add_argument(
        "--rerank-device",
        choices=("auto", "cpu", "cuda"),
        default="auto",
        help="重排序设备；默认跟随 CUDA 可用性",
    )
    parser.add_argument(
        "--rerank-batch-size",
        type=int,
        default=32,
        help="重排序 cross-encoder 批大小",
    )
    parser.add_argument(
        "--rerank-max-chars",
        type=int,
        default=500,
        help="每个重排序候选最多保留的字符数；模拟线上 chunk，而非对整篇原文重排",
    )
    parser.add_argument(
        "--skip-sparse",
        action="store_true",
        help="跳过 FlagEmbedding sparse 对照；适用于只验证 dense + rerank 的场景",
    )
    parser.add_argument("--max-docs", type=int, default=0)
    parser.add_argument("--max-queries", type=int, default=0)
    parser.add_argument("--query-field")
    parser.add_argument("--doc-field")
    parser.add_argument("--relevant-field")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.use_system_ca:
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError as exc:
            raise RuntimeError(
                "--use-system-ca 需要 truststore；请执行 uv pip install --python .\\.venv\\Scripts\\python.exe truststore"
            ) from exc

    if args.device == "auto":
        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    else:
        device = args.device
    if device == "cuda":
        try:
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("请求使用 CUDA，但当前 PyTorch 未检测到可用 CUDA")
        except ImportError as exc:
            raise RuntimeError("请求使用 CUDA，但未安装 torch") from exc
    print(f"device={device}")

    if args.dataset == "scifact":
        doc_ids, doc_texts, examples = _load_scifact(args.data_dir)
    elif args.crud_path:
        doc_ids, doc_texts, examples = _load_local_crud(args)
    else:
        doc_ids, doc_texts, examples = _load_crud(args)
    if args.max_docs:
        doc_ids, doc_texts = doc_ids[: args.max_docs], doc_texts[: args.max_docs]
    if args.max_queries:
        examples = examples[: args.max_queries]
    if args.rrf_candidate_depth < 10:
        raise ValueError("--rrf-candidate-depth 至少应为 10")
    if args.rrf_k < 1:
        raise ValueError("--rrf-k 必须大于 0")

    rerank_device = device if args.rerank_device == "auto" else args.rerank_device
    if rerank_device == "cuda":
        try:
            import torch

            if not torch.cuda.is_available():
                raise RuntimeError("请求重排序使用 CUDA，但当前 PyTorch 未检测到可用 CUDA")
        except ImportError as exc:
            raise RuntimeError("请求重排序使用 CUDA，但未安装 torch") from exc

    from sentence_transformers import SentenceTransformer

    dense_model = SentenceTransformer(args.model, device=device, local_files_only=Path(args.model).exists())
    doc_dense = _dense_encode(dense_model, doc_texts, args.batch_size)
    query_dense = _dense_encode(dense_model, [item[1] for item in examples], args.batch_size)
    dense_scores = query_dense @ doc_dense.T
    dense_ranks = [_rank_scores(dense_scores[i], doc_ids, item[2], 10) for i, item in enumerate(examples)]

    # lexical overlap 是可复现的关键词代理基线；线上为 pg_trgm，二者不能视为同一实现。
    lexical_scores = []
    for _, query, relevant in examples:
        terms = set(query.lower().split())
        scores = np.asarray([sum(text.lower().count(term) for term in terms) for text in doc_texts], dtype=np.float32)
        lexical_scores.append(scores)
    lexical_ranks = [_rank_scores(scores, doc_ids, item[2], 10) for scores, item in zip(lexical_scores, examples, strict=True)]

    dense_lexical_rrf_ranks = []
    for index, (_, _, relevant) in enumerate(examples):
        dense_candidates = _top_ranked_doc_ids(dense_scores[index], doc_ids, args.rrf_candidate_depth)
        lexical_candidates = _top_ranked_doc_ids(
            lexical_scores[index], doc_ids, args.rrf_candidate_depth, positive_only=True
        )
        dense_lexical_rrf_ranks.append(
            _rank_fused(_rrf_scores(dense_candidates, lexical_candidates, rrf_k=args.rrf_k), relevant, 10)
        )

    result = {
        "dataset": args.dataset,
        "evaluation_mode": "crud_1doc_closed_set_proxy" if args.dataset == "crud-rag" and args.crud_path else "qrels",
        "documents": len(doc_ids),
        "queries": len(examples),
        "rrf": {"candidate_depth": args.rrf_candidate_depth, "k": args.rrf_k},
        "dense": _metrics(dense_ranks, len(examples)),
        "lexical_proxy": _metrics(lexical_ranks, len(examples)),
        "dense_lexical_proxy_rrf": _metrics(dense_lexical_rrf_ranks, len(examples)),
    }
    reranker = None
    if args.rerank_model:
        from sentence_transformers import CrossEncoder

        reranker = CrossEncoder(
            args.rerank_model,
            device=rerank_device,
            local_files_only=Path(args.rerank_model).exists(),
        )

    if reranker is not None:
        # 建索引避免每个 query 在 doc_ids 中线性查找。
        doc_index = {doc_id: index for index, doc_id in enumerate(doc_ids)}

        def rerank_fused_batch(fused_rows: list[dict[str, float]]) -> list[list[str]]:
            """将所有 query 的候选一次拼成 pair 批量预测，避免逐 query 调模型。"""
            candidates_per_query = [
                [doc_id for doc_id, _ in sorted(fused.items(), key=lambda item: (-item[1], item[0]))]
                for fused in fused_rows
            ]
            pairs = [
                (examples[query_index][1], doc_texts[doc_index[doc_id]][: args.rerank_max_chars])
                for query_index, candidates in enumerate(candidates_per_query)
                for doc_id in candidates
            ]
            scores = iter(reranker.predict(pairs, batch_size=args.rerank_batch_size, show_progress_bar=True))
            ranked_per_query: list[list[str]] = []
            for candidates in candidates_per_query:
                scored = [(doc_id, float(next(scores))) for doc_id in candidates]
                ranked_per_query.append(
                    [doc_id for doc_id, _ in sorted(scored, key=lambda item: (-item[1], item[0]))]
                )
            return ranked_per_query

        def ranks_from_reranked(ranked_per_query: list[list[str]]) -> list[int | None]:
            ranks: list[int | None] = []
            for ranked, (_, _, relevant) in zip(ranked_per_query, examples, strict=True):
                ranks.append(next((rank for rank, doc_id in enumerate(ranked[:10], 1) if doc_id in relevant), None))
            return ranks

        dense_lexical_fused = []
        for index in range(len(examples)):
            dense_candidates = _top_ranked_doc_ids(dense_scores[index], doc_ids, args.rrf_candidate_depth)
            lexical_candidates = _top_ranked_doc_ids(
                lexical_scores[index], doc_ids, args.rrf_candidate_depth, positive_only=True
            )
            dense_lexical_fused.append(_rrf_scores(dense_candidates, lexical_candidates, rrf_k=args.rrf_k))
        result["dense_lexical_proxy_rrf_rerank"] = _metrics(
            ranks_from_reranked(rerank_fused_batch(dense_lexical_fused)), len(examples)
        )
        dense_only_fused = [
            _rrf_scores(
                _top_ranked_doc_ids(dense_scores[index], doc_ids, args.rrf_candidate_depth), rrf_k=args.rrf_k
            )
            for index in range(len(examples))
        ]
        result["dense_rerank"] = _metrics(
            ranks_from_reranked(rerank_fused_batch(dense_only_fused)), len(examples)
        )
    try:
        if args.skip_sparse:
            raise RuntimeError("skipped by --skip-sparse")
        from FlagEmbedding import BGEM3FlagModel

        sparse_model = BGEM3FlagModel(
            args.sparse_model,
            use_fp16=device == "cuda",
            devices=device,
            batch_size=args.batch_size,
        )
        doc_sparse = _sparse_encode(sparse_model, doc_texts)
        query_sparse = _sparse_encode(sparse_model, [item[1] for item in examples])
        sparse_scores = [_sparse_scores(query_sparse[i], doc_sparse) for i in range(len(examples))]
        sparse_ranks = [_rank_scores(scores, doc_ids, item[2], 10) for scores, item in zip(sparse_scores, examples, strict=True)]
        result["sparse"] = _metrics(sparse_ranks, len(examples))
        dense_sparse_rrf_ranks = []
        for index, (_, _, relevant) in enumerate(examples):
            dense_candidates = _top_ranked_doc_ids(dense_scores[index], doc_ids, args.rrf_candidate_depth)
            sparse_candidates = _top_ranked_doc_ids(
                sparse_scores[index], doc_ids, args.rrf_candidate_depth, positive_only=True
            )
            dense_sparse_rrf_ranks.append(
                _rank_fused(_rrf_scores(dense_candidates, sparse_candidates, rrf_k=args.rrf_k), relevant, 10)
            )
        result["dense_sparse_rrf"] = _metrics(dense_sparse_rrf_ranks, len(examples))
        if reranker is not None:
            dense_sparse_fused = []
            for index in range(len(examples)):
                dense_candidates = _top_ranked_doc_ids(dense_scores[index], doc_ids, args.rrf_candidate_depth)
                sparse_candidates = _top_ranked_doc_ids(
                    sparse_scores[index], doc_ids, args.rrf_candidate_depth, positive_only=True
                )
                dense_sparse_fused.append(_rrf_scores(dense_candidates, sparse_candidates, rrf_k=args.rrf_k))
            result["dense_sparse_rrf_rerank"] = _metrics(
                ranks_from_reranked(rerank_fused_batch(dense_sparse_fused)), len(examples)
            )
    except Exception as exc:  # noqa: BLE001
        result["sparse"] = {"status": "unavailable", "error": str(exc)[:240]}

    output = json.dumps(result, ensure_ascii=False, indent=2)
    print(output)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
