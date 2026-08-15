import asyncio, sys, os
sys.path.insert(0, r'E:\pythonpycharm\lumi_backend')
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from app.core.config import settings
from app.services.rag import create_space, process_document_pipeline, upload_document_file
from app.services.rag.knowledge import search_user_knowledge
import importlib.util
spec = importlib.util.spec_from_file_location("br", r'E:\pythonpycharm\lumi_backend\scripts\benchmark_rag.py')
br = importlib.util.module_from_spec(spec); spec.loader.exec_module(br)

USER_ID = br.USER_ID; TAG = br.SPACE_TAG; TOPK = 5

async def main():
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    space_id = None
    created = []
    try:
        async with factory() as session:
            space = await create_space(session, USER_ID, "DiagSim", "相似度分布诊断", TAG)
            space_id = str(space.id)
            paths = {}
            for fname, _cat, content in br.DOCS:
                doc, path, _ = await upload_document_file(session, USER_ID, space_id, fname, content.encode())
                paths[str(doc.id)] = (fname, str(path))
            await session.commit()
        async with factory() as session:
            for doc_id, (fname, path) in paths.items():
                await process_document_pipeline(session, doc_id, path)
                created.append(path)
        print("corpus ready:", len(paths))

        true_sim, neg_sim = [], []
        none_true = 0
        async with factory() as session:
            for q, t, qt in br.QUERIES:
                _, hits = await search_user_knowledge(session, USER_ID, q, [TAG], top_k=TOPK)
                if t:
                    hit = next((h for h in hits if h.get("title") == t), None)
                    if hit is None:
                        none_true += 1
                    else:
                        true_sim.append(hit.get("similarity"))
                else:
                    neg_sim.append(max([h.get("similarity") or 0 for h in hits], default=0))

        def buckets(sims, label):
            from collections import Counter
            c = Counter()
            for s in sims:
                if s is None: c["None"] += 1
                elif s >= 0.7: c[">=0.7"] += 1
                elif s >= 0.6: c["0.6-0.7"] += 1
                elif s >= 0.5: c["0.5-0.6"] += 1
                elif s >= 0.45: c["0.45-0.5"] += 1
                else: c["<0.45"] += 1
            print(f"\n{label} n={len(sims)}:")
            for k in [">=0.7", "0.6-0.7", "0.5-0.6", "0.45-0.5", "<0.45", "None"]:
                print(f"  {k}: {c.get(k,0)}")
            return c

        tb = buckets(true_sim, "真命中相似度分布")
        nb = buckets(neg_sim, "负例 top-1 相似度分布")
        print("\n真命中未找到:", none_true)
        # 如果阈值设为 0.7：真命中保留比例 vs 负例误召回保留比例
        true_keep = tb.get(">=0.7", 0)
        neg_keep = nb.get(">=0.7", 0)
        print(f"\n若阈值=0.7: 真命中保留 {true_keep}/{len(true_sim)} ({true_keep/max(1,len(true_sim)):.1%}) | 负例误召回保留 {neg_keep}/{len(neg_sim)} ({neg_keep/max(1,len(neg_sim)):.1%})")
        for thr in (0.55, 0.6, 0.65):
            tk = sum(1 for s in true_sim if s is not None and s >= thr)
            nk = sum(1 for s in neg_sim if s >= thr)
            print(f"若阈值={thr}: 真命中保留 {tk}/{len(true_sim)} ({tk/max(1,len(true_sim)):.1%}) | 负例误召回保留 {nk}/{len(neg_sim)} ({nk/max(1,len(neg_sim)):.1%})")
    finally:
        if space_id:
            async with factory() as session:
                await session.execute(text("DELETE FROM document_chunks WHERE space_id = :i"), {"i": space_id})
                await session.execute(text("DELETE FROM documents WHERE space_id = :i"), {"i": space_id})
                await session.execute(text("DELETE FROM knowledge_spaces WHERE id = :i"), {"i": space_id})
                await session.commit()
        for fp in created:
            try: os.remove(fp)
            except OSError: pass
        await engine.dispose()
        print("\n[cleaned]")

asyncio.run(main())
