"""RAG 召回率基准：纯向量 vs 混合检索（向量 + 关键词 + RRF + 时效）.

语料：10 篇不同主题文档；查询：20 条，覆盖精确术语/同义改写/模糊/标识符/表格/代码。
指标：recall@1 / recall@3 / recall@5（目标文档是否出现在 top-k 结果中）。
"""

import asyncio
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.services.rag import create_space, process_document_pipeline, upload_document_file
from app.services.rag.embeddings import embed_query
from app.services.rag.knowledge import _scope_conditions, search_user_knowledge

USER_ID = "58b3f64f-0d22-4ef8-a79f-69c19e32b9b8"
TOPK = 5

DOCS = [
    ("server_deploy.md", "部署文档：生产环境使用 Nginx 反向代理，监听 80 端口，SSL 证书位于 /etc/nginx/ssl。负载均衡策略为加权轮询，后端节点共 4 台。"),
    ("db_setup.md", "数据库配置：PostgreSQL 连接池大小 20，最大溢出 10，空闲超时 300 秒。索引使用 pgvector 存储向量，余弦距离检索。"),
    ("frontend_react.md", "前端开发：React 组件使用 useState 管理状态，useEffect 处理副作用，zustand 作为全局状态管理库。"),
    ("game_guide.md", "游戏攻略：暗影魔龙 BOSS 血量 500 万，第二阶段会释放全屏火焰，需要提前分散站位，坦克拉仇恨，治疗注意驱散。"),
    ("meeting_notes.md", "会议纪要：2026 年 8 月产品例会，讨论 Q3 预算分配，市场部 300 万，研发部 500 万，运营部 200 万。"),
    ("history_tang.md", "历史资料：唐朝贞观年间长安城人口超百万，东西两市商业繁荣，丝绸之路贸易往来频繁。"),
    ("news_launch.md", "新闻：2026 年 8 月 9 日，公司发布新一代 AI 助手 Lumi 2.0，支持多模态对话与本地知识库。"),
    ("pagination.py", "def paginate(items, page, size):\n    start = (page - 1) * size\n    return items[start:start + size]\n\ndef total_pages(count, size):\n    import math\n    return math.ceil(count / size)"),
    ("budget_table.md", "# 2026 Q3 预算表\n\n| 部门 | 预算 |\n| --- | --- |\n| 市场部 | 300 万 |\n| 研发部 | 500 万 |\n| 运营部 | 200 万 |\n| 客服部 | 100 万 |"),
    ("ops_runbook.md", "运维手册：密钥轮换任务编号 ROTATE-7721，每周日凌晨三点执行，执行窗口 30 分钟，失败自动重试 3 次。"),
]

QUERIES = [
    ("Nginx 反向代理怎么配", "server_deploy.md"),
    ("网站负载均衡怎么做", "server_deploy.md"),
    ("SSL 证书放在哪个目录", "server_deploy.md"),
    ("PostgreSQL 连接池多大合适", "db_setup.md"),
    ("向量检索用什么索引", "db_setup.md"),
    ("React 全局状态管理", "frontend_react.md"),
    ("暗影魔龙第二阶段怎么打", "game_guide.md"),
    ("游戏 BOSS 全屏技能怎么躲", "game_guide.md"),
    ("Q3 预算怎么分配的", "meeting_notes.md"),
    ("市场部研发部运营部各分多少钱", "meeting_notes.md"),
    ("唐朝长安城有多少人", "history_tang.md"),
    ("丝绸之路在哪个朝代最繁荣", "history_tang.md"),
    ("Lumi 2.0 什么时候发布的", "news_launch.md"),
    ("分页函数怎么写", "pagination.py"),
    ("怎么算总页数", "pagination.py"),
    ("各部门预算额度是多少", "budget_table.md"),
    ("ROTATE-7721", "ops_runbook.md"),
    ("密钥轮换任务几点执行", "ops_runbook.md"),
    ("服务器经常挂怎么办", "server_deploy.md"),
    ("帮我查一下上次会议的内容", "meeting_notes.md"),
]


def recall_at(results, target, k):
    for i, r in enumerate(results[:k], 1):
        if r.get("title") == target:
            return i
    return None


