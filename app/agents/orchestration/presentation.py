"""办公任务面向用户的执行说明。

这里的文字是可审计的计划/动作/结果摘要，不是模型内部推理链：不记录隐式
推理、不暴露工具参数或文档原文，只说明系统准备做什么、正在做什么和做成了什么。
"""

from __future__ import annotations

import re
from typing import Any


_ACTIONS = {
    "react_step": "动态分析与执行",
    "office_doc_read": "阅读文档内容",
    "office_doc_analyze": "阅读并分析文档",
    "office_doc_edit": "编辑文档",
    "query_knowledge": "检索相关资料",
    "web_search": "查询公开资料",
    "python_exec": "运行脚本处理数据",
    "create_office_document": "生成办公文档",
    "open_app": "打开所需应用",
    "open_file": "打开所需文件",
    "open_url": "打开相关网页",
    "compose_email": "起草邮件内容",
    "compose_official_doc": "起草正式文档",
    "summarize_text": "整理关键信息",
    "meeting_minutes": "整理会议纪要",
    "extract_info": "提取所需信息",
    "invoice_parse": "提取发票信息",
    "calendar_manager": "处理日程安排",
    "todo_manager": "整理待办事项",
    "competitor_analysis": "完成竞品资料分析",
    "document_qa": "基于资料回答问题",
}


def _value(node: Any, key: str) -> str:
    params = getattr(node, "params", {}) or {}
    inputs = params.get("inputs") or {}
    return str(inputs.get(key) or params.get(key) or "").strip()


def _document_name(node: Any) -> str:
    """只从计划中的可读名称取文件名，绝不把内部 doc_id 展示给用户。"""
    for key in ("filename", "name"):
        value = _value(node, key)
        if value:
            return value
    name = str(getattr(node, "name", "") or "")
    matched = re.search(r"(?:文档|文件|发票)\s+(.+)$", name)
    return matched.group(1).strip() if matched else ""


def step_action(node: Any) -> str:
    """返回动作短语，优先使用已规划工具，避免向用户展示 agent/tool 名。"""
    params = getattr(node, "params", {}) or {}
    tool = str(params.get("preferred_tool") or "").strip()
    action = _ACTIONS.get(tool)
    doc_name = _document_name(node)
    if action and doc_name and tool.startswith("office_doc_"):
        return f"{action}《{doc_name}》"
    if action:
        return action
    title = str(getattr(node, "name", "") or "").strip()
    return title or "完成当前处理"


def intent_text(node: Any) -> str:
    """步骤尚未开始时的公开计划说明。"""
    action = step_action(node)
    return f"我需要先{action}，以便为后续处理准备依据。"


def working_text(node: Any) -> str:
    """步骤执行中的公开动作说明。"""
    return f"我正在{step_action(node)}。"


def _outcome(result: dict | None) -> str:
    result = result or {}
    outputs = result.get("outputs") or result.get("artifacts") or []
    if outputs:
        first = outputs[0]
        name = first.get("name") if isinstance(first, dict) else str(first)
        if name:
            return f"已生成 {str(name)[:80]}"
    content = str(result.get("content") or result.get("output") or "").strip()
    if not content:
        return "已得到可用于下一步的结果"
    # 只取首行的短摘要，不把文档正文或工具原始输出塞进执行说明。
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    first_line = re.sub(r"^[#>*\-\d.\s]+", "", first_line).strip()
    if not first_line or len(first_line) > 72:
        return "已得到可用于下一步的结果"
    return f"已得到结果：{first_line}"


def completed_text(node: Any, result: dict | None = None) -> str:
    return f"我已完成{step_action(node)}，{_outcome(result)}。"


def failed_text(node: Any, error: str | None = None) -> str:
    detail = str(error or "未能完成").strip().replace("\n", " ")[:100]
    return f"我尝试{step_action(node)}，但未完成：{detail}。"


def attach_display_plan(node: Any) -> None:
    """把公开计划写入节点快照，方便断线恢复后仍能显示相同说明。"""
    metadata = dict(getattr(node, "metadata", {}) or {})
    metadata["display"] = {
        "intent": intent_text(node),
        "working": working_text(node),
    }
    node.metadata = metadata


def attach_display_result(node: Any, result: dict) -> dict:
    """给节点执行结果附加公开完成摘要，供前端直接显示。"""
    value = dict(result or {})
    display = dict(value.get("display") or {})
    display["completed"] = completed_text(node, value)
    value["display"] = display
    return value
