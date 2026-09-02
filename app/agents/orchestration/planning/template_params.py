"""办公模板的确定性默认参数。"""

from __future__ import annotations


def template_default_params(template: str, request: str) -> dict:
    if template == "document_analysis_flow":
        task = "summary" if any(key in request for key in ("总结", "摘要")) else "qa"
        return {"task": task, "mode": "summary" if task == "summary" else "qa"}
    if template == "daily_brief_flow":
        return {"period": "evening" if any(key in request for key in ("晚报", "晚间")) else "morning", "focus": ""}
    if template == "invoice_filter_flow":
        return {"threshold": 10000, "alert_threshold": 50000, "notify": "财务"}
    if template == "document_compare_flow":
        return {"dimensions": ""}
    if template == "document_combine_flow":
        return {"output": "summary"}
    if template == "document_translate_flow":
        return {"target_lang": next((language for language in ("英文", "日文", "韩文", "法文", "德文") if language in request), "中文")}
    return {}
