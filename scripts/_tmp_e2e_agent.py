"""临时端到端验收：本地项目 → LLM 规划 → DAG → code agent → 客户端文件读写.

用脚本模拟 Electron 客户端（轮询 /tools/requests → 本地执行 → 回传），
验证完整链路；运行前需本地启动后端（uvicorn app.main:app --port 8000）。
"""

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import redis.asyncio as aioredis
from sqlalchemy import delete, select

from app.core.database import async_session_factory
from app.models.db_models import Conversation, Message, RefreshToken, User

BASE = "http://127.0.0.1:8000/api/v1"


async def get_captcha(client, redis):
    resp = await client.get("/auth/captcha")
    d = resp.json()["data"]
    ans = await redis.get(f"captcha:{d['captcha_id']}")
    return d["captcha_id"], ans


async def register(client, redis, account, pw="Test1234"):
    cid, ans = await get_captcha(client, redis)
    return await client.post(
        "/auth/register",
        json={
            "account": account,
            "password": pw,
            "captcha_id": cid,
            "captcha_result": str(ans),
        },
    )


def scan_folder(root: Path) -> list[dict]:
    files = []
    for p in sorted(root.rglob("*")):
        if p.is_dir():
            continue
        rel = str(p.relative_to(root)).replace("\\", "/")
        if any(x in rel for x in ("node_modules", ".git", "__pycache__", ".venv")):
            continue
        size = p.stat().st_size
        symbols = summary = ""
        if size <= 512 * 1024:
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
                syms = [
                    ln.strip().split("def ", 1)[1].split("(")[0]
                    for ln in content.split("\n")
                    if ln.strip().startswith("def ")
                ]
                symbols = ", ".join(syms[:20])
                summary = "".join(ln.strip().lstrip("# ") for ln in content.split("\n") if ln.strip().startswith("#"))[:200]
            except Exception:  # noqa: BLE001
                pass
        files.append({"path": rel, "symbols": symbols, "summary": summary, "size": size})
    return files


def execute_local(req: dict, roots: dict) -> dict:
    skill = req.get("skill")
    params = req.get("params") or {}
    root = roots.get(params.get("project_id"))
    if not root:
        return {"success": False, "error": "项目未在本机注册", "metadata": {"error_code": "NO_PROJECT"}}
    base = Path(root).resolve()
    target = (base / params.get("path", "")).resolve()
    if not str(target).startswith(str(base)):
        return {"success": False, "error": "路径越界", "metadata": {"error_code": "FORBIDDEN"}}
    try:
        if skill == "read_project_file":
            text = target.read_text(encoding="utf-8", errors="replace")
            return {"success": True, "output": text[: (params.get("max_chars") or 200000)]}
        if skill == "write_project_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = params.get("content", "")
            target.write_text(content, encoding="utf-8")
            return {"success": True, "output": f"已写入 {len(content)} 字节 → {params.get('path')}"}
        if skill == "list_project":
            items = [
                {
                    "name": p.name,
                    "isDirectory": p.is_dir(),
                    "size": p.stat().st_size if p.is_file() else None,
                }
                for p in base.iterdir()
                if not p.name.startswith(".")
            ]
            lines = "\n".join(f"[{'目录' if i['isDirectory'] else '文件'}] {i['name']}" for i in items)
            return {"success": True, "output": lines}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc), "metadata": {"error_code": "EXEC_ERROR"}}
    return {"success": False, "error": f"未知技能 {skill}", "metadata": {"error_code": "SKILL_NOT_FOUND"}}


async def fake_client_loop(client, headers, roots, stop):
    while not stop.is_set():
        try:
            r = await client.get("/tools/requests", headers=headers)
            for req in r.json().get("data", {}).get("items", []):
                result = execute_local(req, roots)
                await client.post(
                    f"/tools/requests/{req['request_id']}/result",
                    json=result,
                    headers=headers,
                )
        except Exception as exc:  # noqa: BLE001
            print("  [client] poll error:", exc)
        await asyncio.sleep(0.5)


async def main():
    redis = aioredis.from_url("redis://127.0.0.1:6379/0", decode_responses=True)
    acc = f"e2e_{uuid.uuid4().hex[:6]}"
    demo = Path("data/e2e_project")
    if demo.exists():
        shutil.rmtree(demo)
    demo.mkdir(parents=True)
    (demo / "order_service.py").write_text(
        'def create_order(user_id, amount):\n'
        '    """创建订单"""\n'
        '    return {"user_id": user_id, "amount": amount, "status": "created"}\n',
        encoding="utf-8",
    )

    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        r = await register(client, redis, acc)
        if r.status_code != 200:
            print("注册失败:", r.text[:200])
            return
        tok = r.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {tok}"}
        uid = r.json()["data"]["user_id"]

        # 注册本地项目（结构索引）
        files = scan_folder(demo)
        r = await client.post(
            "/projects",
            json={"name": "订单系统", "root_label": str(demo), "files": files},
            headers=headers,
        )
        proj = r.json()["data"]
        pid = proj["project_id"]
        print("项目已注册:", proj["name"], "| files:", proj["file_count"])
        roots = {pid: str(demo.resolve())}

        # 提交代码任务
        r = await client.post(
            "/agents/jobs",
            json={
                "request": "在订单系统里添加一个 calculate_total 函数，计算订单金额总和，追加到 order_service.py",
                "project_id": pid,
            },
            headers=headers,
        )
        job = r.json()["data"]
        jid = job["job_id"]
        print("任务已提交:", jid[:8], "| 节点:", [(n["agent"], n["name"]) for n in job["nodes"]])

        # 并行：fake client + 任务状态轮询
        stop = asyncio.Event()
        client_task = asyncio.create_task(fake_client_loop(client, headers, roots, stop))
        final = None
        for _ in range(240):
            await asyncio.sleep(1)
            cur = await client.get(f"/agents/jobs/{jid}", headers=headers)
            final = cur.json()["data"]
            if final["status"] in ("completed", "failed", "cancelled", "interrupted"):
                break
        stop.set()
        await client_task

        print("\n任务状态:", final["status"])
        for n in final["nodes"]:
            print(" 节点:", n["name"], "|", n["status"], "| error:", n["error"])
            if n.get("result"):
                content = n["result"].get("content") or json.dumps(n["result"], ensure_ascii=False)
                print("   result:", str(content)[:150])

        content = (demo / "order_service.py").read_text(encoding="utf-8")
        print("\n文件包含 calculate_total:", "calculate_total" in content)
        print("--- 文件内容 ---")
        print(content[:600])

        # 清理
        await client.delete(f"/projects/{pid}", headers=headers)
        async with async_session_factory() as s:
            u = (await s.execute(select(User).where(User.account == acc))).scalar_one_or_none()
            if u:
                cids = (await s.execute(select(Conversation.id).where(Conversation.user_id == u.id))).scalars().all()
                for cid in cids:
                    await s.execute(delete(Message).where(Message.conversation_id == cid))
                await s.execute(delete(Conversation).where(Conversation.user_id == u.id))
                await s.execute(delete(RefreshToken).where(RefreshToken.user_id == u.id))
                await s.execute(delete(User).where(User.id == u.id))
                await s.commit()
        shutil.rmtree(demo, ignore_errors=True)
        print("\n[cleaned]")
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
