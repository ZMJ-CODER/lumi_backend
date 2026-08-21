"""Evaluate scoped retrieval against a labelled JSON set.

Example:
    uv run python scripts/evaluate_rag.py --user-id <uuid> \
      --cases tests/fixtures/rag_eval_cases.json --thresholds 0.5,0.55,0.6

The fixture is intentionally versioned but contains placeholder document names.
Copy it for a seeded test account and replace the expectations before using the
report to change production thresholds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.core.database import async_session_factory
from app.services.memory.retrieval import search_user_memories
from app.services.rag.knowledge import search_user_knowledge


async def _evaluate(user_id: str, cases: list[dict], thresholds: list[float]) -> None:
    for threshold in thresholds:
        total = hits = false_positives = 0
        async with async_session_factory() as session:
            for case in cases:
                scope = case.get("scope")
                query = str(case.get("query") or "")
                total += 1
                if scope == "memory":
                    rows = await search_user_memories(session, user_id, query, top_k=5)
                    expected = [str(v).lower() for v in case.get("expected_memory_substrings") or []]
                    found = "\n".join(str(row.get("fact") or "").lower() for row in rows)
                    matched = bool(expected) and all(item in found for item in expected)
                    unexpected = not expected and bool(rows)
                elif scope == "personal_knowledge":
                    _, citations = await search_user_knowledge(
                        session, user_id, query, [], top_k=5, threshold=threshold,
                    )
                    expected = {str(v).lower() for v in case.get("expected_document_names") or []}
                    found = {str(row.get("title") or "").lower() for row in citations}
                    matched = bool(expected) and expected.issubset(found)
                    unexpected = not expected and bool(found)
                else:
                    print(f"skip unsupported scope: {scope} ({case.get('id')})")
                    total -= 1
                    continue
                hits += int(matched)
                false_positives += int(unexpected)
                print(json.dumps({"threshold": threshold, "id": case.get("id"), "matched": matched, "unexpected": unexpected}, ensure_ascii=False))
        precision = hits / max(1, hits + false_positives)
        recall = hits / max(1, sum(bool(c.get("expected_document_names") or c.get("expected_memory_substrings")) for c in cases))
        print(json.dumps({"threshold": threshold, "precision": round(precision, 4), "recall": round(recall, 4), "cases": total}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--thresholds", default="0.5,0.55,0.6")
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    asyncio.run(_evaluate(args.user_id, cases, [float(value) for value in args.thresholds.split(",")]))


if __name__ == "__main__":
    main()
