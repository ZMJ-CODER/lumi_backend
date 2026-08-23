"""通用规划路由意图。

这里识别的是请求的结构，而不是某个行业的实体。例如“天气”“合同”
和“计算器”都不应成为核心路由规则；它们分别只是网络、文档和应用的
不同对象。具体能力由 Skill/MCP 注册表提供，规划器只负责选择执行形态。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Any

from app.agents.orchestration.policy.lexicon import action_markers, object_markers


@dataclass(frozen=True)
class RoutingConfidence:
    """Separate confidence dimensions so a clear action cannot hide a vague target."""

    intent: float
    object: float
    context: float
    safety: float
    state: str = "known"

    @property
    def overall(self) -> float:
        return min(self.intent, self.object, self.context, self.safety)


@dataclass(frozen=True)
class ActionStep:
    """A normalized action in user order, before it becomes an executable node."""

    action: str
    position: int
    object_type: str | None = None
    risk_level: str = "read_only"


@dataclass(frozen=True)
class RouteIntent:
    actions: tuple[str, ...]
    objects: tuple[str, ...]
    source: str | None
    requires_network: bool
    requires_retrieval: bool
    requires_side_effect: bool
    has_multiple_actions: bool
    needs_clarification: bool
    confidence: float
    reason: str
    action_steps: tuple[ActionStep, ...] = ()
    confidence_detail: RoutingConfidence | None = None
    resolved_request: str = ""
    resolution_notes: tuple[str, ...] = ()
    risk_level: str = "read_only"
    requires_dynamic: bool = False


_ROUTE_ACTIONS = {
    "lookup_history", "converse", "send", "execute", "modify", "transform",
    "create", "analyze", "read", "query",
}
_ROUTE_OBJECTS = {
    "task_history", "external_resource", "application", "message", "document",
    "data", "task_result", "project",
}


_ACTION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("lookup_history", ("上次", "上回", "之前", "此前", "前面", "前文", "刚才那个", "刚才的结果", "刚才说的", "刚才那份", "当时", "历史记录", "那个结果", "之前的结果", "最后确认")),
    ("converse", ("你怎么看", "你觉得", "怎么看待", "聊聊", "说说", "谈谈")),
    ("send", ("发送", "发出", "转发", "发给", "发我", "send", "email", "e-mail", "forward")),
    # Keep the bare verb useful for commands, but exclude common nouns such as
    # “执行难度/执行成本”; substring matching must not turn analysis criteria
    # into a system-command risk.
    ("execute", ("打开", "启动", "运行", "执行", "访问", "关闭", "结束进程", "跑起来", "丢给测试环境", "run", "execute", "open")),
    ("modify", ("修改", "编辑", "删除", "移除", "更新", "保存", "上传", "下载", "改一下", "修一下", "修好", "edit", "delete", "update", "save")),
    ("transform", ("转换", "转成", "转为", "改成", "导出", "整理", "清洗", "合并", "拆分", "弄成", "做成", "压缩", "translate", "翻译", "convert", "format", "rewrite", "翻成")),
    ("create", ("创建", "新建", "制作", "生成", "写一份", "写个", "编写", "一份新的", "画个", "存成", "create", "make", "generate", "table", "markdown table")),
    ("analyze", ("分析", "判断", "对比", "比较", "比一下", "比一比", "比对", "放一起比", "总结", "概括", "提取", "提炼", "归纳", "判断问题", "问题在哪", "怎么回事", "对不对", "是不是", "有没有问题", "有没有坑", "风险", "哪里不对", "哪儿不对", "标一下", "标出来", "看着不太对", "探索", "规律", "summarize", "summary", "extract", "analyze", "compare", "review")),
    ("read", ("读取", "阅读", "查看", "解析", "看看", "看下", "看一下", "看一遍", "读完", "过一遍", "过一下", "瞅瞅", "说明内容", "read", "inspect", "look at")),
    ("query", ("查询", "查一下", "查查", "搜索", "搜一下", "检索", "了解", "解释", "说明", "告诉我", "列出", "找出", "找找", "找一下", "怎么看", "想知道", "会不会", "有没有", "能不能", "帮我确认", "催一下进度", "啥情况", "什么情况", "query", "search", "find out", "tell me")),
)

_OBJECT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("task_history", ("上次", "上回", "之前", "此前", "前面", "前文", "刚才那个", "刚才的结果", "刚才说的", "刚才那份", "当时", "历史记录", "那个结果", "最后确认")),
    ("external_resource", ("网页", "网址", "网上", "网络", "公开资料", "外部资料", "链接", "web", "website", "online", "internet")),
    ("application", ("应用", "软件", "程序", "浏览器", "计算器", "进程", "桌面")),
    ("message", ("消息", "通知", "收件人", "联系人", "message", "email", "recipient")),
    ("document", ("文档", "文件", "附件", "资料", "材料", "表格", "document", "file", "attachment", "spreadsheet", ".pdf", ".docx", ".xlsx", ".csv", ".txt")),
    ("data", ("数据", "记录", "内容", "字段", "信息", "东西", "data", "content", "fields", "release note")),
    ("task_result", ("结果", "日志", "报错", "错误", "异常", "返回", "空的", "乱码", "重复行", "字符串", "401", "404", "500", "missing_")),
    ("project", ("项目", "代码库", "仓库")),
)

# The checked-in tuples above remain a compatibility fallback for old
# deployments that have not mounted policy assets.  Normal deployments load
# the same bounded vocabulary from YAML at process start; the schema forbids
# new actions, objects, executable expressions and arbitrary imports.
try:
    _ACTION_MARKERS = action_markers()
    _OBJECT_MARKERS = object_markers()
except RuntimeError:
    pass

# 仅识别“公开外部来源/明确联网”意图。时间词、天气、价格等领域词本身
# 不足以证明需要 Tavily：它们可能指向用户自己的待办、附件或知识库。
_NETWORK_MARKERS = (
    "联网", "网上搜", "网页搜索", "搜索网页", "检索公开资料", "查网页", "给我来源",
    "搜索新闻", "最新新闻", "公开资料", "web search", "search the web", "browse the web",
)
_NETWORK_CONTEXT_MARKERS = ("公开网页", "外部网站", "互联网来源", "网页链接")
_RETRIEVAL_MARKERS = ("知识库", "上传的", "上传内容", "附件中", "资料中", "文档中", "文件中", "根据我的资料", "根据文档", "根据文件")
_MULTI_CONNECTORS = ("然后", "接着", "之后", "并且", "同时", "另外", "还要", "最后", "再", "以及", "then", "next", "after that", "and then", "finally", "also")
_VAGUE_REFERENTS = ("那个", "这份", "这个", "该文件", "它", "相关内容", "想要的样子")
_VAGUE_ACTIONS = ("处理一下", "处理下", "整理一下", "弄一下", "做成我想要的样子", "帮我处理")
_BARE_QUERY_COMMANDS = ("查询", "搜索", "检索")
_GREETING_MARKERS = ("你好", "嗨", "哈喽", "早上好", "晚上好", "在吗")
_FEEDBACK_MARKERS = (
    "结果不对", "跑出来不对", "又错了", "有问题", "乱码", "异常", "报错", "错误", "数字不对",
    "output is garbled", "garbled output", "wrong result", "something went wrong", "what went wrong",
    "error", "failed", "failure", "bug", "incorrect",
)
_IMPLICIT_HISTORY_MARKERS = ("也发给", "再发给", "同样发给", "改成", "它怎么", "它又", "图表里")
_DYNAMIC_MARKERS = (
    "探索一下", "探索", "根据结果", "能修就修", "按这个思路继续", "继续做下去",
    "try to fix", "try a low-risk fix", "if possible fix", "diagnose and fix",
    "figure out what went wrong", "determine what went wrong", "根据分析结果",
)
_CONDITIONAL_MARKERS = (
    "如果", "若", "要是", "否则", "不然", "根据结果", "满足条件", "超过", "低于", "达到",
    "if ", "unless ", "otherwise", "depending on", "when ", "exceeds", "below",
)


def _matches(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.casefold() in text.casefold() for marker in markers)


def _matches_action(text: str, action: str, markers: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    for marker in markers:
        marker_lower = marker.casefold()
        start = lowered.find(marker_lower)
        if start < 0:
            continue
        prefix = text[max(0, start - 6):start]
        if re.search(r"(?:不要|别|无需|不用|不需要|不必|暂时不|先不)[^，。；,，]{0,8}$", prefix):
            continue
        if action == "execute" and marker == "执行":
            # These are evaluation dimensions, not instructions to run a
            # program. Other explicit execute phrases remain fast-path hits.
            if any(phrase in text for phrase in ("执行难度", "执行成本", "执行力", "执行方案")):
                continue
        return True
    return False


def _action_steps(text: str, actions: list[str], objects: list[str]) -> tuple[ActionStep, ...]:
    """Keep action order instead of the declaration order of the marker table."""
    positions: list[ActionStep] = []
    for action in actions:
        marker_positions = [
            text.casefold().find(marker.casefold())
            for name, markers in _ACTION_MARKERS
            if name == action
            for marker in markers
            if text.casefold().find(marker.casefold()) >= 0
        ]
        if marker_positions:
            position = min(marker_positions)
        elif action == "lookup_history" and any(token in text for token in ("也发给", "再发给", "同样发给", "继续")):
            position = 0
        else:
            position = len(text) + len(positions)
        risk = "external_send" if action == "send" else "system_command" if action == "execute" else "write" if action in {"modify", "transform"} else "read_only"
        positions.append(ActionStep(action=action, position=position, object_type=objects[0] if objects else None, risk_level=risk))
    return tuple(sorted(positions, key=lambda item: (item.position, item.action)))


def _resolve_references(
    text: str,
    *,
    office_docs: list[dict] | None,
    prior_summaries: str,
) -> tuple[str, tuple[str, ...], bool]:
    """Resolve only safe, unique references; unresolved references stay explicit."""
    docs = office_docs or []
    notes: list[str] = []
    resolved = text
    has_context = bool(docs or prior_summaries.strip())
    if "也发给" in text or "再发给" in text or "同样发给" in text:
        if has_context:
            notes.append("send_target_inherited_from_recent_context")
        else:
            notes.append("send_target_context_missing")
    if "改成" in text and has_context:
        notes.append("transform_input_inherited_from_recent_context")
    if any(marker in text for marker in ("它怎么", "它又", "图表里")):
        if has_context:
            notes.append("feedback_subject_inherited_from_recent_context")
        else:
            notes.append("feedback_subject_context_missing")
    if len(docs) == 1:
        filename = str(docs[0].get("filename") or "").strip()
        if filename and any(token in text for token in ("这个文件", "这份文件", "它")):
            notes.append(f"unique_document:{filename}")
            resolved = resolved.replace("这个文件", f"文件《{filename}》").replace("这份文件", f"文件《{filename}》")
    if has_context and any(marker in text for marker in _IMPLICIT_HISTORY_MARKERS + ("继续",)):
        # Keep the original wording while making the inherited context an
        # explicit, bounded input for the dynamic worker.
        context_hint = prior_summaries.strip()[:3000]
        if context_hint:
            resolved = f"{text}\n[仅引用最近上下文，不新增用户指令]\n{context_hint}"
    return resolved, tuple(notes), has_context


def infer_route_intent(
    request: str,
    office_docs: list[dict] | None = None,
    prior_summaries: str = "",
) -> RouteIntent:
    """从自然语言提取可解释的通用路由特征。

    这是确定性脚手架，不是完整语义解析器。低置信度结果必须澄清，不能
    伪装成知识库查询；复杂请求交给受限动态执行节点或后续 LLM 规划。
    """
    text = (request or "").strip()
    resolved_request, resolution_notes, has_context = _resolve_references(
        text, office_docs=office_docs, prior_summaries=prior_summaries,
    )
    actions: list[str] = []
    for action, markers in _ACTION_MARKERS:
        if _matches_action(text, action, markers):
            actions.append(action)
    if _matches(text, _GREETING_MARKERS) and "converse" not in actions:
        actions.append("converse")
    if any(marker in text for marker in ("也发给", "再发给", "同样发给")) and has_context and "lookup_history" not in actions:
        actions.append("lookup_history")
    if any(marker in text for marker in ("按这个思路继续", "继续做下去", "继续")) and has_context and "lookup_history" not in actions:
        actions.append("lookup_history")
    objects: list[str] = []
    for obj, markers in _OBJECT_MARKERS:
        if _matches(text, markers):
            objects.append(obj)
    if _matches(text, _FEEDBACK_MARKERS) and "task_result" not in objects:
        objects.append("task_result")

    has_question_shape = bool(re.search(r"[?？]|吗[？?]?$|什么|多少|为何|为什么|如何|怎样|怎么|是否", text))
    if has_question_shape and "query" not in actions:
        actions.append("query")
    if "lookup_history" in actions and "task_history" not in objects:
        objects.append("task_history")

    requires_network = bool(
        "query" in actions
        and (_matches(text, _NETWORK_MARKERS) or _matches(text, _NETWORK_CONTEXT_MARKERS) or "external_resource" in objects)
    )
    requires_retrieval = bool(
        _matches(text, _RETRIEVAL_MARKERS)
        or ("document" in objects and any(action in actions for action in ("read", "analyze")))
    )
    # 仅把会改变外部状态或需要真正调用外部系统的动作标记为副作用。
    # “创建一篇说明”仍可由普通生成能力完成，不应自动获得写权限。
    requires_side_effect = bool(
        any(action in actions for action in ("send", "execute", "modify"))
        or _matches(text, ("发消息", "调用系统", "操作系统"))
    )
    has_feedback = "task_result" in objects
    # Conditional branches and repair requests cannot be compiled safely from
    # the initial text: the next action depends on an intermediate result.
    # Route them to the bounded dynamic runner even when only one lexical
    # action was recognized.
    requires_dynamic = _matches(text, _DYNAMIC_MARKERS) or _matches(text, _CONDITIONAL_MARKERS) or has_feedback
    # “看下现在什么情况”会同时命中 read + query，但它仍是一条查询链路；
    # 只有不同的执行动作才算多步骤，避免把网络查询误送进 ReAct。
    execution_actions = [action for action in actions if action not in {"read", "query", "converse"}]
    action_count = len(execution_actions)
    # 多个文件/对象即使只写了一个动词，也需要保留多目标上下文，不能退化为
    # 单次知识检索（例如“对比 A.pdf 和 B.pdf”）。
    multiple_targets = (
        len(re.findall(r"\.[a-z0-9]{1,10}", text, flags=re.IGNORECASE)) >= 2
        or bool(re.search(r"(?:两|2|多个|几份|几个|几 个).{0,8}(?:文件|文档|材料|资料|附件)", text))
    )
    has_multiple_actions = action_count >= 2 or multiple_targets or (
        action_count >= 1 and _matches(text, _MULTI_CONNECTORS)
    )

    docs = office_docs or []
    vague_reference = _matches(text, _VAGUE_REFERENTS)
    vague_goal = _matches(text, _VAGUE_ACTIONS)
    context_reference_missing = any(note.endswith("context_missing") for note in resolution_notes)
    needs_clarification = bool(
        (vague_reference and "document" in objects and len(docs) != 1)
        or (vague_goal and (
            not any(action not in {"query", "read"} for action in actions)
            or (vague_reference and "document" in objects and len(docs) != 1)
        ))
        or (vague_reference and "send" in actions and not docs and "lookup_history" not in actions)
        or context_reference_missing
        or (not objects and "query" in actions and "converse" not in actions and len(text) < 8 and text not in _BARE_QUERY_COMMANDS)
        or (not actions and not objects and not text)
        or (not actions and not objects and len(text) < 4)
    )

    if needs_clarification:
        reason = "请求包含未解析的指代或缺少可执行目标"
    elif has_multiple_actions:
        reason = "检测到多个动作或动作连接词，应保留中间结果并动态编排"
    elif requires_side_effect:
        reason = "请求涉及外部状态变化，应交给受限执行能力并按权限确认"
    elif requires_network:
        reason = "请求包含网络/时效性来源"
    elif requires_retrieval:
        reason = "请求明确引用用户资料或文档内容"
    elif "query" in actions:
        reason = "请求是一个可直接回答的查询"
    else:
        reason = "未识别到足够高置信度的执行意图"

    confidence_detail = RoutingConfidence(
        intent=0.9 if actions else 0.2,
        object=0.9 if objects or len(office_docs or []) == 1 or (actions and not any(action in actions for action in ("send", "modify", "execute", "transform"))) else 0.45,
        context=0.9 if not resolution_notes or has_context else 0.35,
        safety=0.85 if not requires_side_effect else (0.55 if needs_clarification else 0.9),
        state="ambiguous" if needs_clarification else "known" if actions else "unknown",
    )
    confidence = confidence_detail.overall
    if needs_clarification:
        confidence = min(confidence, 0.45)
    action_steps = _action_steps(text, actions, objects)
    risk_level = "external_send" if "send" in actions else "system_command" if "execute" in actions else "write" if any(action in actions for action in ("modify", "transform")) else "read_only"
    return RouteIntent(
        actions=tuple(actions),
        objects=tuple(objects),
        source=("network" if requires_network else "uploaded_material" if requires_retrieval else "task_history" if "lookup_history" in actions else "task_result" if has_feedback else None),
        requires_network=requires_network,
        requires_retrieval=requires_retrieval,
        requires_side_effect=requires_side_effect,
        has_multiple_actions=has_multiple_actions,
        needs_clarification=needs_clarification,
        confidence=min(confidence, 1.0),
        reason=reason,
        action_steps=action_steps,
        confidence_detail=confidence_detail,
        resolved_request=resolved_request,
        resolution_notes=resolution_notes,
        risk_level=risk_level,
        requires_dynamic=requires_dynamic,
    )


def should_use_llm_route_fallback(intent: RouteIntent, request: str) -> bool:
    """Return whether the deterministic router needs a long-tail classifier.

    This is deliberately conservative: the fallback is for missing/weak intent
    signals, not for silently resolving an unsafe or unresolved target.
    """
    text = (request or "").strip()
    if not text or intent.needs_clarification and intent.requires_side_effect:
        return False
    # Purely foreign-language requests use the classifier as a semantic
    # fallback even when a few English verbs happen to match the lexical
    # table. Mixed-language requests retain their deterministic Chinese
    # scaffolding and can still be routed without an extra model round trip.
    if re.search(r"[A-Za-z]{4}", text) and not re.search(r"[\u4e00-\u9fff]", text):
        return True
    if not intent.actions:
        return True
    if intent.confidence < 0.7:
        return True
    if any(action in intent.actions for action in ("send", "execute", "modify", "transform")) and not intent.objects:
        return True
    # Long colloquial phrases can contain a weak verb that the marker table did
    # not recognize even when another object marker was found.
    return len(text) >= 24 and intent.confidence <= 0.85


async def classify_route_with_llm(
    request: str,
    *,
    user_id: str,
    api_key: str | None,
    prior_summaries: str = "",
    office_docs: list[dict] | None = None,
    llm_config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Classify only the execution shape for long-tail language.

    The model is intentionally not given tool names and cannot create a plan.
    Its output is normalized and later passed through the same deterministic
    safety, static compilation, and approval gates as rule-based routing.
    """
    effective_api_key = (api_key or (llm_config or {}).get("api_key") or "").strip()
    if not effective_api_key:
        # Resolve through the same dynamic user/scene/global configuration used
        # by get_chat_model. A request-level key always wins; a missing user key
        # falls back to the configured office/default key.
        try:
            from app.core.llm_config import get_llm_config

            cfg = await get_llm_config("office", user_id=user_id)
            effective_api_key = str(cfg.get("api_key") or "").strip()
            if not effective_api_key:
                cfg = await get_llm_config(None, user_id=None)
                effective_api_key = str(cfg.get("api_key") or "").strip()
        except Exception:
            effective_api_key = ""
    if not effective_api_key:
        return None
    from app.agents.langchain.planning import invoke_json_object

    doc_names = [str(item.get("filename") or "") for item in (office_docs or [])]
    prompt = (
        "你是通用任务路由分类器，只判断用户请求的执行形态，不执行任务。\n"
        "只返回一个 JSON 对象，不要 Markdown、解释或额外文本。\n"
        "actions 只能从 [lookup_history, converse, send, execute, modify, "
        "transform, create, analyze, read, query] 选择，按用户意图顺序排列；"
        "objects 只能从 [task_history, external_resource, application, message, "
        "document, data, task_result, project] 选择。\n"
        "requires_dynamic 仅在下一步动作类型必须由中间结果决定时为 true；"
        "needs_clarification 仅在缺少关键对象/收件人/目标时为 true。"
        "不要因为不知道行业实体就澄清，行业实体属于对象内容而非路由类别。\n"
        "JSON 字段：actions(list), objects(list), requires_dynamic(bool), "
        "needs_clarification(bool), confidence(0到1数字), reason(短字符串)。\n"
        f"用户请求：{request[:4000]}\n"
        f"近期文件：{', '.join(doc_names[:8]) or '无'}\n"
        f"近期上下文（仅供指代消解）：{prior_summaries[:1200] or '无'}"
    )
    try:
        kwargs = {"user_id": user_id, "api_key": effective_api_key, "max_tokens": 350}
        if llm_config is not None:
            kwargs["llm_config"] = llm_config
        value = await invoke_json_object(prompt, **kwargs)
    except Exception as exc:
        from app.agents.skills.recovery import classify_model_error, is_terminal_model_error_code

        code, message = classify_model_error(exc)
        # The classifier is an optional routing hint. A generic connection
        # error here may safely fall back to deterministic routing; the first
        # required planner/worker call still enforces the terminal policy.
        if is_terminal_model_error_code(code) and code != "MODEL_UNAVAILABLE":
            from app.agents.orchestration.planner import PlannerModelError

            raise PlannerModelError(code, message) from exc
        return None
    if not isinstance(value, dict):
        return None
    actions = [str(item) for item in value.get("actions", []) if str(item) in _ROUTE_ACTIONS]
    objects = [str(item) for item in value.get("objects", []) if str(item) in _ROUTE_OBJECTS]
    if not actions and not objects:
        return None
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "actions": tuple(dict.fromkeys(actions)),
        "objects": tuple(dict.fromkeys(objects)),
        "requires_dynamic": bool(value.get("requires_dynamic")),
        "needs_clarification": bool(value.get("needs_clarification")),
        "confidence": confidence,
        "reason": str(value.get("reason") or "长尾语言由轻量分类器识别"),
    }


