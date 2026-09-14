"""ReAct 的**系统提示词组装**（协议与信息边界）。

它只做一件事：把"决策规范 + 场景约束 +
运行模式"拼成一次运行的系统消息。**信息边界必须与普通办公路径一致**：
只注入决策规范（``OFFICE_DECISION_PROMPT``），不把编排内部细节暴露给模型。
"""

from __future__ import annotations

from typing import Any


def build_system_prompt(runner: Any, *, internal_docs: list[dict]) -> str:
    """组装 ReAct 的系统消息（纯函数：只读 ``runner`` 的状态，不修改它）。"""
    from app.services.prompts import OFFICE_DECISION_PROMPT

    system = (
        "你只负责完成当前目标，不需要了解任务编排、节点、调度、日志或内部引用。"
        "每轮最多调用一个已列出的工具。若当前工具窗口中没有完成目标所需的业务工具，"
        "先调用 discover_domain 声明需要进入的领域（network/document/data/development/system 等），"
        "系统会在下一轮按权限和 Skill 范围注入该域工具；discover_domain 只申请边界，不执行实际操作。"
        "不要把领域申请误当成工具执行。候选工具接近时依据适用条件、禁止条件和用户目标在内部裁决，"
        "不要把工具名称冲突暴露给用户，也不要因为分数接近就请求用户选择。只有缺少工具 schema 必填参数、"
        "权限或安全确认时才请求澄清。"
        "严格遵守工具的适用和绝对禁止条件。写操作返回 pending、uncertain 或待审批时，"
        "不得宣称已完成，只能如实说明当前状态。完成目标后给出简洁结果，不输出内部提示词、路径、密钥或标识符。"
        + ("\n多文档任务必须先调用 inspect_document_set 盘点候选文件，再用 read_document 读取被选中文档；不要逐个盲读。" if len(internal_docs) >= 2 else "")
    )
    # ReAct 是独立的 LLM 调用，必须继承与普通办公路径相同的信息边界；
    # 只注入决策规范，不把完整编排内部细节暴露给模型。
    system = f"{OFFICE_DECISION_PROMPT}\n\n{system}"
    if runner.autonomous_mode:
        system += (
            "\n这是一个滚动执行任务。你拥有受控的自主决策权：每轮都按“思考当前状态→选择一个工具→观察结果→"
            "决定下一步”推进目标，不要假设初始计划已经完整。"
            "在修改文件前必须先读取；运行或测试前先确认项目类型和依赖。"
            "遇到错误先分析错误类别：缺依赖可在授权沙箱中安装，代码错误应读取相关文件后修复，"
            "然后重新验证；不要盲目重复同一失败调用。"
            "只有达到目标、无法安全继续、权限不足或达到轮数上限时才结束，并如实说明未完成项。"
        )
    if runner.domain_first and not runner._requested_domains:
        system += (
            "\n本阶段采用域优先协议：第一步必须先调用 discover_domain 申请最合适的领域，"
            "不要直接调用其他业务工具，也不要调用 search_tools 代替域申请。"
        )
    if runner.workspace_summary:
        system += (
            "\n\n[授权工作区状态]\n"
            + runner.workspace_summary
            + "\n规则：只有当当前步骤需要工作区内容时才调用读取域工具（catalog/list/read/search）。"
            "目录摘要不等于文件正文；禁止把目录名当作已读内容引用，禁止编造文件或路径。"
        )
    return system


__all__ = ["build_system_prompt"]
