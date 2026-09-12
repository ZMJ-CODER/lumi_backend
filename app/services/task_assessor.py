"""TaskAssessor：自然语言 → 严格 TaskProfile（LLM 优先，确定性保守兜底）。

设计要点：
  - Prompt 明确区分 USER_PROVIDED（已在上下文中）与 WORKSPACE（需读取）；
  - 只要求模型输出抽象能力 required_capabilities，不输出具体工具名；
  - 输出经严格 schema 校验（``lumi_orch.task_assessment.TaskProfile``）；
  - 解析失败/低置信度 → 确定性保守画像 + ``apply_confidence_policy``；
  - 该层不做路由决策（路由在 lumi_orch.execution_router）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from lumi_contracts.routing.task_profile import (
    ActionIntent as CanonicalActionIntent,
    Complexity as CanonicalComplexity,
    ConfidenceSource as CanonicalConfidenceSource,
    ExecutionTarget as CanonicalExecutionTarget,
    InfoSource as CanonicalInfoSource,
    IntentType as CanonicalIntentType,
    TargetScope as CanonicalTargetScope,
    TaskProfile as CanonicalTaskProfile,
)
from lumi_orch.task_assessment import TaskProfile, apply_confidence_policy

#: canonical 生产开关（默认关；关掉时本模块输出逐字不变，见 ``canonical_profile``）。
CANONICAL_FLAG = "TASK_PROFILE_CANONICAL"

_SIDE_EFFECT_WORDS = re.compile(
    r"(?iu)(?:保存|写入|修改|编辑|删除|移除|发送|邮件|提交|发布|部署|运行|执行|跑一下|安装|"
    r"create|write|save|edit|modify|delete|remove|send|submit|publish|deploy|run|execute|install)"
)
# 改/调整 类动词只有作用在本地目标（文件/配置/工作区）上才算写入副作用，
# 纯文本改写（“把这句话改得更正式”）不得被判为副作用。
_CHANGE_WORDS = re.compile(r"(?iu)(?:改成|改为|改一下|变更|调整|替换|设置|重命名|移动|复制)")
_LOCAL_TARGET = re.compile(
    r"(?iu)(?:工作区|项目|仓库|文件|目录|配置|config|路径|\.json\b|\.ya?ml\b|\.py\b|\.ts\b|\.md\b|\.txt\b)"
)
_DELETE_WORDS = re.compile(r"(?iu)(?:删除|移除|清空|delete|remove|rm\b)")
_SEND_WORDS = re.compile(r"(?iu)(?:发送|邮件|通知|短信|钉钉|企微|send|email|notify)")
_PUBLISH_WORDS = re.compile(r"(?iu)(?:发布|上线|部署|publish|deploy)")
_EXECUTE_WORDS = re.compile(r"(?iu)(?:运行|执行|跑一下|测试|命令|脚本|run|execute|test|shell)")
_EXTERNAL_WEB = re.compile(r"(?iu)(?:联网|网上|公开资料|搜索|检索|最新|实时|新闻|竞品|web|search|browse)")
_PRIVATE_SERVICE = re.compile(r"(?iu)(?:邮箱|收件箱|日历|日程|crm|内网|工单|第三方|api|接口)")
_WORKSPACE_HINT = re.compile(
    r"(?iu)(?:工作区|项目|仓库|代码库|workspace|project|repo|readme|config\.|\.py\b|\.json\b|\.yaml\b|\.ts\b)"
)
_RUNTIME_DECISION = re.compile(
    r"(?iu)(?:自行|自己判断|排查|诊断|修复|直到|反复|根据结果|遇到问题|动态|探索|研究一下|"
    r"investigate|diagnose|fix|explore)"
)
_MEMORY_HINT = re.compile(
    r"(?iu)(?:刚才|刚刚|上文|前面|之前|上次|继续|接着|刚才那份|刚刚那份|still|previous|earlier)"
)
_DEPENDENCY = re.compile(
    r"(?iu)(?:然后|之后|再|最后|先.+再|根据.+结果|逐个|批量|分别|多步|step|first.+then|after)"
)
_HIGH_RISK = re.compile(r"(?iu)(?:删除|清空|覆盖|格式化|drop|delete|rm\s+-rf|生产|线上)")
_CAPABILITY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?iu)(?:文档|pdf|word|ppt|excel|表格|附件|document)"), "DOCUMENT_READ"),
    (re.compile(r"(?iu)(?:修改|编辑|写入|保存|edit|write|modify)"), "DOCUMENT_EDIT"),
    (re.compile(r"(?iu)(?:联网|搜索|资料|研究|web|search|research)"), "WEB_RESEARCH"),
    (re.compile(r"(?iu)(?:邮件|发送|email|send)"), "EMAIL_SEND"),
    (re.compile(r"(?iu)(?:运行|执行|脚本|测试|run|execute|test)"), "CODE_EXECUTION"),
    (re.compile(r"(?iu)(?:工作区|项目|仓库|文件|workspace|project|file)"), "WORKSPACE_MANIPULATION"),
)

# ── 旧严格画像 → canonical TaskProfile 的**唯一**映射表 ─────────────────
# 只映射旧画像确实算得出、且语义一一对应的信号；算不出的字段留空并在 debug 留痕
# （见 ``to_canonical_profile``），绝不按字面猜。
#: 旧 ``side_effects`` → canonical 动作意图。``WRITE`` 无法区分新建/修改（旧画像
#: 不产出该区分），统一映射为 ``MODIFY``（其工具窗口是 ``workspace_write`` 的超集）。
_LEGACY_EFFECT_TO_ACTION: dict[str, CanonicalActionIntent] = {
    "WRITE": CanonicalActionIntent.MODIFY,
    "DELETE": CanonicalActionIntent.DELETE,
    "SEND": CanonicalActionIntent.SEND,
    "PUBLISH": CanonicalActionIntent.PUBLISH,
    "EXECUTE": CanonicalActionIntent.EXECUTE,
}
#: 旧复杂度档位 → canonical 复杂度（有损：M0/M1 都是 ATOMIC）。
_LEGACY_COMPLEXITY_TO_CANONICAL: dict[str, CanonicalComplexity] = {
    "M0": CanonicalComplexity.ATOMIC,
    "M1": CanonicalComplexity.ATOMIC,
    "M2": CanonicalComplexity.SEQUENTIAL,
    "M3": CanonicalComplexity.DYNAMIC,
}
#: 旧信息源名 → canonical（旧表的 ``EXTERNAL_WEB`` 在 canonical 里叫 ``PUBLIC_WEB``）。
_LEGACY_SOURCE_TO_CANONICAL: dict[str, CanonicalInfoSource] = {
    "USER_PROVIDED": CanonicalInfoSource.USER_PROVIDED,
    "CONVERSATION_MEMORY": CanonicalInfoSource.CONVERSATION_MEMORY,
    "INTERNAL_KNOWLEDGE": CanonicalInfoSource.INTERNAL_KNOWLEDGE,
    "WORKSPACE": CanonicalInfoSource.WORKSPACE,
    "EXTERNAL_WEB": CanonicalInfoSource.PUBLIC_WEB,
    "PRIVATE_SERVICE": CanonicalInfoSource.PRIVATE_SERVICE,
}
#: 旧执行位置 → canonical（有损：``BACKEND``/``EXTERNAL_SERVICE`` 都归 ``SERVER``）。
_LEGACY_EXECUTION_TARGET_TO_CANONICAL: dict[str, CanonicalExecutionTarget] = {
    "NONE": CanonicalExecutionTarget.NONE,
    "BACKEND": CanonicalExecutionTarget.SERVER,
    "DESKTOP": CanonicalExecutionTarget.DESKTOP,
    "SANDBOX": CanonicalExecutionTarget.SANDBOX,
    "EXTERNAL_SERVICE": CanonicalExecutionTarget.SERVER,
}
#: canonical 评估来源（``assess_task_profile`` 的 source）→ 置信度来源枚举。
_SOURCE_TO_CONFIDENCE: dict[str, CanonicalConfidenceSource] = {
    "llm": CanonicalConfidenceSource.LLM,
    "heuristic": CanonicalConfidenceSource.HEURISTIC,
}


@dataclass(slots=True)
class AssessmentContext:
    """入口可得的信号（不包含任何模型判定结果）。"""

    request: str = ""
    has_attachments: bool = False
    has_office_docs: bool = False
    workspace_id: str = ""
    workspace_bound: bool = False
    has_conversation_memory: bool = False
    web_search_enabled: bool = False
    rag_available: bool = False
    extra_sources: list[str] = field(default_factory=list)


def heuristic_profile(context: AssessmentContext) -> TaskProfile:
    """确定性保守画像：解析失败/无模型时的安全兜底（confidence 低以触发降级）。"""
    text = str(context.request or "")
    sources: list[str] = []
    if context.has_attachments or context.has_office_docs:
        sources.append("USER_PROVIDED")
    # 会话记忆只在请求确实回指上文时才算来源，避免“有会话”就升级多源任务。
    if context.has_conversation_memory and _MEMORY_HINT.search(text):
        sources.append("CONVERSATION_MEMORY")
    if context.rag_available:
        sources.append("INTERNAL_KNOWLEDGE")
    if _WORKSPACE_HINT.search(text) or context.workspace_id:
        sources.append("WORKSPACE")
    if _EXTERNAL_WEB.search(text) or context.web_search_enabled:
        sources.append("EXTERNAL_WEB")
    if _PRIVATE_SERVICE.search(text):
        sources.append("PRIVATE_SERVICE")
    if not sources:
        sources = ["USER_PROVIDED"]

    side_effects: list[str] = []
    if _DELETE_WORDS.search(text):
        side_effects.append("DELETE")
    if _SEND_WORDS.search(text):
        side_effects.append("SEND")
    if _PUBLISH_WORDS.search(text):
        side_effects.append("PUBLISH")
    if _EXECUTE_WORDS.search(text):
        side_effects.append("EXECUTE")
    writes_local_target = bool(_CHANGE_WORDS.search(text)) and bool(_LOCAL_TARGET.search(text))
    if (_SIDE_EFFECT_WORDS.search(text) or writes_local_target) and "EXECUTE" not in side_effects:
        side_effects.append("WRITE")

    runtime_decision = bool(_RUNTIME_DECISION.search(text))
    dependency = bool(_DEPENDENCY.search(text))
    external_read = bool(set(sources) & {"WORKSPACE", "EXTERNAL_WEB", "PRIVATE_SERVICE"})
    if runtime_decision:
        complexity = "M3"
    elif dependency:
        complexity = "M2"
    elif side_effects or external_read:
        complexity = "M1"
    else:
        complexity = "M0"

    if "EXECUTE" in side_effects:
        execution_target = "SANDBOX"
    elif "WORKSPACE" in sources and side_effects:
        execution_target = "DESKTOP"
    elif "EXTERNAL_WEB" in sources or "PRIVATE_SERVICE" in sources:
        execution_target = "BACKEND"
    elif side_effects:
        execution_target = "BACKEND"
    else:
        execution_target = "NONE"

    if "SEND" in side_effects or "PUBLISH" in side_effects:
        output_target = "EXTERNAL_SERVICE"
    elif "WORKSPACE" in sources and side_effects:
        output_target = "WORKSPACE"
    elif re.search(r"(?iu)(?:生成|整理成|写成|汇总|报告|表格|excel|文档|下载)", text):
        output_target = "DOWNLOAD_ARTIFACT"
    else:
        output_target = "CHAT"

    if _HIGH_RISK.search(text):
        risk_level = "HIGH_RISK"
    elif side_effects:
        risk_level = "REQUIRES_APPROVAL" if execution_target == "DESKTOP" else "REVERSIBLE"
    else:
        risk_level = "READ_ONLY"

    capabilities = [name for pattern, name in _CAPABILITY_HINTS if pattern.search(text)]
    return TaskProfile(
        complexity=complexity,
        confidence=0.4,  # 保守：低置信度触发 apply_confidence_policy
        intent_type="EXECUTE_ACTION" if side_effects else "GENERATE_ONLY",
        side_effects=side_effects,
        info_sources=sources,
        output_target=output_target,
        execution_target=execution_target,
        required_capabilities=sorted(set(capabilities)),
        path_determinism="UNKNOWN" if runtime_decision else "KNOWN",
        risk_level=risk_level,
        data_sensitivity="CREDENTIAL" if re.search(r"(?iu)(密码|密钥|token|credential|secret)", text) else "NORMAL",
        context_size_estimate="LARGE" if len(text) > 4000 else ("MEDIUM" if len(text) > 800 else "SMALL"),
    )


def _canonical_target_scope(
    sources: list[CanonicalInfoSource],
    context: AssessmentContext | None,
) -> CanonicalTargetScope:
    """目标范围：只用"信息源 / 附件"这两个入口事实，不从用户文本猜目标。"""
    if CanonicalInfoSource.WORKSPACE in sources:
        return CanonicalTargetScope.WORKSPACE
    if context is not None and (context.has_attachments or context.has_office_docs):
        return CanonicalTargetScope.ATTACHMENT
    if {CanonicalInfoSource.PUBLIC_WEB, CanonicalInfoSource.PRIVATE_SERVICE} & set(sources):
        return CanonicalTargetScope.EXTERNAL_SERVICE
    return CanonicalTargetScope.USER_INPUT


def to_canonical_profile(
    profile: TaskProfile,
    *,
    source: str = "heuristic",
    context: AssessmentContext | None = None,
) -> CanonicalTaskProfile:
    """旧严格画像 → canonical ``TaskProfile``（契约唯一权威定义，方案 §3.1）。

    只映射旧画像**确实算得出**的信号：

    * ``action_intents`` ← ``side_effects``（唯一映射表）+ ``WORKSPACE`` 信息源 → ``READ``
      （assessor prompt 明确"WORKSPACE = 需要读取本地项目/文件才可获得"）；
    * ``target_scope`` ← 信息源/附件；``required_capabilities`` ← 抽象能力原样保留；
    * ``has_runtime_decision`` ← ``complexity == "M3"``（旧画像里"需探索/自行判断/排查
      修复"是 M3 的**唯一**来源，见 heuristic_profile 与 assessor_prompt 规则 3）；
    * ``approval_required`` ← ``risk_level == "HIGH_RISK"``（明确的高风险信号）。

    算不出的字段**留空**而不是猜，并在 ``debug["unavailable"]`` 留痕：

    * ``target_clarity``：旧画像没有"目标是否已给出"的信号。唯一相关的
      ``path_determinism`` 被 ``apply_confidence_policy`` 对"低置信度 + 有副作用"的
      任务统一改写成 ``UNKNOWN``（策略标签，不是事实），据此判定会把每个写任务
      变成"目标未知"。目标澄清仍由入口 ``task_preflight`` 负责。
    * ``has_dependency``：启发式算了 ``dependency`` 但**没有写进画像**，M2 档位又被
      上述策略升级污染，无法还原。
    """
    canonical_sources: list[CanonicalInfoSource] = []
    unmapped_sources: list[str] = []
    for item in profile.info_sources or []:
        mapped = _LEGACY_SOURCE_TO_CANONICAL.get(str(item))
        if mapped is None:
            unmapped_sources.append(str(item))
        elif mapped not in canonical_sources:
            canonical_sources.append(mapped)
    if not canonical_sources:
        canonical_sources = [CanonicalInfoSource.USER_PROVIDED]

    intents: list[CanonicalActionIntent] = []
    unmapped_effects: list[str] = []
    for effect in profile.side_effects or []:
        action = _LEGACY_EFFECT_TO_ACTION.get(str(effect))
        if action is None:
            unmapped_effects.append(str(effect))
        elif action not in intents:
            intents.append(action)
    if CanonicalInfoSource.WORKSPACE in canonical_sources and CanonicalActionIntent.READ not in intents:
        intents.append(CanonicalActionIntent.READ)

    legacy_intent = str(profile.intent_type or "")
    # 动作意图非空 ⇒ 必须是 EXECUTE_ACTION（§3.2 硬约束：不许再落到只读直答）。
    intent_type = (
        CanonicalIntentType.EXECUTE_ACTION
        if intents or legacy_intent == "EXECUTE_ACTION"
        else CanonicalIntentType.GENERATE_ONLY
    )

    legacy_complexity = str(profile.complexity or "")
    complexity = _LEGACY_COMPLEXITY_TO_CANONICAL.get(legacy_complexity)
    runtime_decision = legacy_complexity == "M3"
    high_risk = str(profile.risk_level or "") == "HIGH_RISK"
    capabilities: list[str] = []
    for item in profile.required_capabilities or []:
        text = str(item or "").strip()
        if text and text not in capabilities:
            capabilities.append(text)

    if runtime_decision:
        reason_code = "assessor.runtime_decision"
    elif intents:
        reason_code = "assessor.side_effects"
    else:
        reason_code = "assessor.generate_only"

    debug: dict[str, Any] = {
        # 审计：只放枚举/原因码，绝不放用户原文。
        "canonical_from": "lumi_orch.task_assessment.TaskProfile",
        "assessor_source": str(source),
        "legacy_complexity": legacy_complexity,
        "legacy_intent_type": legacy_intent,
        "legacy_side_effects": [str(item) for item in (profile.side_effects or [])],
        "legacy_risk_level": str(profile.risk_level or ""),
        "has_runtime_decision_from": "complexity==M3",
        "approval_required_from": "risk_level==HIGH_RISK",
        # 没有信号、因此留空的字段（留痕，供影子比对与排障）。
        "unavailable": {
            "target_clarity": "旧画像无目标是否给出的信号；path_determinism 是低置信度策略标签",
            "has_dependency": "启发式的 dependency 未写入画像，M2 档位被策略升级污染",
        },
        "lossy": {
            "complexity": f"{legacy_complexity or '?'} → {str(complexity or 'ATOMIC')}",
            "execution_target": (
                f"{str(profile.execution_target or '')} → "
                f"{str(_LEGACY_EXECUTION_TARGET_TO_CANONICAL.get(str(profile.execution_target), CanonicalExecutionTarget.NONE))}"
            ),
            "side_effect_WRITE": "WRITE 无法区分新建/修改 → MODIFY",
        },
    }
    if unmapped_sources:
        debug["unmapped_info_sources"] = unmapped_sources
    if unmapped_effects:
        debug["unmapped_side_effects"] = unmapped_effects
    if complexity is None:
        debug["unknown_complexity"] = legacy_complexity
        complexity = CanonicalComplexity.ATOMIC

    return CanonicalTaskProfile(
        goal="",
        complexity=complexity,
        side_effects=bool(profile.side_effects),
        info_sources=canonical_sources,
        output_target=str(profile.output_target or ""),
        execution_target=_LEGACY_EXECUTION_TARGET_TO_CANONICAL.get(
            str(profile.execution_target), CanonicalExecutionTarget.NONE
        ),
        required_capabilities=capabilities,
        risk_level=str(profile.risk_level or "low"),
        confidence=float(profile.confidence or 0.0),
        debug=debug,
        intent_type=intent_type,
        action_intents=intents,
        target_scope=_canonical_target_scope(canonical_sources, context),
        has_runtime_decision=runtime_decision,
        approval_required=high_risk,
        confidence_source=_SOURCE_TO_CONFIDENCE.get(str(source), CanonicalConfidenceSource.HEURISTIC),
        decision_reason_code=reason_code,
    )


def _log_canonical_shadow_diff(old: CanonicalTaskProfile, new: CanonicalTaskProfile) -> None:
    """影子模式：一行记录 old→new 的差异（只有枚举/原因码，不含用户原文）。"""
    old_fp, new_fp = old.fingerprint(), new.fingerprint()
    changed = {
        key: [old_fp.get(key), new_fp.get(key)]
        for key in sorted(set(old_fp) | set(new_fp))
        if old_fp.get(key) != new_fp.get(key)
    }
    logger.info(
        "TaskProfile canonical shadow diff: {}",
        json.dumps(changed, ensure_ascii=False, sort_keys=True),
    )


def canonical_profile(
    context: AssessmentContext,
    *,
    source: str = "heuristic",
    legacy: Any = None,
) -> CanonicalTaskProfile:
    """canonical 画像的唯一生产入口（灰度 ``TASK_PROFILE_CANONICAL``）。

    * **开关关（默认）**：只做别名归一（``from_mapping``），旧视图，语义不新增；
    * **开 + 影子（``INTEGRATION_SHADOW_MODE``）**：记录 old/new ``fingerprint()``
      差异后**继续返回旧视图**（实际行为不变）；
    * **开**：返回按 §3.1 映射出的 canonical 画像。

    ``legacy`` 由调用方给出（例如 LLM 评估结果）；缺省用确定性启发式画像，
    保证预检等"计划前"的调用点**不额外发起模型调用**。
    """
    from app.core.feature_flags import feature_enabled, shadow_mode

    if legacy is None:
        legacy = apply_confidence_policy(heuristic_profile(context))
    old_view = CanonicalTaskProfile.from_mapping(legacy)
    if not feature_enabled(CANONICAL_FLAG):
        return old_view
    mapped = to_canonical_profile(legacy, source=source, context=context)
    if shadow_mode():
        _log_canonical_shadow_diff(old_view, mapped)
        return old_view
    return mapped


async def assess_canonical_task_profile(
    context: AssessmentContext,
    *,
    user_id: str = "",
    llm_api_key: str | None = None,
    llm_config: dict | None = None,
    use_llm: bool = True,
) -> tuple[CanonicalTaskProfile, str]:
    """canonical 生产者（LLM 优先，确定性兜底）：返回 ``(canonical画像, source)``。

    与 :func:`assess_task_profile` 共用同一评估结果与降级链，只在出口换成 canonical
    契约；开关关上时返回旧视图（等价于今天"只做别名归一"的 canonical 读法）。
    """
    legacy, source = await assess_task_profile(
        context,
        user_id=user_id,
        llm_api_key=llm_api_key,
        llm_config=llm_config,
        use_llm=use_llm,
    )
    return canonical_profile(context, source=source, legacy=legacy), source


def assessor_prompt(context: AssessmentContext) -> str:
    """Assessor Prompt：严格 schema + 关键区分（USER_PROVIDED vs WORKSPACE）。"""
    return (
        "你是任务画像评估器。只输出一个 JSON 对象，不要输出解释或 Markdown。\n"
        "字段（严格，不允许数组/单值混用）：\n"
        "complexity: M0|M1|M2|M3；confidence: 0..1；\n"
        "intent_type: GENERATE_ONLY|EXECUTE_ACTION；\n"
        'side_effects: ["WRITE"|"DELETE"|"SEND"|"EXECUTE"|"PUBLISH"] 的子集（无副作用用 []）；\n'
        'info_sources: ["INTERNAL_KNOWLEDGE"|"USER_PROVIDED"|"CONVERSATION_MEMORY"|"WORKSPACE"|'
        '"EXTERNAL_WEB"|"PRIVATE_SERVICE"] 的子集；\n'
        "output_target: CHAT|WORKSPACE|DOWNLOAD_ARTIFACT|EXTERNAL_SERVICE|LOCAL_APPLICATION（单值）；\n"
        "execution_target: NONE|BACKEND|DESKTOP|SANDBOX|EXTERNAL_SERVICE（单值）；\n"
        'required_capabilities: 抽象能力字符串数组（如 ["DOCUMENT_READ","CODE_EXECUTION"]），'
        "禁止填写具体工具名；\n"
        "path_determinism: KNOWN|UNKNOWN；estimated_steps: 整数（可选）；\n"
        "risk_level: READ_ONLY|REVERSIBLE|REQUIRES_APPROVAL|HIGH_RISK；\n"
        "data_sensitivity: NORMAL|PRIVATE|CREDENTIAL|PII；\n"
        "context_size_estimate: SMALL|MEDIUM|LARGE。\n\n"
        "判定规则：\n"
        "1) USER_PROVIDED = 用户已粘贴/已上传且内容已在上下文；WORKSPACE = 需要读取本地项目/文件才可获得，"
        "两者不可混用；\n"
        "2) 只要存在任何副作用（写/删/发/执行/发布），complexity 不得为 M0，且不得视为只读任务；\n"
        "3) 路径已知的多步骤（先读后改再跑）→ M2；需要探索/自行判断/排查修复 → M3；\n"
        "4) 纯文本生成/改写在已有材料内完成 → M0；单次外部只读或单次原子动作 → M1；\n"
        "5) 低置信度时请如实给出较低的 confidence。\n\n"
        f"输入信号：附件={context.has_attachments}，办公文档={context.has_office_docs}，"
        f"工作区绑定={bool(context.workspace_id) or context.workspace_bound}，"
        f"历史记忆={context.has_conversation_memory}，知识库={context.rag_available}，"
        f"允许联网={context.web_search_enabled}\n"
        f"用户请求：{context.request[:4000]}"
    )


def _sanitize_profile_payload(payload: Any) -> dict:
    """只保留严格 schema 字段，容错 LLM 常见偏差（单值/数组混用）。"""
    if not isinstance(payload, dict):
        return {}
    allowed = set(TaskProfile.model_fields)
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key not in allowed:
            continue
        clean[key] = value
    # 单值字段被模型写成数组时取首个元素。
    for single in ("output_target", "execution_target", "complexity", "intent_type",
                   "path_determinism", "risk_level", "data_sensitivity", "context_size_estimate"):
        if isinstance(clean.get(single), list) and clean[single]:
            clean[single] = clean[single][0]
    for key in ("side_effects", "info_sources", "required_capabilities"):
        value = clean.get(key)
        if isinstance(value, str):
            clean[key] = [value] if value else []
    return clean


async def assess_task_profile(
    context: AssessmentContext,
    *,
    user_id: str = "",
    llm_api_key: str | None = None,
    llm_config: dict | None = None,
    use_llm: bool = True,
) -> tuple[TaskProfile, str]:
    """返回 (profile, source)；source ∈ {"llm","heuristic"}，异常全部兜底。

    模型档位：**role=intent_assessor**（默认 cheap）。安全边界不变——
    低成本模型只负责"理解意图"：是否存在副作用、是否需要审批、是否越权仍由
    确定性规则判定（``lumi_orch.execution_router`` + Capability/Approval 链）。
    结构化输出不合法或模型不可用时，回退顺序是
    ``cheap → main（一次） → 确定性启发式画像``。
    """
    if use_llm:
        try:
            from app.agents.langchain.planning import invoke_json_object
            from app.core.model_roles import ROLE_INTENT_ASSESSOR

            payload = await invoke_json_object(
                assessor_prompt(context),
                user_id=user_id,
                api_key=llm_api_key,
                llm_config=llm_config,
                role=ROLE_INTENT_ASSESSOR,
            )
            profile = TaskProfile.model_validate(_sanitize_profile_payload(payload))
            return apply_confidence_policy(profile), "llm"
        except Exception as exc:  # noqa: BLE001 - 评估失败必须安全兜底
            logger.warning("TaskAssessor LLM 评估失败，使用确定性保守画像: {}", str(exc)[:200])
    return apply_confidence_policy(heuristic_profile(context)), "heuristic"


def assessor_profile_json(context: AssessmentContext) -> str:
    """调试/测试用：输出当前启发式画像 JSON。"""
    return json.dumps(heuristic_profile(context).model_dump(), ensure_ascii=False)


__all__ = [
    "CANONICAL_FLAG",
    "AssessmentContext",
    "assess_canonical_task_profile",
    "assess_task_profile",
    "assessor_prompt",
    "assessor_profile_json",
    "canonical_profile",
    "heuristic_profile",
    "to_canonical_profile",
]
