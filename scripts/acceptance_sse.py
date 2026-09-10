# 用法: python acceptance_sse.py <token> <conversation_id> "<问题>" [workspace_id] <case_name>
import json, sys, time, urllib.request

token, conv, question = sys.argv[1], sys.argv[2], sys.argv[3]
workspace = sys.argv[4] if len(sys.argv) > 5 else ""
case = sys.argv[-1]
body = {"content": question, "conversation_id": conv, "scene": "office"}
if workspace:
    body["workspace_id"] = workspace

req = urllib.request.Request(
    "http://127.0.0.1:8000/api/v1/chat/stream",
    data=json.dumps(body).encode(),
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
)
t0 = time.perf_counter()
out = open(f"artifacts/acceptance/{case}.sse.tsv", "w", encoding="utf-8")
with urllib.request.urlopen(req, timeout=600) as resp:
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        ts = int((time.perf_counter() - t0) * 1000)
        try:
            evt = json.loads(line[5:].strip())
        except ValueError:
            out.write(f"{ts}\tRAW\t{line[:200]}\n"); continue
        t = evt.get("type")
        detail = {
            "delta": len(evt.get("content") or ""),
            "process": (evt.get("content") or "")[:60],
            "tool_started": evt.get("tool"), "tool_completed": evt.get("status"),
            "step_started": evt.get("step_id"), "step_completed": evt.get("status"),
            "waiting_next": evt.get("next_step_id"), "task_failed": evt.get("error_code"),
            "task_completed": len(evt.get("final_answer") or ""),
            "done": (evt.get("run_view") or {}).get("status") or evt.get("status"),
            "task_router": (evt.get("route_mode"), evt.get("safety_action")),
            "task_policy": evt.get("execution_policy"),
            "job": evt.get("job_id"), "plan_ready": evt.get("status"),
            "error": evt.get("code"), "warning": (evt.get("content") or "")[:40],
        }.get(t, "")
        out.write(f"{ts}\t{t}\t{detail}\n")
out.close()
print(open(f"artifacts/acceptance/{case}.sse.tsv", encoding="utf-8").read())