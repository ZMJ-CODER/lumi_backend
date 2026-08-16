"""意图分类层：把任务粗分类为 模板 / 半结构 / 自由，路由到不同处理路径.

工程原则：不要一上来就让 LLM 自由规划。
先用规则做粗粒度分类：
  - 模板任务：高频场景关键词命中 → 直接走模板 DAG；
  - 半结构任务：含条件/分发关键词（如果/超过/并且/还要…）→ 走模式 + 参数化规划；
  - 自由任务：其余复杂流程 → Plan-then-Execute（LLM 自由规划）。
"""

from __future__ import annotations

# 模板关键词（值 = 模板名）
TEMPLATE_KEYWORDS: dict[str, list[str]] = {
    "invoice_filter_flow": ["发票", "报销", "报销单", "发票筛选"],
    "daily_brief_flow": ["早报", "晚报", "晨报"],
    "document_analysis_flow": [
        "总结", "分析", "问答", "摘要", "会议纪要", "改写",
        "根据文档", "根据文件", "这份文档", "这个文件", "文档里", "文件里",
        "阅读文档", "读一下", "提取", "概括",
    ],
    "document_compare_flow": ["对比", "比较", "异同", "区别", "差异"],
    "document_combine_flow": ["合并", "整合"],
    "document_translate_flow": ["翻译", "译成", "翻译成"],
}

# 半结构任务：条件/分发/通知等结构化意图
SEMI_KEYWORDS = [
    "如果", "超过", "大于", "低于", "小于", "当", "并且", "同时", "还要",
    "另外", "条件", "才", "否则", "分别", "通知", "汇总", "筛选", "审批",
]

# 脚本任务：格式转换 / 批量处理 / 数据导出等 → 写脚本执行
SCRIPT_KEYWORDS = [
    "转换", "转成", "导出", "生成csv", "生成CSV", "批量", "脚本",
    "保存为", "另存为", "提取到", "整理", "汇总成",
]


def classify(request: str, office_docs: list[dict] | None = None) -> dict:
    """返回 {'task_type': 'template'|'script'|'semi_structured'|'free', 'template': 模板名或None}."""
    req = request or ""
    # 先查更具体的模板（对比/合并/翻译/发票/早晚报），避免"合并成总结"被文档分析截胡
    for tpl, kws in TEMPLATE_KEYWORDS.items():
        if tpl == "document_analysis_flow":
            continue
        if any(k in req for k in kws):
            return {"task_type": "template", "template": tpl}
    # 文档在场 + 分析/总结/问答/改写等 → document_analysis_flow
    if office_docs and any(k in req for k in TEMPLATE_KEYWORDS["document_analysis_flow"]):
        return {"task_type": "template", "template": "document_analysis_flow"}
    # 文档在场 + 转换/导出/批量/脚本 → office_script（写脚本一次执行，不逐步查看）
    if office_docs and any(k in req for k in SCRIPT_KEYWORDS):
        return {"task_type": "script", "template": None}
    if any(k in req for k in SEMI_KEYWORDS):
        return {"task_type": "semi_structured", "template": None}
    return {"task_type": "free", "template": None}
