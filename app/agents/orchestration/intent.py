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
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

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


def _has_semi_structure(request: str) -> bool:
    """Match conditional/workflow wording without treating ``当前`` as ``当``."""
    if any(keyword in request for keyword in SEMI_KEYWORDS if keyword != "当"):
        return True
    return bool(re.search(r"当(?!前)", request))

# 脚本任务：格式转换 / 批量处理 / 数据导出等 → 写脚本执行
SCRIPT_KEYWORDS = [
    "转换", "转成", "导出", "生成csv", "生成CSV", "批量", "脚本",
    "保存为", "另存为", "提取到", "整理", "汇总成",
]

# 这类转换只需读取一个已上传的文本文件并生成一个新文件。把它们从通用 LLM
# 规划中提前取出，既能避免无关文档被逐个分析，也不会为一行转换任务付出规划模型
# 和“模型生成脚本”两次额外往返。
_DIRECT_TEXT_CONVERSION_SOURCES = {".csv", ".tsv", ".txt", ".json", ".xml", ".yaml", ".yml", ".log"}
# 无损文本导出只承诺目标为 .txt；CSV -> JSON / XLSX -> CSV 等带结构语义的
# 转换仍需由受限脚本按任务实现，不能把原始文本直接改后缀后交付。
_DIRECT_TEXT_CONVERSION_TARGETS = {".txt"}
_CONVERSION_RE = re.compile(
    r"(?is)(?:将|把)?\s*([^\s，。；;：:]+\.[a-z0-9]{1,10})\s*"
    r"(?:转换(?:成|为)?|转(?:成|为)|另存为|导出为|保存为)\s*"
    r"(?:文件\s*)?\.?([a-z0-9]{1,10})\b"
)
_NAMED_CONVERSION_RE = re.compile(
    r"(?is)(?:将|把)?\s*([^\s，。；;：:]+\.[a-z0-9]{1,10})\s*"
    r"(?:转换(?:成|为)?|转(?:成|为)|另存为|导出为|保存为)\s*"
    r"(?:文件\s*)?([^\s，。；;：:]+\.[a-z0-9]{1,10})\b"
)
_OUTPUT_FILENAME_RE = re.compile(
    r"(?iu)(?:保存为|另存为|输出为|导出为|生成(?:文件)?(?:名为)?|命名为)\s*"
    r"(?:文件\s*)?([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*\.[a-z0-9]{1,10})\b"
)


def _requested_txt_delimiter(request: str) -> str | None:
    """Return a supported explicit TXT delimiter, never a free-form value."""
    text = (request or "").casefold()
    if "制表符" in text or "tab 分隔" in text or "tab分隔" in text:
        return "\t"
    if "逗号分隔" in text or "comma 分隔" in text or "comma分隔" in text:
        return ","
    return None


def _requested_output_encoding(request: str) -> str | None:
    """Return a supported explicit output encoding, never a model-invented value."""
    text = (request or "").casefold().replace("_", "-")
    if "utf-8-sig" in text or "utf8-sig" in text or "带 bom" in text:
        return "utf-8-sig"
    if "utf-8" in text or "utf8" in text:
        return "utf-8"
    if "gb18030" in text:
        return "gb18030"
    return None


def _is_standalone_conversion(request: str, match: re.Match[str]) -> bool:
    """Allow M0 only when text after the conversion is a known format modifier.

    A conversion followed by "then query the current time" must reach the
    compound planner.  Treating it as M0 would silently discard the second
    action.  Conversely, delimiter and encoding are part of the same conversion
    contract and remain eligible for the deterministic path.
    """
    suffix = (request or "")[match.end():].casefold()
    suffix = re.sub(
        r"(?:使用|以|采用|并用|编码|格式|分隔符|utf-?8(?:-sig)?|gb18030|"
        r"制表符|tab\s*分隔|逗号分隔|comma\s*分隔|\s|[，,。；;：:、（）()]+)",
        "",
        suffix,
    )
    return not bool(re.search(r"[\w\u4e00-\u9fff]", suffix))


def _explicit_output_filename(request: str) -> str | None:
    """Extract an explicit deliverable name only from unambiguous command phrases."""
    match = _OUTPUT_FILENAME_RE.search(request or "")
    return Path(match.group(1)).name if match else None


def _output_filenames_in_request(request: str) -> set[str]:
    """Names explicitly used as deliverables, not as input-document references."""
    names = {_normalise_filename(name) for name in [_explicit_output_filename(request)] if name}
    named_conversion = _NAMED_CONVERSION_RE.search(request or "")
    if named_conversion:
        names.add(_normalise_filename(Path(named_conversion.group(2)).name))
    return names


