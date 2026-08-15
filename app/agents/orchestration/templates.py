"""办公流程模板库：高频办公场景的预定义 DAG 模板.

规划器只做"意图分类 + 参数抽取"，由模板构造器生成确定性 DAG，
避免 LLM 从零写 DAG 的高出错率（如漏掉文档读取节点）。
"""

from __future__ import annotations

import time
import uuid


def _node(agent: str, name: str, params: dict, depends_on: list[str] | None = None) -> dict:
    return {
        "id": f"n{int(time.time())}-{uuid.uuid4().hex[:6]}",
        "name": name,
        "agent": agent,
        "params": params,
        "depends_on": list(depends_on or []),
    }


class FlowTemplate:
    """流程模板基类."""

    name: str = ""
    description: str = ""
    # 参数说明（给 LLM 抽取参数用），如 {"threshold": "金额阈值（数字）"}
    parameters_schema: dict[str, str] = {}

    def build(
        self,
        request: str,
        params: dict,
        office_docs: list[dict] | None = None,
    ) -> list[dict]:
        """返回节点 dict 列表（供规划器构造 TaskNode）."""
        raise NotImplementedError


class DocumentAnalysisFlow(FlowTemplate):
    """上传文档后的分析/总结/问答/产出流程."""

    name = "document_analysis_flow"
    description = "上传文档后：先分析/读取文档，再按用户要求产出（总结、问答、邮件、改写、纪要等）"
    parameters_schema = {
        "doc_ids": "要处理的文档 doc_id 列表（来自上传的 office_docs）",
        "task": "产出类型：summary/qa/email/rewrite/minutes/extract（默认 qa）",
        "mode": "分析模式：qa（问答）或 summary（总结）",
    }

    def build(self, request, params, office_docs=None):
        docs = office_docs or []
        if params.get("doc_ids"):
            docs = [d for d in docs if str(d.get("doc_id")) in params.get("doc_ids", [])]
        task = str(params.get("task") or "qa")
        mode = str(params.get("mode") or ("summary" if task == "summary" else "qa"))
        nodes = []
        analyze_ids = []
        for d in docs:
            doc_id = str(d.get("doc_id") or "")
            if not doc_id:
                continue
            n = _node(
                "office_doc",
                f"分析文档 {d.get('filename') or doc_id[:8]}",
                {"doc_id": doc_id, "instruction": request, "mode": "analyze", "analyze_mode": mode},
            )
            nodes.append(n)
            analyze_ids.append(n["id"])
        # 产出节点（总结/邮件等），依赖分析节点
        if task in ("summary", "email", "rewrite", "minutes", "extract"):
            nodes.append(
                _node(
                    "office_text",
                    {"summary": "总结文档", "email": "撰写邮件", "rewrite": "改写文本",
                     "minutes": "整理会议纪要", "extract": "信息抽取"}.get(task, task),
                    {"instruction": request, "task": task},
                    analyze_ids,
                )
            )
        return nodes


class InvoiceFilterFlow(FlowTemplate):
    """发票/报销筛选 → 汇总 → 高额通知."""

    name = "invoice_filter_flow"
    description = "发票/报销处理：筛选出金额超过阈值的发票，生成汇总，超过告警阈值的高额发票发邮件通知"
    parameters_schema = {
        "threshold": "筛选金额阈值（数字，默认 10000）",
        "alert_threshold": "高额告警阈值（数字，默认 50000）",
        "notify": "高额通知对象（默认：财务）",
        "doc_ids": "要处理的发票文档 doc_id 列表",
    }

    def build(self, request, params, office_docs=None):
        docs = office_docs or []
        threshold = params.get("threshold") or 10000
        alert = params.get("alert_threshold") or 50000
        notify = str(params.get("notify") or "财务")
        nodes = []
        analyze_ids = []
        for d in docs:
            doc_id = str(d.get("doc_id") or "")
            if not doc_id:
                continue
            n = _node(
                "office_doc",
                f"提取发票 {d.get('filename') or doc_id[:8]}",
                {
                    "doc_id": doc_id,
                    "instruction": f"提取所有发票信息（金额、日期、项目），只保留金额>{threshold} 的：{request}",
                    "mode": "analyze",
                    "analyze_mode": "extract",
                },
            )
            nodes.append(n)
            analyze_ids.append(n["id"])
        nodes.append(
            _node(
                "office_text",
                "生成发票汇总表",
                {
                    "instruction": f"把筛选出的发票整理成汇总表（金额>{threshold}）：{request}",
                    "task": "extract",
                },
                analyze_ids,
            )
        )
        email_node = _node(
            "office_text",
            f"高额发票通知 {notify}",
            {
                "instruction": f"把金额>{alert} 的高额发票整理成邮件发送给{notify}：{request}",
                "task": "email",
            },
            analyze_ids,
        )
        # 高风险写操作（发邮件）默认需人工审批；模板参数 approval=false 可关闭
        if params.get("approval", True):
            email_node["approval"] = True
            email_node["approval_note"] = f"将向 {notify} 发送金额>{alert} 的高额发票邮件（不可逆操作）"
        nodes.append(email_node)
        return nodes


