"""意图分类层：把任务粗分类为 模板 / 半结构 / 自由，路由到不同处理路径.

工程原则：不要一上来就让 LLM 自由规划。
先用规则做粗粒度分类：
  - 多步骤/多主题的复杂任务：优先走半结构/自由规划，避免被单个模板关键词劫持；
  - 模板任务：短指令命中高频场景关键词，且该模板所需文档已就绪 → 直接走模板 DAG；
  - 半结构任务：含条件/分发关键词（如果/超过/并且/还要…）→ 走模式 + 参数化规划；
  - 自由任务：其余复杂流程 → Plan-then-Execute（LLM 自由规划）。
"""

from __future__ import annotations

import re

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

# 多步骤 / 多主题结构词：命中说明是"复杂任务"，不应被单个模板关键词劫持
MULTI_STEP_RE = re.compile(r"第\s*[0-9一二三四五六七八九十百]+\s*步")
MULTI_TOPIC_KEYWORDS = [
    "并且", "同时", "分别", "还要", "另外", "最后", "然后", "汇总成", "交叉核对",
]

EXTERNAL_ACTION_KEYWORDS = [
    "打开应用", "打开软件", "启动应用", "启动软件", "打开浏览器", "打开文件",
    "发送邮件", "发邮件", "打开网址", "访问网页", "结束进程",
]

EXTERNAL_ACTION_VERBS = ["打开", "启动", "运行", "发送", "访问", "结束"]
EXTERNAL_ACTION_TARGETS = [
    "应用", "软件", "程序", "浏览器", "文件", "网址", "网页", "邮件", "进程",
    "excel", "word", "wps", "powerpoint", "微信", "钉钉", "飞书",
]

# 需要先上传文档/上下文才能执行的模板：无 office_docs 时不命中，避免生成空跑节点
DOC_REQUIRED_TEMPLATE_KEYWORDS: dict[str, list[str]] = {
    "invoice_filter_flow": ["发票", "报销", "报销单", "发票筛选"],
    "document_compare_flow": ["对比", "比较", "异同", "区别", "差异"],
    "document_combine_flow": ["合并", "整合"],
    "document_translate_flow": ["翻译", "译成", "翻译成"],
}


def classify(request: str, office_docs: list[dict] | None = None) -> dict:
    """返回 {'task_type': 'template'|'script'|'semi_structured'|'free', 'template': 模板名或None}."""
    req = request or ""
    req_lower = req.lower()
    has_external_action = any(k in req for k in EXTERNAL_ACTION_KEYWORDS) or (
        any(verb in req for verb in EXTERNAL_ACTION_VERBS)
        and any(target in req_lower for target in EXTERNAL_ACTION_TARGETS)
    )
    if office_docs and has_external_action:
        return {"task_type": "free", "template": None}
    # 1) 显式分步骤的复杂任务（第1步/第一步…）：Plan-then-Execute，
    #    模板 / 单一模式都不劫持；仅明确要求"写脚本"时才走脚本节点
    if MULTI_STEP_RE.search(req):
        if office_docs and any(k in req for k in ("脚本", "写个脚本", "编写脚本", "写脚本")):
            return {"task_type": "script", "template": None}
        return {"task_type": "free", "template": None}
    # 1.5) 多主题协调词（并且/同时/分别/还要…）：说明不止一件事，按条件/分发模式规划
    if any(k in req for k in MULTI_TOPIC_KEYWORDS):
        if any(k in req for k in SEMI_KEYWORDS):
            return {"task_type": "semi_structured", "template": None}
        return {"task_type": "free", "template": None}
    # 2) 需要文档的模板：先确认文档已挂载，否则不劫持（避免空跑节点）
    for tpl, kws in DOC_REQUIRED_TEMPLATE_KEYWORDS.items():
        if any(k in req for k in kws):
            if not office_docs:
                break
            return {"task_type": "template", "template": tpl}
    # 文档在场 + 分析/总结/问答/改写等 → document_analysis_flow
    if office_docs and any(k in req for k in TEMPLATE_KEYWORDS["document_analysis_flow"]):
        return {"task_type": "template", "template": "document_analysis_flow"}
    # 早晚报等无文档模板：关键词命中即可
    if any(k in req for k in TEMPLATE_KEYWORDS["daily_brief_flow"]):
        return {"task_type": "template", "template": "daily_brief_flow"}
    # 文档在场 + 转换/导出/批量/脚本 → office_script（写脚本一次执行，不逐步查看）
    if office_docs and any(k in req for k in SCRIPT_KEYWORDS):
        return {"task_type": "script", "template": None}
    if any(k in req for k in SEMI_KEYWORDS):
        return {"task_type": "semi_structured", "template": None}
    return {"task_type": "free", "template": None}
