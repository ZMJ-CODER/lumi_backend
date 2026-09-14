"""端到端复现：20 段 PPT → workspace_coverage → direct_llm 的实际提示词。"""

from __future__ import annotations

import asyncio
import sys


def main() -> int:
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.knowledge.workspace_coverage import WorkspaceCoverageAgent
    from app.agents.skills.loader import load_skill_plugins
    from app.workspace.read import navigator as _wn  # noqa: F401
    import app.agents.mcp.manager as manager
    import app.workspace.context as wc
    import app.workspace.read.reader as wr

    load_skill_plugins()

    # 20 页 PPT，每页约 3000 字符（模拟真实的答辩 PPT）
    slides = [
        {"title": f"第{i}页", "text": f"第{i}页正文：" + "智慧物业管理系统内容" * 300}
        for i in range(1, 21)
    ]

    async def list_tools(_server):
        return [
            {"name": "workspace_list"},
            {"name": "workspace_stat"},
            {"name": "workspace_search"},
            {"name": "workspace_content_extract"},
        ]

    async def call_tool(_server, tool_name, args, **_kwargs):
        args = dict(args or {})
        if tool_name == "workspace_list":
            return {
                "status": "success",
                "data": {"entries": [{"path": "智慧物业管理系统毕业设计答辩.pptx", "type": "file", "size": 999}]},
                "has_more": False,
                "cursor": "",
            }
        if tool_name == "workspace_stat":
            return {"status": "success", "data": {"kind": "file", "size": 999}}
        if tool_name == "workspace_search":
            return {
                "status": "success",
                "data": {"matches": [{
                    "path": "智慧物业管理系统毕业设计答辩.pptx", "line": 1,
                    "text": "基于SSM+Vue的智慧物业管理系统", "match_type": "content",
                }]},
                "has_more": False,
                "cursor": "",
            }
        return {"status": "success", "data": {"slides": slides}}

    manager.list_tools = list_tools
    manager.call_tool = call_tool
    route = {"status_code": "WORKSPACE_READY", "server_name": "lumi_pc", "device_id": "dev-1"}
    wc.resolve_workspace_desktop = lambda *a, **k: dict(route)
    wr.WorkspaceReader._route = lambda self: dict(route)

    node = TaskNode(
        id="discover_workspace",
        name="搜索并读取相关工作区文件",
        agent="workspace_coverage",
        params={"query": "你再读取一下文档看看说了什么", "mode": "selected"},
        metadata={"fast_path": "workspace_coverage", "require_workspace_read_result": True},
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="ws1")
    result = asyncio.run(WorkspaceCoverageAgent().execute(node, ctx))

    content = str(result.get("content") or "")
    print("[coverage] success:", result.get("success"), "status:", result.get("status"))
    print("[coverage] content_chars:", len(content))
    print("[coverage] 含第20页:", "第20页正文" in content)
    print("[coverage] 元数据 coverage:", (result.get("tool_metadata") or {}).get("workspace_coverage", {}).get("coverage"))

    # 交给 direct_llm
    import app.agents.roles.direct_llm as module
    from app.agents.roles.direct_llm import DirectLlmAgent

    captured: dict = {}

    async def fake_office_llm(_ctx, system, prompt, **_kwargs):
        captured["system"] = system
        captured["prompt"] = prompt
        return "OK"

    module.office_llm = fake_office_llm
    answer = TaskNode(
        id="answer", name="根据工作区文件回答", agent="direct_llm",
        params={"instruction": "根据前一步读取到的文件内容回答：你再读取一下文档看看说了什么"},
        depends_on=["discover_workspace"],
        metadata={
            "fast_path": "workspace_coverage",
            "allow_missing_capability_answer": True,
            "require_workspace_read_result": True,
        },
    )
    # 模拟 DAG 的 dependency payload
    dependency = dict(result)
    dependency.setdefault("status", "completed")
    answer.metadata["dependency_results"] = {"discover_workspace": dependency}
    asyncio.run(DirectLlmAgent().execute(answer, ctx))

    prompt = captured.get("prompt", "")
    system = captured.get("system", "")
    print("[direct_llm] prompt_chars:", len(prompt))
    print("[direct_llm] 证据在提示词里:", "===== 工作区文件" in prompt)
    print("[direct_llm] 含第20页:", "第20页正文" in prompt)
    print("[direct_llm] 降级规则被触发:", "尚未读取到文件正文" in system)
    print("[direct_llm] 正面契约在:", "不要声明" in system)
    return 0


if __name__ == "__main__":
    sys.exit(main())