class DailyBriefFlow(FlowTemplate):
    """早晚报生成."""

    name = "daily_brief_flow"
    description = "生成早报/晚报：联网要闻 + 关注领域 + 个人知识库"
    parameters_schema = {
        "period": "morning（早报）或 evening（晚报）",
        "focus": "关注领域/关键词",
    }

    def build(self, request, params, office_docs=None):
        return [
            _node(
                "office_research",
                "生成早晚报",
                {
                    "instruction": request,
                    "mode": "daily_report",
                    "period": str(params.get("period") or "morning"),
                    "focus": str(params.get("focus") or ""),
                },
            )
        ]


class DocumentCompareFlow(FlowTemplate):
    """两份（或以上）文档对比：先分别读取关键信息，再按维度对比异同."""

    name = "document_compare_flow"
    description = "对比两份（或以上）上传文档：分别读取后，按指定维度对比异同/优劣"
    parameters_schema = {
        "doc_ids": "要对比的文档 doc_id 列表（≥2）",
        "dimensions": "对比维度（功能/价格/条款/优缺点等）",
    }

    def build(self, request, params, office_docs=None):
        docs = office_docs or []
        dims = str(params.get("dimensions") or "主要内容与差异")
        nodes = []
        reader_ids = []
        for d in docs:
            doc_id = str(d.get("doc_id") or "")
            if not doc_id:
                continue
            n = _node(
                "office_doc",
                f"读取 {d.get('filename') or doc_id[:8]}",
                {
                    "doc_id": doc_id,
                    "instruction": f"提取该文档的关键信息（供后续对比）：{request}",
                    "mode": "analyze",
                    "analyze_mode": "qa",
                },
            )
            nodes.append(n)
            reader_ids.append(n["id"])
        nodes.append(
            _node(
                "office_text",
                "生成对比结果",
                {
                    "instruction": f"按维度「{dims}」对比以上文档并输出异同/优劣：{request}",
                    "task": "extract",
                },
                reader_ids,
            )
        )
        return nodes


class DocumentCombineFlow(FlowTemplate):
    """多份文档合并汇总：先分别读取，再整合成一份综合总结/报告."""

    name = "document_combine_flow"
    description = "合并多份上传文档：分别读取后，整合成一份综合总结/报告"
    parameters_schema = {
        "doc_ids": "要合并的文档 doc_id 列表",
        "output": "输出形式：summary（总结）或 report（报告）",
    }

    def build(self, request, params, office_docs=None):
        docs = office_docs or []
        output = str(params.get("output") or "summary")
        nodes = []
        reader_ids = []
        for d in docs:
            doc_id = str(d.get("doc_id") or "")
            if not doc_id:
                continue
            n = _node(
                "office_doc",
                f"读取 {d.get('filename') or doc_id[:8]}",
                {
                    "doc_id": doc_id,
                    "instruction": f"提取该文档的要点（供合并）：{request}",
                    "mode": "analyze",
                    "analyze_mode": "summary",
                },
            )
            nodes.append(n)
            reader_ids.append(n["id"])
        nodes.append(
            _node(
                "office_text",
                "合并汇总",
                {
                    "instruction": (
                        f"把以上多份文档的要点合并成一份{'报告' if output == 'report' else '综合总结'}：{request}"
                    ),
                    "task": "summary",
                },
                reader_ids,
            )
        )
        return nodes


class DocumentTranslateFlow(FlowTemplate):
    """文档翻译：读取文档内容 → 翻译成目标语言."""

    name = "document_translate_flow"
    description = "翻译上传文档：读取内容后翻译成目标语言"
    parameters_schema = {"target_lang": "目标语言（如 英文/中文/日文）"}

    def build(self, request, params, office_docs=None):
        docs = office_docs or []
        target = str(params.get("target_lang") or "中文")
        nodes = []
        reader_ids = []
        for d in docs:
            doc_id = str(d.get("doc_id") or "")
            if not doc_id:
                continue
            n = _node(
                "office_doc",
                f"读取 {d.get('filename') or doc_id[:8]}",
                {"doc_id": doc_id, "instruction": request, "mode": "read"},
            )
            nodes.append(n)
            reader_ids.append(n["id"])
        nodes.append(
            _node(
                "office_text",
                f"翻译成{target}",
                {
                    "instruction": f"把文档内容翻译成{target}：{request}",
                    "task": "rewrite",
                },
                reader_ids,
            )
        )
        return nodes


TEMPLATES: list[FlowTemplate] = [
    DocumentAnalysisFlow(),
    InvoiceFilterFlow(),
    DailyBriefFlow(),
    DocumentCompareFlow(),
    DocumentCombineFlow(),
    DocumentTranslateFlow(),
]


def get_template(name: str) -> FlowTemplate | None:
    for t in TEMPLATES:
        if t.name == name:
            return t
    return None


def template_catalog_text() -> str:
    """模板目录文本（给 LLM 做意图分类 + 参数抽取）."""
    lines = []
    for t in TEMPLATES:
        lines.append(f"- {t.name}：{t.description}")
        if t.parameters_schema:
            lines.append(
                "  参数：" + "；".join(f"{k}={v}" for k, v in t.parameters_schema.items())
            )
    return "\n".join(lines)
