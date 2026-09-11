"""验收用 SSE 抓取：把 /chat/stream 的原始事件按“毫秒时间戳 + 类型 + 关键字段”落盘。

用法:
    python scripts/acceptance/sse_capture.py <base_url> <token> <conversation_id> "<问题>" [workspace_id] <case_name>

产出:
    artifacts/acceptance/<case>.sse.tsv   —— 每行: ts_ms <TAB> type <TAB> 关键字段
    artifacts/acceptance/<case>.sse.raw   —— 原始 data: 行（便于复核）
"""

from __future__ import annotations

import json
import pathlib
import sys
import time
import urllib.request

OUT_DIR = pathlib.Path("artifacts/acceptance")


def _detail(event_type: str, evt: dict) -> str:
    if event_type == "delta":
        return f"chars={len(evt.get('content') or '')}"
    if event_type == "process":
        return f"content={(evt.get('content') or '')[:60]!r}"
    if event_type == "task_router":
        return (
            f"route_mode={evt.get('route_mode')} safety={evt.get('safety_action')} "
            f"complexity={(evt.get('task_profile') or {}).get('complexity')}"
        )
    if event_type == "task_policy":
        return f"execution_policy={evt.get('execution_policy')}"
    if event_type in {"tool_started", "tool_completed"}:
        return f"tool={evt.get('tool')} status={evt.get('status')} call_id={evt.get('call_id')}"
    if event_type == "step_started":
        return f"step_id={evt.get('step_id')} index={evt.get('step_index')}"
    if event_type == "step_completed":
        return f"step_id={evt.get('step_id')} status={evt.get('status')}"
    if event_type == "waiting_next":
        return f"next_step_id={evt.get('next_step_id')} status={evt.get('status')}"
    if event_type == "waiting_approval":
        return f"step_id={evt.get('step_id')} risk={evt.get('risk')}"
    if event_type == "task_completed":
        return f"final_chars={len(evt.get('final_answer') or '')}"
    if event_type == "task_failed":
        return f"error_code={evt.get('error_code')} error={(evt.get('error') or '')[:80]!r}"
    if event_type == "done":
        return f"status={evt.get('status')} content_chars={len(evt.get('content') or '')}"
    if event_type == "job":
        return f"job_id={evt.get('job_id')}"
    if event_type == "plan_ready":
        return f"status={evt.get('status')} steps={len(evt.get('steps') or [])}"
    if event_type == "error":
        return f"code={evt.get('code')} message={(evt.get('message') or '')[:80]!r}"
    if event_type == "warning":
        return f"content={(evt.get('content') or '')[:60]!r}"
    return ""


def main() -> int:
    if len(sys.argv) < 7:
        print(__doc__)
        return 2
    base_url, token, conversation_id, content = sys.argv[1:5]
    rest = sys.argv[5:]
    if len(rest) == 1:
        workspace_id, case = "", rest[0]
    else:
        workspace_id, case = rest[0], rest[1]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    body: dict = {"content": content, "conversation_id": conversation_id, "scene": "office"}
    if workspace_id:
        body["workspace_id"] = workspace_id

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/chat/stream",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    started = time.perf_counter()
    types: list[str] = []
    done_count = 0
    first_delta_ms: int | None = None
    tsv_path = OUT_DIR / f"{case}.sse.tsv"
    raw_path = OUT_DIR / f"{case}.sse.raw"
    with tsv_path.open("w", encoding="utf-8") as tsv, raw_path.open("w", encoding="utf-8") as raw:
        with urllib.request.urlopen(request, timeout=1800) as response:
            for line in response:
                text = line.decode("utf-8", "replace").strip()
                if not text.startswith("data:"):
                    continue
                raw.write(text + "\n")
                ts_ms = int((time.perf_counter() - started) * 1000)
                payload = text[5:].strip()
                try:
                    evt = json.loads(payload)
                except ValueError:
                    tsv.write(f"{ts_ms}\tRAW\t{payload[:200]}\n")
                    continue
                event_type = str(evt.get("type") or "")
                types.append(event_type)
                if event_type == "delta" and first_delta_ms is None:
                    first_delta_ms = ts_ms
                if event_type == "done":
                    done_count += 1
                tsv.write(f"{ts_ms}\t{event_type}\t{_detail(event_type, evt)}\n")
                tsv.flush()

    print(f"[{case}] event_sequence = {types}")
    print(f"[{case}] done_count = {done_count}, first_delta_ms = {first_delta_ms}")
    print(f"[{case}] files: {tsv_path} / {raw_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
