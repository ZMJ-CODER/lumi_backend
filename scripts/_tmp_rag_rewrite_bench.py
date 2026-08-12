"""临时基准：本地小模型改写提问 vs 原始提问 的 RAG 召回率对比（用完删除）.

复用 benchmark_rag 的语料与查询集：10 篇文档 / 20 条查询。
对每条查询分别用：
  - 原始提问 → 混合检索
  - 本地小模型改写后（qwen2.5:3b, office 场景触发）→ 混合检索
统计 recall@1 / @3 / @5。
"""

import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services.rag import create_space, process_document_pipeline, upload_document_file
from app.services.rag.knowledge import search_user_knowledge
from app.services.rag.query_rewriter import get_retrieval_query
from scripts.benchmark_rag import DOCS, recall_at

USER_ID = "58b3f64f-0d22-4ef8-a79f-69c19e32b9b8"
TOPK = 5

# 口语化查询（模拟真实用户输入）：目标文档与 benchmark 一致
COLLOQUIAL_QUERIES = [
    ("那个服务器部署的文档，Nginx 反代咋配的来着", "server_deploy.md"),
    ("网站负载均衡怎么弄啊，后端有几台机器", "server_deploy.md"),
    ("SSL 证书一般放哪个目录来着", "server_deploy.md"),
    ("数据库连接池整多大比较合适啊", "db_setup.md"),
    ("向量检索用啥索引来着", "db_setup.md"),
    ("暗影魔龙第二阶段全屏火咋躲，有人知道吗", "game_guide.md"),
    ("上次开会说了预算咋分没，市场部多少", "meeting_notes.md"),
    ("唐朝时候长安城有多少人啊", "history_tang.md"),
    ("Lumi 2.0 啥时候发布的来着", "news_launch.md"),
    ("那个分页函数咋写的，帮我看看", "pagination.py"),
    ("各部门预算分别多少来着", "budget_table.md"),
    ("密钥轮换那个任务 ROTATE-7721 是几点跑", "ops_runbook.md"),
    ("服务器老挂，一般咋排查", "server_deploy.md"),
    ("帮我查一下上次会议说了啥", "meeting_notes.md"),
]


def _acc(stats, rank, n):
    if rank is None:
        stats["miss"] += 1
        return
    for k in (1, 3, 5):
        if rank <= k:
            stats[k] += 1


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    space_id = None
    try:
        # 1. 语料入库
        async with factory() as session:
            space = await create_space(session, USER_ID, "RagRewriteBench", "bench", "chat")
            space_id = str(space.id)
            paths = {}
            for fname, content in DOCS:
                doc, path, _ = await upload_document_file(
                    session, USER_ID, space_id, fname, content.encode("utf-8")
                )
                paths[str(doc.id)] = (fname, str(path))
            await session.commit()
        async with factory() as session:
            for doc_id, (fname, path) in paths.items():
                await process_document_pipeline(session, doc_id, path)
        print(f"语料入库完成: {len(paths)} 篇")

        # 2. 逐条对比
        raw_s = {1: 0, 3: 0, 5: 0, "miss": 0}
        rw_s = {1: 0, 3: 0, 5: 0, "miss": 0}
        changed = 0
        rows = []
        async with factory() as session:
            for q, target in COLLOQUIAL_QUERIES:
                _, raw_hits = await search_user_knowledge(
                    session, USER_ID, q, ["chat"], top_k=TOPK
                )
                raw_rank = recall_at(raw_hits, target, TOPK)

                rewritten = await get_retrieval_query(q, None, "office", USER_ID)
                rq = rewritten or q
                if rq != q:
                    changed += 1
                _, rw_hits = await search_user_knowledge(
                    session, USER_ID, rq, ["chat"], top_k=TOPK
                )
                rw_rank = recall_at(rw_hits, target, TOPK)

                _acc(raw_s, raw_rank, TOPK)
                _acc(rw_s, rw_rank, TOPK)
                rows.append((q, rq, target, raw_rank, rw_rank))

        n = len(COLLOQUIAL_QUERIES)
        print(f"\n共 {n} 条查询，top_k={TOPK}，改写生效 {changed} 条")
        print("\n逐条明细 (查询 | 改写后 | 目标 | 原始rank | 改写rank):")
        for q, rq, target, rr, wr in rows:
            mark = " ←改" if rq != q else ""
            diff = " ✓提升" if wr is not None and (rr is None or wr < rr) else (
                " ✗下降" if rr is not None and (wr is None or wr > rr) else ""
            )
            print(f"  {q[:22]:<24} | {rq[:24]:<26} | {target[:18]:<20} | {str(rr):<5} | {wr}{diff}{mark}")

        print("\n===== 汇总 =====")
        for label, s in (("原始提问", raw_s), ("本地改写后", rw_s)):
            print(
                f"{label}: recall@1={s[1]}/{n} ({s[1]/n:.0%}) | "
                f"@3={s[3]}/{n} ({s[3]/n:.0%}) | "
                f"@5={s[5]}/{n} ({s[5]/n:.0%}) | miss={s['miss']}"
            )
    finally:
        if space_id:
            async with factory() as session:
                await session.execute(
                    text("DELETE FROM document_chunks WHERE space_id = :i"), {"i": space_id}
                )
                await session.execute(
                    text("DELETE FROM documents WHERE space_id = :i"), {"i": space_id}
                )
                await session.execute(
                    text("DELETE FROM knowledge_spaces WHERE id = :i"), {"i": space_id}
                )
                await session.commit()
        shutil.rmtree(Path(settings.UPLOAD_DIR) / USER_ID, ignore_errors=True)
        await engine.dispose()
        print("\n[cleaned]")


if __name__ == "__main__":
    asyncio.run(main())