def extract_output_contract(request: str, conversion: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile verifiable delivery requirements from the user instruction.

    This deliberately has a small allowlist.  It is an execution contract, not a
    second natural-language planner: only requirements that can be checked
    against the generated artifact are included.  Other prose requirements stay
    in ``task`` for the model and must not be reported as guaranteed.
    """
    output_filename = ""
    if isinstance(conversion, dict):
        output_filename = Path(str(conversion.get("output_filename") or "")).name
    output_filename = output_filename or (_explicit_output_filename(request) or "")
    target_extension = Path(output_filename).suffix.casefold() if output_filename else ""
    if isinstance(conversion, dict):
        target_extension = str(conversion.get("target_extension") or target_extension).casefold()

    delimiter = None
    if target_extension == ".txt":
        delimiter = _requested_txt_delimiter(request)
        if isinstance(conversion, dict):
            delimiter = conversion.get("text_delimiter") or delimiter
    encoding = _requested_output_encoding(request) if target_extension in _DIRECT_TEXT_CONVERSION_TARGETS else None
    contract: dict[str, Any] = {
        "version": 1,
        "requires_artifact": bool(output_filename),
        "expected_output_names": [output_filename] if output_filename else [],
    }
    if target_extension:
        contract["target_extension"] = target_extension
    if delimiter:
        contract["text_delimiter"] = delimiter
    if encoding:
        contract["encoding"] = encoding
    return contract
_FILENAME_RE = re.compile(
    r"(?iu)(?:^|[\s《“\"'：:，,])([a-z0-9_\-\u4e00-\u9fff][a-z0-9_.\-\u4e00-\u9fff]*\.[a-z0-9]{1,10})\b"
)


def _normalise_filename(value: str) -> str:
    """用于用户输入文件名与上传文件名的宽松比较，不返回或展示内部路径。"""
    return re.sub(r"[\s_\-]+", "", Path(value or "").name.casefold())


def _filename_match_score(requested: str, uploaded: str) -> float:
    """返回文件名匹配度；仅接受明确/高置信候选，避免误处理另一份文档。"""
    requested_name = _normalise_filename(requested)
    uploaded_name = _normalise_filename(uploaded)
    if not requested_name or not uploaded_name:
        return 0.0
    if requested_name == uploaded_name:
        return 1.0
    requested_path = Path(requested_name)
    uploaded_path = Path(uploaded_name)
    if requested_path.suffix != uploaded_path.suffix:
        return 0.0
    requested_stem = requested_path.stem.rstrip("s")
    uploaded_stem = uploaded_path.stem.rstrip("s")
    if requested_stem and requested_stem == uploaded_stem:
        return 0.94
    return SequenceMatcher(None, requested_name, uploaded_name).ratio()


def select_named_office_documents(request: str, office_docs: list[dict] | None) -> tuple[list[dict], list[str], bool]:
    """按用户明确写出的文件名选择文档。

    返回 ``(selected, unresolved, has_reference)``。文件名是操作对象，不是供模型
    检索的关键词；无法唯一定位时必须停止，不能把所有附件作为兜底输入。
    """
    output_names = _output_filenames_in_request(request)
    requested_names: list[str] = []
    for raw in _FILENAME_RE.findall(request or ""):
        name = Path(raw.strip()).name
        if _normalise_filename(name) in output_names:
            continue
        if name and name not in requested_names:
            requested_names.append(name)
    if not requested_names:
        return list(office_docs or []), [], False

    selected: list[dict] = []
    unresolved: list[str] = []
    seen_ids: set[str] = set()
    for requested_name in requested_names:
        candidates = [
            (_filename_match_score(requested_name, str(document.get("filename") or "")), document)
            for document in office_docs or []
            if document.get("doc_id") and document.get("filename")
        ]
        candidates = [(score, document) for score, document in candidates if score >= 0.86]
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates or (len(candidates) > 1 and candidates[1][0] >= candidates[0][0] - 0.04):
            unresolved.append(requested_name)
            continue
        doc_id = str(candidates[0][1]["doc_id"])
        if doc_id not in seen_ids:
            selected.append(candidates[0][1])
            seen_ids.add(doc_id)
    return selected, unresolved, True


def resolve_direct_text_conversion(request: str, office_docs: list[dict] | None) -> dict[str, Any] | None:
    """解析“将 a.csv 转为 txt”并唯一定位上传文件。

    不以文档正文/RAG 命中来猜测文件，因为格式转换只依赖二进制源文件。若文件名
    候选不唯一，则留给正常规划链路澄清，绝不批量处理所有上传文档。
    """
    named_match = _NAMED_CONVERSION_RE.search(request or "")
    match = named_match or _CONVERSION_RE.search(request or "")
    if not match or not office_docs:
        return None
    if not _is_standalone_conversion(request, match):
        return None
    requested_name = Path(match.group(1)).name
    source_ext = Path(requested_name).suffix.casefold()
    named_target = Path(match.group(2)).name if named_match else ""
    target_ext = Path(named_target).suffix.casefold() if named_target else f".{match.group(2).casefold().lstrip('.')}"
    if source_ext not in _DIRECT_TEXT_CONVERSION_SOURCES or target_ext not in _DIRECT_TEXT_CONVERSION_TARGETS:
        return None

    candidates: list[tuple[float, dict]] = []
    for document in office_docs:
        filename = str(document.get("filename") or "")
        doc_id = str(document.get("doc_id") or "")
        if not filename or not doc_id or Path(filename).suffix.casefold() != source_ext:
            continue
        score = _filename_match_score(requested_name, filename)
        if score >= 0.86:
            candidates.append((score, document))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, selected = candidates[0]
    # 两份近似命名文档时不擅自选择其中一份。
    if len(candidates) > 1 and candidates[1][0] >= best_score - 0.04:
        return None
    filename = str(selected["filename"])
    result = {
        "doc_id": str(selected["doc_id"]),
        "filename": filename,
        "target_extension": target_ext,
        "requested_filename": requested_name,
        "output_filename": named_target or f"{Path(filename).stem}{target_ext}",
    }
    delimiter = _requested_txt_delimiter(request)
    if delimiter:
        result["text_delimiter"] = delimiter
    encoding = _requested_output_encoding(request)
    if encoding:
        result["encoding"] = encoding
    return result

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
        if _has_semi_structure(req):
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
    if _has_semi_structure(req):
        return {"task_type": "semi_structured", "template": None}
    return {"task_type": "free", "template": None}
