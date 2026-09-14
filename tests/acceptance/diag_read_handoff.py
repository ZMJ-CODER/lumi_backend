"""诊断：工作区 read 成功后，进入下游文本节点的依赖内容到底是什么。

复现用户报告的现象：模型读到文档却声称"看不到具体内容"。
"""

from __future__ import annotations

import asyncio
import json
import sys

from tests.acceptance.workspace_navigator_sanitize import collect_report


def main() -> int:
    collect_report()  # 写入验收文件 + 安装假 Electron

    from contextlib import asynccontextmanager

    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    from app.agents.roles.atomic import AtomicStepAgent
    from app.agents.skills import executor as executor_module
    from app.agents.skills.loader import load_skill_plugins

    # 诊断环境没有 Redis：跳过并发互斥租约，只验证数据交接。
    @asynccontextmanager
    async def _no_lock(tool_name, execution_scope):
        yield

    executor_module._claim_tool_execution = _no_lock

    load_skill_plugins()

    node = TaskNode(
        id="read_workspace",
        name="读取工作区文件",
        agent="atomic_step",
        params={
            "instruction": "读取工作区文件 答辩.pptx，为后续回答提供事实材料。",
            "preferred_tool": "workspace_navigator",
            "inputs": {"action": "read", "path": "答辩.pptx"},
        },
        metadata={"fast_path": "workspace_m1_read"},
    )
    ctx = WorkerContext(user_id="u1", job_id="j1", scene="office", workspace_id="ws-acceptance")
    result = asyncio.run(AtomicStepAgent().execute(node, ctx))

    print("=== 原子步骤结果键 ===")
    print(sorted(result.keys()))
    print("success:", result.get("success"), "tool:", result.get("tool"))
    content = str(result.get("content") or "")
    print(f"=== 交给下游的 content：{len(content)} 字符 ===")
    print(repr(content[:600]))
    print("--- 是否包含正文事实片段 ---")
    for probe in ("PPTX 幻灯片正文", "三层架构", "sections", "slide-1"):
        print(f"  {probe!r} in content -> {probe in content}")
    print("--- 是否为合法 JSON ---")
    try:
        json.loads(content)
        print("  content 是合法 JSON")
    except Exception as exc:  # noqa: BLE001
        print(f"  不是合法 JSON：{str(exc)[:120]}")

    execution = result.get("execution") or {}
    print("=== execution 信封 ===")
    print(json.dumps(execution, ensure_ascii=False)[:800])
    return 0


if __name__ == "__main__":
    sys.exit(main())