async def vector_only(session, query, space_id):
    """纯向量检索（阈值过滤），复现旧行为."""
    vec = await embed_query(query)
    if not vec:
        return []
    conds, params = _scope_conditions(USER_ID, ["chat"], need_embedding=True)
    sql = f"""
        SELECT d.filename AS title, 1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity
        FROM document_chunks c
        JOIN documents d ON d.id = c.document_id
        JOIN knowledge_spaces s ON s.id = c.space_id
        WHERE {' AND '.join(conds)} AND s.id = :space_id
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :top_k
    """
    params["space_id"] = space_id
    stmt = text(sql).bindparams(bindparam("qvec", "[" + ",".join(repr(float(x)) for x in vec) + "]"),
                                bindparam("top_k", TOPK))
    if "tags" in params:
        stmt = stmt.bindparams(bindparam("tags", expanding=True))
    rows = (await session.execute(stmt, params)).mappings().all()
    return [{"title": r["title"], "similarity": float(r["similarity"])} for r in rows
            if float(r["similarity"]) >= settings.RAG_SIMILARITY_THRESHOLD]


async def main() -> None:
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    space_id = None
    try:
        async with factory() as session:
            space = await create_space(session, USER_ID, "RagBench", "bench", "chat")
            space_id = str(space.id)
            paths = {}
            for fname, content in DOCS:
                doc, path = await upload_document_file(session, USER_ID, space_id, fname, content.encode("utf-8"))
                paths[str(doc.id)] = (fname, str(path))
            await session.commit()

        async with factory() as session:
            for doc_id, (fname, path) in paths.items():
                await process_document_pipeline(session, doc_id, path)
        print(f"语料入库完成: {len(paths)} 篇")

        stats = {"vector": {1: 0, 3: 0, 5: 0, "miss": 0}, "hybrid": {1: 0, 3: 0, 5: 0, "miss": 0}}
        weak = []
        detail = []
        async with factory() as session:
            for q, target in QUERIES:
                v = await vector_only(session, q, space_id)
                _, h = await search_user_knowledge(session, USER_ID, q, ["chat"], top_k=TOPK)
                rv = recall_at(v, target, TOPK)
                rh = recall_at(h, target, TOPK)
                detail.append((q, target, rv, rh))
                for mode, r, stats_k in (("vector", rv, stats["vector"]), ("hybrid", rh, stats["hybrid"])):
                    if r is None:
                        stats_k["miss"] += 1
                    elif r <= 1:
                        stats_k[1] += 1
                    if r is not None and r <= 3:
                        stats_k[3] += 1
                    if r is not None and r <= 5:
                        stats_k[5] += 1
                if rh is None:
                    weak.append((q, target, "双路都未召回"))
                elif rv is None and rh is not None:
                    weak.append((q, target, f"混合救回(rank={rh})"))
                elif rv is not None and rh is None:
                    weak.append((q, target, "混合反而丢失!"))
        print("\n逐条明细 (目标 | 纯向量rank | 混合rank):")
        for q, target, rv, rh in detail:
            print(f"  {q[:26]:<28} | {target:<20} | {str(rv):<4} | {rh}")
        print(f"\n共 {len(QUERIES)} 条查询, top_k={TOPK}")
        for mode, s in (("纯向量", stats["vector"]), ("混合", stats["hybrid"])):
            n = len(QUERIES)
            print(f"{mode}: recall@1={s[1]}/{n} ({s[1]/n:.0%}) | @3={s[3]}/{n} ({s[3]/n:.0%}) | @5={s[5]}/{n} ({s[5]/n:.0%}) | miss={s['miss']}")
        print("\n弱项/差异:")
        for row in weak:
            print(" -", row)
    finally:
        if space_id:
            async with factory() as session:
                await session.execute(text("DELETE FROM document_chunks WHERE space_id = :i"), {"i": space_id})
                await session.execute(text("DELETE FROM documents WHERE space_id = :i"), {"i": space_id})
                await session.execute(text("DELETE FROM knowledge_spaces WHERE id = :i"), {"i": space_id})
                await session.commit()
        shutil.rmtree(Path(settings.UPLOAD_DIR) / USER_ID, ignore_errors=True)
        await engine.dispose()
        print("\n[cleaned]")


if __name__ == "__main__":
    asyncio.run(main())