def merge_llm_route_intent(intent: RouteIntent, candidate: dict[str, Any], request: str) -> RouteIntent:
    """Merge a validated classifier candidate without weakening safety gates."""
    actions = tuple(candidate.get("actions") or intent.actions)
    objects = tuple(dict.fromkeys((*intent.objects, *(candidate.get("objects") or ()))))
    # Existing rule signals remain authoritative for source, retrieval, network,
    # and side effects. The classifier can only add a missing execution shape.
    side_effect = intent.requires_side_effect or any(
        action in actions for action in ("send", "execute", "modify")
    )
    dynamic = intent.requires_dynamic or bool(candidate.get("requires_dynamic"))
    action_steps = _action_steps(request, list(actions), list(objects))
    risk_level = (
        "external_send" if "send" in actions else
        "system_command" if "execute" in actions else
        "write" if any(action in actions for action in ("modify", "transform")) else
        intent.risk_level
    )
    model_confidence = float(candidate.get("confidence") or 0.0)
    base_confidence = intent.confidence_detail or RoutingConfidence(0.2, 0.45, 0.35, 0.55, "unknown")
    newly_side_effectful = side_effect and not intent.requires_side_effect
    missing_send_target = (
        "send" in actions
        and not any(item in objects for item in ("message", "document", "data", "task_result"))
        and "lookup_history" not in actions
    )
    missing_execute_target = "execute" in actions and not objects
    # A typed target is enough to route into the approval-gated worker. An
    # untyped side effect remains low-confidence and must be clarified.
    safety = (
        min(base_confidence.safety, 0.55)
        if newly_side_effectful and (missing_send_target or missing_execute_target)
        else base_confidence.safety
    )
    confidence_detail = replace(
        base_confidence,
        intent=max(intent.confidence_detail.intent if intent.confidence_detail else 0.2, model_confidence),
        object=max(base_confidence.object, model_confidence) if objects else base_confidence.object,
        safety=safety,
        state="ambiguous" if intent.needs_clarification or candidate.get("needs_clarification") or missing_send_target or missing_execute_target else "known",
    )
    confidence = confidence_detail.overall
    # A model cannot turn an unresolved/high-risk request into an auto-approved
    # request. Keep existing clarification and context confidence intact.
    needs_clarification = (
        intent.needs_clarification
        or bool(candidate.get("needs_clarification"))
        or missing_send_target
        or missing_execute_target
    )
    if needs_clarification:
        confidence = min(confidence, 0.45)
    reason = str(candidate.get("reason") or intent.reason)
    return replace(
        intent,
        actions=actions,
        objects=objects,
        requires_side_effect=side_effect,
        has_multiple_actions=len([a for a in actions if a not in {"read", "query"}]) >= 2 or intent.has_multiple_actions,
        needs_clarification=needs_clarification,
        confidence=max(0.0, min(1.0, confidence)),
        reason=reason,
        action_steps=action_steps,
        confidence_detail=confidence_detail,
        risk_level=risk_level,
        requires_dynamic=dynamic,
    )


def clarification_for_intent(intent: RouteIntent, request: str, office_docs: list[dict] | None = None) -> str:
    """生成不带行业假设的澄清问题。"""
    docs = office_docs or []
    if "document" in intent.objects and len(docs) != 1:
        if docs:
            names = "、".join(str(item.get("filename") or "未命名文件") for item in docs[:5])
            return f"请明确要处理哪一份文件（当前可选：{names}）。"
        return "请上传或明确要处理的文件，并说明希望执行的操作。"
    if not intent.actions:
        return "请说明希望我完成什么动作，以及作用对象或资料来源。"
    return "请补充明确的目标、对象或期望输出，我再为你安排执行步骤。"
