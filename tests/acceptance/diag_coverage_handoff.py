"""临时诊断：覆盖链路（workspace_coverage → direct_llm）的交接与提示词。"""

from __future__ import annotations

import asyncio
import sys

EVIDENCE = (
    "已读取 1 个工作区文件（候选 1，失败 0，跳过 0，coverage=SELECTED）\n\n"
    "===== 工作区文件：智慧物业管理系统毕业设计答辩.pptx =====\n"
    "XX大学 计算机与信息工程学院 基于SSM+Vue的智慧物业管理系统设计与实现 本科毕业设计答辩"
)


def main() -> int:
    from app.agents.core.base import WorkerContext
    from app.agents.orchestration.models import TaskNode
    import app.agents.roles.direct_llm as module
    from app.agents.roles.direct_llm import DirectLlmAgent

    captured: dict = {}

    async def fake_office_llm(_ctx, system, prompt, **_kwargs):
        captured["system"] = system
        captured["prompt"] = prompt
        return "OK"

    module.office_llm = fake_office_llm

    # 形态 1：DAG 把节点结果原样放进 dependency_results（最可能的真实形态）
    variants = {
        "plain_content": {"success": True, "content": EVIDENCE, "tool": "workspace_coverage"},
        "with_flag": {"success": True, "content": EVIDENCE, "tool": "workspace_coverage", "read_evidence": True},
        "failed_status": {"status": "failed", "content": EVIDENCE, "error_code": "WORKSPACE_READ_FAILED"},
        "empty_content": {"success": True, "content": "", "tool": "workspace_coverage"},
        "output_only": {"success": True, "output": EVIDENCE},
    }

    for name, dep in variants.items():
        captured.clear()
        node = TaskNode(
            id="answer",
            name="根据工作区文件回答",
            agent="direct_llm",
            params={"instruction": "这个 PPT 说了什么？"},
            depends_on=["discover_workspace"],
            metadata={
                "require_workspace_read_result": True,
                "allow_missing_capability_answer": True,
                "fast_path": "workspace_coverage",
            },
        )
        node.metadata["dependency_results"] = {"discover_workspace": dep}
        ctx = WorkerContext(user_id="u1", job_id="j1", scene="office")
        asyncio.run(DirectLlmAgent().execute(node, ctx))
        system = captured.get("system", "")
        prompt = captured.get("prompt", "")
        honesty = "尚未读取到文件正文" in system
        positive = "不要声明" in system
        has_evidence = "智慧物业" in prompt
        print(
            f"[{name}] honesty_rule={honesty} positive_rule={positive} "
            f"evidence_in_prompt={has_evidence} prompt_chars={len(prompt)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
