"""Model Capability Router：把**能力降级决策表**接到模型选择上（灰度 ``MODEL_CAPABILITY_ROUTER_V2``）。

定位（与用户已拍板的职责边界一致）：

* 候选**只**来自两处——``app/core/model_catalog.py``（模型注册表：上下文/多模态）
  与 ``.env``（含管理员动态覆盖）配置出来的 ``main`` / ``cheap`` / ``reasoning`` / ``vision``
  档位；解析走 ``app/core/model_roles.py``，任务内一律以 ``app/core/model_plan.py`` 的
  **冻结计划**为准（不新造第二条选择路径）；
* **BYOK 用户模型不参与系统默认切换**：命中 BYOK 时结论为 ``BYOK_PINNED``、
  ``switch_allowed=False``，用户模型本身按 ``BYOK`` 记进 ``excluded[]``；
* 能力过滤用冻结的 :func:`lumi_contracts.routing.model_capability.decide_degradation`
  （工具/视觉/长上下文/流式）；**工具能力缺失是硬阻断**（绝不静默降级成纯文本瞎答）；
* 能力允许时优先低成本档位；主档位不可用时降级到最便宜的可用候选；
* **Capability Broker 不参与选模型**：它只回答"哪个 Provider 提供某能力"，
  本模块不调用 Broker，也不把 Broker 结论当成模型结论。

结构化结论（**前端冻结契约**，写入 ``job.routing["model_routing"]``；
``src/services/modelPlan.js::describeModelRouting`` 就是按这些名字读的）::

    {
      "degraded": bool,              # 是否发生了切换/有损降级/阻断
      "action": str,                 # NONE|AUTO_SWITCH|PRECOMPRESS|EXTRACT_FRAMES|SIMULATE_STREAM|BLOCK
      "reason_code": str,            # 稳定码（无变化时为 ""）
      "safe_message": str,           # 用户可见结论（前端优先展示，不自造文案）
      "from_profile": str,           # 计划里的档位（main/cheap/reasoning/vision）
      "to_profile": str,             # 实际选用的档位
      "from_model": str,
      "to_model": str,
      "excluded": [{"model": str, "profile": str, "reason_code": str}],
      "missing_capabilities": [str],  # tools|vision|video|long_context|streaming
      "switch_allowed": bool,
      # ── 保留既有 DegradationDecision 信息（additive，不改名）──
      "decision_reason_code": str,    # decide_degradation 的原生 reason_code
      "target_model": str,
      "target_profile": str,          # 决策表里是能力画像对象，契约里扁平化为档位名
      "lossy": bool,
      "process_notice": str,
      "details": {...},
      # ── 失败/阻断（沿用冻结错误码）──
      "error_code": str,              # UnifiedError.code（如 CAPABILITY_UNAVAILABLE）
      "error_message": str,           # UnifiedError.safe_message
      "blocked": bool,
      "byok": bool,
    }

``excluded[]`` 只收录**被拒绝**的候选（BYOK / UNHEALTHY / LACKS_TOOLS / LACKS_VISION /
LACKS_STREAMING / CONTEXT_TOO_SMALL），前端把它折叠成"候选为什么没被选"，
与前端词表 ``MODEL_EXCLUSION_LABELS`` 一致。

**``excluded[]`` 的展示时机**：只有当结论真的做了一次决策时才有内容——即
``degraded``（换档/有损/阻断）或 ``switch_allowed=False``（BYOK 钉住、阻断）；
没有决策时（``action=NONE`` 且档位未变、``degraded=False``、``switch_allowed=True``）
**键仍在但恒为空数组**，否则前端会渲染"被排除的候选模型(n)"，让人误以为发生了降级。
影子/排障调用方（``route(shadow=True)`` 或 ``INTEGRATION_SHADOW_MODE``）保留这份诊断明细，
但它**不改执行路径**（见 :meth:`ModelCapabilityRouter._apply_excluded_policy`）。

切换的落点：:meth:`ModelCapabilityRouter.refine_roles` 只把**没有工具/审批依赖**
（``ROLE_FALLBACK == "none"``）且当前就在 ``from_profile`` 上的角色改指到 ``to_profile``；
``tool_*`` / ``planner_*`` / ``code_*`` 这类角色保持原档位（它们的能力需求不在任务级需求里，
不能被降级）。改写发生在**计划冻结时**，因此任务执行期间计划仍然不变。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from lumi_contracts import (
    DegradationAction,
    ModelCapabilityProfile,
    ModalityRequest,
    decide_degradation,
    from_role_capabilities,
)
from lumi_contracts.events.errors import translate_error

from app.core import model_roles
from app.core.model_plan import ModelPlan, profile_user_label

#: 灰度开关（默认关闭；关闭时调用方必须逐字走旧路径）。
FLAG = "MODEL_CAPABILITY_ROUTER_V2"

#: 结构化结论在 ``job.routing`` 里的键（前端 ``describeModelRouting`` 的入口）。
MODEL_ROUTING_KEY = "model_routing"
#: 切换/降级发生时的 process 事件载荷（与 ``preflight_notice`` 同机制）。
MODEL_ROUTING_NOTICE_KEY = "model_routing_notice"
#: 稳定去重键：SSE 实时帧与刷新投影（``app/contracts/process_log.py``）合并成同一行。
MODEL_ROUTING_NOTICE_ENTRY_ID = "process:model_routing"
MODEL_ROUTING_NOTICE_TITLE = "模型路由"

#: 结论的**精确键集**（测试钉死；新增键必须同时改前端契约与本 docstring）。
MODEL_ROUTING_KEYS: tuple[str, ...] = (
    "degraded",
    "action",
    "reason_code",
    "safe_message",
    "from_profile",
    "to_profile",
    "from_model",
    "to_model",
    "excluded",
    "missing_capabilities",
    "switch_allowed",
    "decision_reason_code",
    "target_model",
    "target_profile",
    "lossy",
    "process_notice",
    "details",
    "error_code",
    "error_message",
    "blocked",
    "byok",
)

#: ``excluded[]`` 条目的精确键集。
EXCLUDED_ENTRY_KEYS: tuple[str, ...] = ("model", "profile", "reason_code")

#: 任务锚点角色：路由回答的是"这个角色用哪个档位/模型"（最终回答 = 任务主模型）。
PROFILE_ANCHOR_ROLES: dict[str, str] = {
    model_roles.PROFILE_MAIN: model_roles.ROLE_DIRECT_ANSWER,
    model_roles.PROFILE_CHEAP: model_roles.ROLE_TITLE,
    model_roles.PROFILE_REASONING: model_roles.ROLE_TOOL_EXECUTE,
    model_roles.PROFILE_VISION: model_roles.ROLE_VISION,
}

#: 成本序（小 = 便宜）：能力允许时优先取小者。
PROFILE_PRICE_RANK: dict[str, int] = {
    model_roles.PROFILE_CHEAP: 0,
    model_roles.PROFILE_MAIN: 10,
    model_roles.PROFILE_REASONING: 20,
    model_roles.PROFILE_VISION: 30,
}
UNKNOWN_PRICE_RANK = 50

#: 决策表原生 reason_code → 前端 ``MODEL_EXCLUSION_LABELS`` 词表。
_DECISION_EXCLUSION: dict[str, str] = {
    "MODEL_TOOLS_UNSUPPORTED": "LACKS_TOOLS",
    "MODEL_MODALITY_UNSUPPORTED": "LACKS_VISION",
}

#: 能力名 → 用户可见说法（不含模型名/档位内部命名）。
_CAPABILITY_TEXT: dict[str, str] = {
    "tools": "工具调用",
    "vision": "视觉输入",
    "video": "视频输入",
    "long_context": "长上下文",
    "streaming": "流式输出",
}

#: 候选被拒绝的原因码 → 切换原因码（锚点不可用/不达标时用）。
_REJECT_TO_SWITCH_REASON: dict[str, str] = {
    "CONTEXT_TOO_SMALL": "CONTEXT_TOO_LARGE",
    "UNHEALTHY": "PRIMARY_UNAVAILABLE",
}


@dataclass(frozen=True, slots=True)
class CapabilityRequirements:
    """任务级能力需求（来自入口事实 + 附件；不含业务判断）。"""

    needs_tools: bool = False
    needs_vision: bool = False
    needs_streaming: bool = False
    #: 真 = 不支持流式就换候选；假 = 允许 ``SIMULATE_STREAM``（协议不变、首字变慢）。
    strict_streaming: bool = False
    min_context_tokens: int = 0
    image_count: int = 0
    max_image_size_bytes: int = 0
    wants_video: bool = False
    video_duration_seconds: int = 0
    target: str = ""

    def capability_names(self) -> tuple[str, ...]:
        names: list[str] = []
        if self.needs_tools:
            names.append("tools")
        if self.needs_vision:
            names.append("vision")
        if self.wants_video:
            names.append("video")
        if self.min_context_tokens:
            names.append("long_context")
        if self.strict_streaming and self.needs_streaming:
            names.append("streaming")
        return tuple(names)

    def to_modality_request(self) -> ModalityRequest:
        modalities = ["text"]
        if self.needs_vision:
            modalities.append("image")
        if self.wants_video:
            modalities.append("video")
        return ModalityRequest(
            modalities=tuple(modalities),
            image_count=int(self.image_count),
            max_image_size_bytes=int(self.max_image_size_bytes),
            video_duration_seconds=int(self.video_duration_seconds),
            needs_tools=bool(self.needs_tools),
            needs_streaming=bool(self.needs_streaming),
            target=str(self.target or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "needs_tools": bool(self.needs_tools),
            "needs_vision": bool(self.needs_vision),
            "needs_streaming": bool(self.needs_streaming),
            "strict_streaming": bool(self.strict_streaming),
            "min_context_tokens": int(self.min_context_tokens),
            "capabilities": list(self.capability_names()),
        }


def missing_for_capability(
    capability: ModelCapabilityProfile, requirements: CapabilityRequirements
) -> list[str]:
    """能力画像相对需求的缺口（顺序固定：tools → vision → video → long_context → streaming）。"""
    missing: list[str] = []
    if requirements.needs_tools and not capability.supports_tools:
        missing.append("tools")
    if requirements.needs_vision and not capability.accepts("image"):
        missing.append("vision")
    if requirements.wants_video and not capability.accepts("video"):
        missing.append("video")
    if requirements.min_context_tokens and capability.max_context_tokens < int(
        requirements.min_context_tokens
    ):
        missing.append("long_context")
    if (
        requirements.strict_streaming
        and requirements.needs_streaming
        and not capability.supports_streaming
    ):
        missing.append("streaming")
    return missing


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """一个候选模型（档位配置或注册表条目；**不含密钥**）。"""

    profile: str = ""
    model: str = ""
    provider: str = ""
    source: str = ""
    capability: ModelCapabilityProfile | None = None
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    timeout: float = 0.0
    available: bool = True
    byok: bool = False
    price_rank: int = UNKNOWN_PRICE_RANK
    origin: str = "profile"

    def capability_profile(self) -> ModelCapabilityProfile:
        """能力画像（显式画像优先，否则由档位能力旗标适配——唯一适配点）。"""
        if self.capability is not None:
            return self.capability
        return from_role_capabilities(dict(self.capabilities), model=self.model)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "model": self.model,
            "provider": self.provider,
            "origin": self.origin,
            "available": bool(self.available),
            "byok": bool(self.byok),
        }


@dataclass(frozen=True, slots=True)
class ModelRoutingConclusion:
    """一次路由结论（``to_routing()`` 就是前端读的那份结构化契约）。"""

    action: str = DegradationAction.NONE.value
    reason_code: str = ""
    safe_message: str = ""
    from_profile: str = ""
    to_profile: str = ""
    from_model: str = ""
    to_model: str = ""
    excluded: tuple[Mapping[str, str], ...] = ()
    missing_capabilities: tuple[str, ...] = ()
    switch_allowed: bool = True
    decision_reason_code: str = ""
    target_model: str = ""
    target_profile: str = ""
    lossy: bool = False
    process_notice: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    blocked: bool = False
    byok: bool = False
    #: 仅运行期使用（不进入结构化契约）：目标候选的档位配置，供计划冻结改写角色用。
    to_provider: str = ""
    to_source: str = ""
    to_capabilities: Mapping[str, Any] = field(default_factory=dict)
    to_timeout: float = 0.0

    @property
    def switched(self) -> bool:
        return bool(self.from_profile and self.to_profile and self.from_profile != self.to_profile)

    @property
    def degraded(self) -> bool:
        return bool(
            self.blocked
            or self.lossy
            or self.switched
            or self.action not in {"", DegradationAction.NONE.value}
        )

    @property
    def notice_required(self) -> bool:
        """真正发生切换/有损降级/阻断时才发 process 事件（无变化不发）。"""
        return bool(
            self.switched
            or self.blocked
            or self.action
            in {
                DegradationAction.PRECOMPRESS.value,
                DegradationAction.EXTRACT_FRAMES.value,
                DegradationAction.SIMULATE_STREAM.value,
            }
        )

    def to_routing(self) -> dict[str, Any]:
        """结构化结论（精确键集 = :data:`MODEL_ROUTING_KEYS`）。"""
        return {
            "degraded": self.degraded,
            "action": self.action,
            "reason_code": self.reason_code,
            "safe_message": self.safe_message,
            "from_profile": self.from_profile,
            "to_profile": self.to_profile,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "excluded": [dict(item) for item in self.excluded],
            "missing_capabilities": list(self.missing_capabilities),
            "switch_allowed": bool(self.switch_allowed),
            "decision_reason_code": self.decision_reason_code,
            "target_model": self.target_model,
            "target_profile": self.target_profile,
            "lossy": bool(self.lossy),
            "process_notice": self.process_notice,
            "details": _json_safe(self.details),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "blocked": bool(self.blocked),
            "byok": bool(self.byok),
        }

    def process_notice_payload(self) -> dict[str, Any] | None:
        """process 事件载荷（``kind/title/summary/status/detail``）；无变化返回 None。"""
        if not self.notice_required:
            return None
        summary = str(self.process_notice or self.safe_message or "").strip()
        if not summary:
            return None
        return {
            "kind": "thinking",
            "title": MODEL_ROUTING_NOTICE_TITLE,
            "summary": summary,
            "status": "failed" if self.blocked else "completed",
            "detail": self.reason_code,
        }


def _json_safe(value: Any) -> Any:
    """把决策细节折成 JSON-safe（tuple/set → list；非标量兜底 str）。"""
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _excluded_entry(candidate: ModelCandidate, reason_code: str) -> dict[str, str]:
    return {
        "model": str(candidate.model or ""),
        "profile": str(candidate.profile or ""),
        "reason_code": str(reason_code or ""),
    }


def attach_model_routing(
    routing: Mapping[str, Any] | None,
    conclusion: ModelRoutingConclusion | Mapping[str, Any],
) -> dict[str, Any]:
    """把路由结论并入 ``job.routing``（唯一写入点；返回新字典，不改原对象）。

    * ``routing["model_routing"]``：结构化结论（前端契约）；
    * ``routing["model_routing_notice"]``：切换/降级/阻断时的 process 载荷
      （无变化时不写，界面也就不会出现"其实没降级"的提示）。
    """
    merged = dict(routing or {})
    payload = (
        dict(conclusion.to_routing())
        if isinstance(conclusion, ModelRoutingConclusion)
        else dict(conclusion or {})
    )
    merged[MODEL_ROUTING_KEY] = payload
    notice = (
        conclusion.process_notice_payload()
        if isinstance(conclusion, ModelRoutingConclusion)
        else _notice_from_mapping(payload)
    )
    if notice:
        merged[MODEL_ROUTING_NOTICE_KEY] = dict(notice)
    return merged


def _notice_from_mapping(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """已序列化的结论 → process 载荷（从快照恢复/跨进程时用）。"""
    if not payload:
        return None
    summary = str(payload.get("process_notice") or payload.get("safe_message") or "").strip()
    if not summary:
        return None
    return {
        "kind": "thinking",
        "title": MODEL_ROUTING_NOTICE_TITLE,
        "summary": summary,
        "status": "failed" if payload.get("blocked") else "completed",
        "detail": str(payload.get("reason_code") or ""),
    }


def model_routing_process_frame(routing: Any) -> dict[str, Any] | None:
    """``routing`` 里的路由载荷 → SSE ``process`` 帧（没有则 None）。

    只**新增**这一帧；``entry_id`` 与过程日志投影一致，刷新后合并成同一行。
    """
    notice = routing.get(MODEL_ROUTING_NOTICE_KEY) if isinstance(routing, Mapping) else None
    if not isinstance(notice, Mapping) or not notice:
        return None
    from lumi_contracts.events.process import ProcessLogEntry

    entry = ProcessLogEntry.from_event({"entry_id": MODEL_ROUTING_NOTICE_ENTRY_ID, **dict(notice)})
    return {"type": "process", "content": entry.summary, **entry.to_sse_fields()}


def _plan_entry(plan: ModelPlan | None, role: str) -> dict[str, Any] | None:
    """冻结计划里该角色的条目（缺失/非冻结命中返回 None）。"""
    if plan is None:
        return None
    entry = plan.roles.get(model_roles.normalize_role(role))
    if not isinstance(entry, Mapping) or entry.get("frozen_missing"):
        return None
    return dict(entry)


def _candidate_from_entry(entry: Mapping[str, Any], *, price_rank: int) -> ModelCandidate:
    model = str(entry.get("model") or "")
    capabilities = dict(entry.get("capabilities") or {})
    return ModelCandidate(
        profile=model_roles.normalize_profile(str(entry.get("profile") or "")),
        model=model,
        provider=str(entry.get("provider") or ""),
        source=str(entry.get("source") or ""),
        capability=from_role_capabilities(capabilities, model=model),
        capabilities=capabilities,
        timeout=float(entry.get("timeout") or 0.0),
        available=bool(model),
        byok=bool(entry.get("byok")),
        price_rank=price_rank,
        origin="plan",
    )


class ModelCapabilityRouter:
    """候选构建 + 能力过滤 + 档位选择（``enabled()`` 为假时调用方不介入）。"""

    def __init__(
        self,
        *,
        resolver: Callable[..., Awaitable[model_roles.ResolvedModel]] | None = None,
        catalog: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        settings: Any = None,
    ) -> None:
        self._resolver = resolver
        self._catalog = catalog
        self._settings = settings

    def enabled(self) -> bool:
        from app.core.feature_flags import feature_enabled

        return feature_enabled(FLAG, settings=self._settings)

    # ── 候选 ──────────────────────────────────────────────

    async def _resolve(self, role: str, *, scene: str | None, user_id: str | None):
        resolve = self._resolver or model_roles.resolve_role
        return await resolve(role, scene=scene, user_id=user_id)

    def _catalog_items(self) -> list[dict[str, Any]]:
        if self._catalog is not None:
            return [dict(item) for item in self._catalog() or ()]
        from app.core.model_catalog import get_model_catalog

        return [dict(item) for item in get_model_catalog()]

    def _enrich_with_catalog(self, candidate: ModelCandidate) -> ModelCandidate:
        """注册表补充能力事实（上下文长度/多模态）；不覆盖档位已有声明。"""
        key = str(candidate.model or "").casefold()
        if not key:
            return candidate
        for item in self._catalog_items():
            if str(item.get("id") or "").casefold() != key:
                continue
            profile = candidate.capability_profile()
            modalities = list(profile.input_modalities)
            if item.get("multimodal") and "image" not in modalities:
                modalities.append("image")
            context_window = int(item.get("context_window") or 0)
            return replace(
                candidate,
                capability=profile.model_copy(
                    update={
                        "input_modalities": modalities,
                        "max_context_tokens": (
                            max(profile.max_context_tokens, context_window)
                            if context_window
                            else profile.max_context_tokens
                        ),
                    }
                ),
            )
        return candidate

    def _registry_candidate(self, item: Mapping[str, Any], index: int) -> ModelCandidate:
        modalities = ["text"]
        if item.get("multimodal"):
            modalities.append("image")
        return ModelCandidate(
            profile="",
            model=str(item.get("id") or ""),
            provider=str(item.get("provider") or ""),
            source="registry",
            capability=ModelCapabilityProfile(
                model=str(item.get("id") or ""),
                input_modalities=modalities,
                max_context_tokens=int(item.get("context_window") or 32_000),
                # 注册表只声明上下文/多模态；工具/JSON 能力一律不猜（保守）。
                supports_tools=False,
                supports_json=False,
                supports_streaming=True,
            ),
            available=False,
            price_rank=UNKNOWN_PRICE_RANK + index,
            origin="registry",
        )

    def _candidate_from_resolved(
        self, resolved: model_roles.ResolvedModel, *, price_rank: int
    ) -> ModelCandidate:
        return ModelCandidate(
            profile=model_roles.normalize_profile(resolved.profile),
            model=str(resolved.model or ""),
            provider=str(resolved.provider or ""),
            source=str(resolved.source or ""),
            capability=from_role_capabilities(dict(resolved.capabilities), model=resolved.model),
            capabilities=dict(resolved.capabilities),
            timeout=float(resolved.timeout or 0.0),
            available=bool(resolved.model),
            byok=bool(resolved.byok),
            price_rank=price_rank,
            origin="profile",
        )

    async def build_candidates(
        self,
        *,
        plan: ModelPlan | None = None,
        scene: str | None = None,
        user_id: str | None = None,
        resolver: Callable[..., Awaitable[model_roles.ResolvedModel]] | None = None,
    ) -> list[ModelCandidate]:
        """候选池：冻结计划（优先）→ ``.env``/管理员档位解析 → 注册表补充条目。"""
        previous = self._resolver
        if resolver is not None:
            self._resolver = resolver
        candidates: list[ModelCandidate] = []
        seen: set[tuple[str, str]] = set()
        models: set[str] = set()
        try:
            for profile, role in PROFILE_ANCHOR_ROLES.items():
                rank = PROFILE_PRICE_RANK.get(profile, UNKNOWN_PRICE_RANK)
                entry = _plan_entry(plan, role)
                try:
                    candidate = (
                        _candidate_from_entry(entry, price_rank=rank)
                        if entry is not None
                        else self._candidate_from_resolved(
                            await self._resolve(role, scene=scene, user_id=user_id),
                            price_rank=rank,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - 单档位解析失败不毁掉候选池
                    logger.debug("[model-router] 档位候选解析失败({}): {}", profile, str(exc)[:120])
                    continue
                candidate = self._enrich_with_catalog(candidate)
                candidate = replace(
                    candidate,
                    price_rank=PROFILE_PRICE_RANK.get(candidate.profile, candidate.price_rank),
                )
                key = (candidate.profile, candidate.model)
                if key in seen:
                    continue
                seen.add(key)
                if candidate.model:
                    models.add(candidate.model.casefold())
                candidates.append(candidate)
        finally:
            self._resolver = previous

        # 注册表：已被档位配置覆盖的模型不重复出现；其余作为"注册表里有但未启用"的记录。
        for index, item in enumerate(self._catalog_items()):
            model_id = str(item.get("id") or "")
            if not model_id or model_id.casefold() in models:
                continue
            candidates.append(self._registry_candidate(item, index))
        return candidates

    # ── 过滤 ──────────────────────────────────────────────

    def reject_reason(
        self, candidate: ModelCandidate, requirements: CapabilityRequirements
    ) -> str:
        """候选被拒绝的原因（空串 = 可用）；码表 = 前端 ``MODEL_EXCLUSION_LABELS``。"""
        if candidate.byok:
            return "BYOK"
        if not candidate.available or not candidate.model:
            return "UNHEALTHY"
        capability = candidate.capability_profile()
        if requirements.min_context_tokens and capability.max_context_tokens < int(
            requirements.min_context_tokens
        ):
            return "CONTEXT_TOO_SMALL"
        if (
            requirements.strict_streaming
            and requirements.needs_streaming
            and not capability.supports_streaming
        ):
            return "LACKS_STREAMING"
        decision = decide_degradation(capability, requirements.to_modality_request(), candidates=())
        if decision.blocked:
            return _DECISION_EXCLUSION.get(decision.reason_code, "LACKS_VISION")
        return ""

    def _planned_capability(
        self,
        profile: str,
        candidates: Sequence[ModelCandidate],
        plan: ModelPlan | None,
    ) -> ModelCapabilityProfile:
        """计划档位的能力画像：候选声明 → 冻结计划 → 档位默认（不猜模型名）。"""
        for candidate in candidates:
            if candidate.profile == profile:
                return candidate.capability_profile()
        entry = _plan_entry(plan, PROFILE_ANCHOR_ROLES.get(profile, ""))
        if entry is not None:
            return from_role_capabilities(
                dict(entry.get("capabilities") or {}), model=str(entry.get("model") or "")
            )
        return from_role_capabilities(model_roles.profile_capabilities(profile), model="")

    # ── 决策 ──────────────────────────────────────────────

    async def route(
        self,
        *,
        plan: ModelPlan | None = None,
        requirements: CapabilityRequirements | None = None,
        primary_role: str | None = None,
        candidates: Sequence[ModelCandidate] | None = None,
        resolver: Callable[..., Awaitable[model_roles.ResolvedModel]] | None = None,
        scene: str | None = None,
        user_id: str | None = None,
        shadow: bool | None = None,
    ) -> ModelRoutingConclusion:
        """按冻结计划 + 能力需求给出结论（**不调用模型、不调用 Broker**）。

        ``shadow``（影子/排障模式；``None`` = 读 ``INTEGRATION_SHADOW_MODE``）只决定
        ``excluded[]`` 是否保留诊断明细，**不改执行路径与其它字段**。
        """
        conclusion = await self._decide(
            plan=plan,
            requirements=requirements,
            primary_role=primary_role,
            candidates=candidates,
            resolver=resolver,
            scene=scene,
            user_id=user_id,
        )
        return self._apply_excluded_policy(conclusion, shadow=shadow)

    async def _decide(
        self,
        *,
        plan: ModelPlan | None = None,
        requirements: CapabilityRequirements | None = None,
        primary_role: str | None = None,
        candidates: Sequence[ModelCandidate] | None = None,
        resolver: Callable[..., Awaitable[model_roles.ResolvedModel]] | None = None,
        scene: str | None = None,
        user_id: str | None = None,
    ) -> ModelRoutingConclusion:
        """按冻结计划 + 能力需求给出结论（**不调用模型、不调用 Broker**）。"""
        req = requirements or CapabilityRequirements()
        role = str(primary_role or model_roles.ROLE_DIRECT_ANSWER)
        byok_entry = _plan_entry(plan, role)
        pool = (
            list(candidates)
            if candidates is not None
            else await self.build_candidates(
                plan=plan, scene=scene, user_id=user_id, resolver=resolver
            )
        )
        from_profile = model_roles.normalize_profile(
            str((byok_entry or {}).get("profile") or "")
            or model_roles.role_profile(role)
            or model_roles.PROFILE_MAIN
        )

        excluded: list[dict[str, str]] = []
        eligible: list[ModelCandidate] = []
        for candidate in pool:
            reason = self.reject_reason(candidate, req)
            if reason:
                excluded.append(_excluded_entry(candidate, reason))
            else:
                eligible.append(candidate)

        # BYOK：用户自备模型不参与系统默认切换（钉住，不降级、不换档）。
        if bool(getattr(plan, "byok", False)) or bool((byok_entry or {}).get("byok")):
            return self._byok_conclusion(
                byok_entry=byok_entry or {},
                from_profile=from_profile,
                excluded=self._byok_excluded(byok_entry or {}, pool, excluded),
                requirements=req,
                candidates=pool,
            )

        anchor_candidate = next(
            (item for item in pool if item.profile == from_profile), None
        )
        anchor = next((item for item in eligible if item.profile == from_profile), None)
        # 决策针对"计划里那个档位**实际会用的模型**"（同档位可能有多个候选模型）。
        planned_capability = (
            anchor.capability_profile()
            if anchor is not None
            else (
                anchor_candidate.capability_profile()
                if anchor_candidate is not None
                else self._planned_capability(from_profile, pool, plan)
            )
        )
        decision = decide_degradation(
            planned_capability,
            req.to_modality_request(),
            candidates=[
                item.capability_profile() for item in eligible if item.profile != from_profile
            ],
        )

        if decision.blocked:
            # 工具/模态缺失：**阻断**，不静默换模型执行（决策表铁律①）。
            return self._blocked_conclusion(
                from_profile=from_profile,
                pool=pool,
                plan=plan,
                requirements=req,
                excluded=excluded,
                decision=decision,
                anchor=anchor or anchor_candidate,
            )

        if decision.action == DegradationAction.AUTO_SWITCH.value:
            target = next(
                (item for item in eligible if item.model == decision.target_model), None
            )
            if target is not None:
                return self._switched_conclusion(
                    chosen=target,
                    from_profile=from_profile,
                    anchor=anchor or anchor_candidate,
                    requirements=req,
                    excluded=excluded,
                    pool=pool,
                    eligible=eligible,
                    reason_code=(
                        "VISION_REQUIRED"
                        if "image" in set(req.to_modality_request().modalities)
                        else "NO_TOOL_SUPPORT"
                    ),
                    action=DegradationAction.AUTO_SWITCH.value,
                    decision=decision,
                )
            return self._blocked_conclusion(
                from_profile=from_profile,
                pool=pool,
                plan=plan,
                requirements=req,
                excluded=excluded,
                decision=decision,
                anchor=anchor or anchor_candidate,
            )

        if anchor is not None:
            if decision.action != DegradationAction.NONE.value:
                # PRECOMPRESS / EXTRACT_FRAMES / SIMULATE_STREAM：留在原档位，告知用户。
                return self._degraded_in_place_conclusion(
                    anchor=anchor,
                    from_profile=from_profile,
                    requirements=req,
                    excluded=excluded,
                    decision=decision,
                    pool=pool,
                    eligible=eligible,
                )
            # 能力允许 → 优先低成本档位（能力不足的候选已在 eligible 之外）。
            cheaper = self._cheapest(eligible, cheaper_than=anchor.price_rank)
            if cheaper is not None:
                return self._switched_conclusion(
                    chosen=cheaper,
                    from_profile=from_profile,
                    anchor=anchor,
                    requirements=req,
                    excluded=excluded,
                    pool=pool,
                    eligible=eligible,
                    reason_code="COST_OPTIMIZED",
                    action=DegradationAction.AUTO_SWITCH.value,
                    decision=decision,
                )
            return ModelRoutingConclusion(
                action=DegradationAction.NONE.value,
                reason_code="",
                from_profile=from_profile,
                to_profile=anchor.profile,
                from_model=anchor.model,
                to_model=anchor.model,
                excluded=tuple(excluded),
                missing_capabilities=tuple(
                    missing_for_capability(anchor.capability_profile(), req)
                ),
                switch_allowed=True,
                target_model=anchor.model,
                target_profile=anchor.profile,
                details=self._details(req, decision, pool, eligible),
                to_provider=anchor.provider,
                to_source=anchor.source,
                to_capabilities=dict(anchor.capabilities),
                to_timeout=float(anchor.timeout or 0.0),
            )

        # 计划档位不可用/不达标（没配模型、上下文不够、严格流式不满足）：
        # 降级到最便宜的可用候选；没有候选则阻断。
        if eligible:
            reject = (
                self.reject_reason(anchor_candidate, req)
                if anchor_candidate is not None
                else "UNHEALTHY"
            )
            return self._switched_conclusion(
                chosen=min(eligible, key=_pick_order),
                from_profile=from_profile,
                anchor=anchor_candidate,
                requirements=req,
                excluded=excluded,
                pool=pool,
                eligible=eligible,
                reason_code=_REJECT_TO_SWITCH_REASON.get(reject, "MODEL_LACKS_CAPABILITY"),
                action=DegradationAction.AUTO_SWITCH.value,
                decision=decision,
            )
        return self._blocked_conclusion(
            from_profile=from_profile,
            pool=pool,
            plan=plan,
            requirements=req,
            excluded=excluded,
            decision=decision,
            anchor=anchor_candidate,
        )

    @staticmethod
    def _cheapest(
        eligible: Sequence[ModelCandidate], *, cheaper_than: int
    ) -> ModelCandidate | None:
        cheaper = [item for item in eligible if item.price_rank < cheaper_than]
        if not cheaper:
            return None
        return min(cheaper, key=_pick_order)

    # ── ``excluded[]`` 展示时机（前端"被排除的候选模型(n)"只在真决策时出现）──

    @staticmethod
    def excluded_is_meaningful(conclusion: ModelRoutingConclusion) -> bool:
        """``excluded[]`` 是否在解释一次**真的发生了**的决策。

        真决策 = 降级/换档/有损/阻断（``degraded``）或"不允许切换"（``switch_allowed=False``，
        含 BYOK 钉住）；其余情况（``action=NONE`` 且档位未变）``excluded[]`` 只是噪声。
        """
        return bool(conclusion.degraded or not conclusion.switch_allowed)

    def _diagnostics_enabled(self, shadow: bool | None) -> bool:
        """影子/排障模式：保留 ``excluded[]`` 诊断明细（不改执行路径）。"""
        if shadow is not None:
            return bool(shadow)
        from app.core.feature_flags import shadow_mode

        return shadow_mode(settings=self._settings)

    def _apply_excluded_policy(
        self, conclusion: ModelRoutingConclusion, *, shadow: bool | None
    ) -> ModelRoutingConclusion:
        """没有决策 → ``excluded[]`` 留空（键仍在，键集/码表不变）；影子模式保留明细。"""
        if not conclusion.excluded:
            return conclusion
        if self.excluded_is_meaningful(conclusion) or self._diagnostics_enabled(shadow):
            return conclusion
        return replace(conclusion, excluded=())

    def _details(
        self,
        requirements: CapabilityRequirements,
        decision: Any,
        pool: Sequence[ModelCandidate],
        eligible: Sequence[ModelCandidate],
    ) -> dict[str, Any]:
        return {
            "requirements": requirements.to_dict(),
            "decision": {
                "action": str(getattr(decision, "action", "")),
                "reason_code": str(getattr(decision, "reason_code", "")),
                "lossy": bool(getattr(decision, "lossy", False)),
                "details": _json_safe(getattr(decision, "details", {}) or {}),
            },
            "candidate_count": len(pool),
            "eligible_count": len(eligible),
            "candidates": [item.to_snapshot() for item in pool],
        }

    def _switched_conclusion(
        self,
        *,
        chosen: ModelCandidate,
        from_profile: str,
        anchor: ModelCandidate | None,
        requirements: CapabilityRequirements,
        excluded: Sequence[Mapping[str, str]],
        pool: Sequence[ModelCandidate],
        eligible: Sequence[ModelCandidate],
        reason_code: str,
        action: str,
        decision: Any,
    ) -> ModelRoutingConclusion:
        notice = _switch_message(reason_code, from_profile, chosen.profile)
        return ModelRoutingConclusion(
            action=action,
            reason_code=reason_code,
            safe_message=notice,
            from_profile=from_profile,
            to_profile=chosen.profile,
            from_model=str(anchor.model if anchor is not None else ""),
            to_model=chosen.model,
            excluded=tuple(excluded),
            missing_capabilities=tuple(missing_for_capability(chosen.capability_profile(), requirements)),
            switch_allowed=True,
            decision_reason_code=str(getattr(decision, "reason_code", "")),
            target_model=chosen.model,
            target_profile=chosen.profile,
            lossy=bool(getattr(decision, "lossy", False)),
            process_notice=notice,
            details=self._details(requirements, decision, pool, eligible),
            to_provider=chosen.provider,
            to_source=chosen.source,
            to_capabilities=dict(chosen.capabilities),
            to_timeout=float(chosen.timeout or 0.0),
        )

    def _degraded_in_place_conclusion(
        self,
        *,
        anchor: ModelCandidate,
        from_profile: str,
        requirements: CapabilityRequirements,
        excluded: Sequence[Mapping[str, str]],
        decision: Any,
        pool: Sequence[ModelCandidate],
        eligible: Sequence[ModelCandidate],
    ) -> ModelRoutingConclusion:
        action = str(decision.action)
        notice = str(decision.process_notice or "") or (
            "当前模型不支持流式输出，将按非流式生成后一次性返回。"
            if action == DegradationAction.SIMULATE_STREAM.value
            else ""
        )
        return ModelRoutingConclusion(
            action=action,
            reason_code="MODEL_LACKS_CAPABILITY",
            safe_message=notice,
            from_profile=from_profile,
            to_profile=anchor.profile,
            from_model=anchor.model,
            to_model=anchor.model,
            excluded=tuple(excluded),
            missing_capabilities=tuple(
                missing_for_capability(anchor.capability_profile(), requirements)
            ),
            switch_allowed=True,
            decision_reason_code=str(decision.reason_code),
            target_model=anchor.model,
            target_profile=anchor.profile,
            lossy=bool(decision.lossy),
            process_notice=notice,
            details=self._details(requirements, decision, pool, eligible),
            to_provider=anchor.provider,
            to_source=anchor.source,
            to_capabilities=dict(anchor.capabilities),
            to_timeout=float(anchor.timeout or 0.0),
        )

    def _blocked_conclusion(
        self,
        *,
        from_profile: str,
        pool: Sequence[ModelCandidate],
        plan: ModelPlan | None,
        requirements: CapabilityRequirements,
        excluded: Sequence[Mapping[str, str]],
        decision: Any,
        anchor: ModelCandidate | None = None,
    ) -> ModelRoutingConclusion:
        capability = (
            anchor.capability_profile()
            if anchor is not None
            else self._planned_capability(from_profile, pool, plan)
        )
        missing = missing_for_capability(capability, requirements)
        error = translate_error({"code": "CAPABILITY_UNAVAILABLE"})
        message = _block_message(missing)
        return ModelRoutingConclusion(
            action=DegradationAction.BLOCK.value,
            reason_code="MODEL_LACKS_CAPABILITY",
            safe_message=message,
            from_profile=from_profile,
            to_profile=from_profile,
            from_model=str(anchor.model if anchor is not None else capability.model),
            to_model="",
            excluded=tuple(excluded),
            missing_capabilities=tuple(missing),
            switch_allowed=False,
            decision_reason_code=str(getattr(decision, "reason_code", "")),
            target_model="",
            target_profile="",
            lossy=False,
            process_notice=message,
            details=self._details(requirements, decision, pool, ()),
            error_code=str(error.code),
            error_message=str(error.safe_message),
            blocked=True,
        )

    def _byok_excluded(
        self,
        byok_entry: Mapping[str, Any],
        pool: Sequence[ModelCandidate],
        excluded: Sequence[Mapping[str, str]],
    ) -> list[dict[str, str]]:
        """BYOK 结论的 excluded[]：用户模型本身按 ``BYOK`` 记录（不参与默认切换）。"""
        entries: list[dict[str, str]] = []
        model = str(byok_entry.get("model") or "")
        if model:
            entries.append(
                {
                    "model": model,
                    "profile": str(byok_entry.get("profile") or ""),
                    "reason_code": "BYOK",
                }
            )
        for candidate in pool:
            if candidate.byok and candidate.model and candidate.model != model:
                entries.append(_excluded_entry(candidate, "BYOK"))
        for item in excluded or ():
            if dict(item) not in entries:
                entries.append(dict(item))
        return entries

    def _byok_conclusion(
        self,
        *,
        byok_entry: Mapping[str, Any],
        from_profile: str,
        excluded: Sequence[Mapping[str, str]],
        requirements: CapabilityRequirements,
        candidates: Sequence[ModelCandidate],
    ) -> ModelRoutingConclusion:
        model = str(byok_entry.get("model") or "")
        profile = model_roles.normalize_profile(str(byok_entry.get("profile") or from_profile))
        return ModelRoutingConclusion(
            action=DegradationAction.NONE.value,
            reason_code="BYOK_PINNED",
            safe_message="本次使用你自备的模型（不参与系统默认切换）。",
            from_profile=profile,
            to_profile=profile,
            from_model=model,
            to_model=model,
            excluded=tuple(excluded),
            missing_capabilities=(),
            switch_allowed=False,
            target_model=model,
            target_profile=profile,
            details={
                "requirements": requirements.to_dict(),
                "byok": True,
                "candidates": [item.to_snapshot() for item in candidates],
            },
            byok=True,
        )

    # ── 计划改写（让切换真正生效，且任务期间不再变）──────────

    def refine_roles(
        self, plan: ModelPlan, conclusion: ModelRoutingConclusion
    ) -> dict[str, dict[str, Any]]:
        """把结论落到冻结计划的角色上（返回新的 ``roles`` 字典，不改原计划）。

        只改 ``from_profile`` 上且 ``ROLE_FALLBACK == "none"`` 的角色：这些角色调用方
        已有确定性兜底，不依赖工具/审批；``tool_*`` / ``planner_*`` / ``code_*`` 等角色
        保持原档位（它们的能力需求不在任务级需求里，不能被降级）。
        """
        if plan is None:
            return {}
        if not conclusion.switched:
            return {role: dict(entry) for role, entry in plan.roles.items()}
        refined: dict[str, dict[str, Any]] = {}
        for role, entry in plan.roles.items():
            item = dict(entry)
            if (
                str(item.get("profile") or "") == conclusion.from_profile
                and model_roles.ROLE_FALLBACK.get(model_roles.normalize_role(role)) == "none"
            ):
                item.update(
                    {
                        "profile": conclusion.to_profile,
                        "model": conclusion.to_model,
                        "provider": conclusion.to_provider or str(item.get("provider") or ""),
                        "source": conclusion.to_source or "capability_router",
                        "byok": False,
                        "capabilities": dict(conclusion.to_capabilities),
                        "timeout": float(conclusion.to_timeout or item.get("timeout") or 0.0),
                    }
                )
            refined[str(role)] = item
        return refined


def _pick_order(candidate: ModelCandidate) -> tuple[int, str, str]:
    return (int(candidate.price_rank), str(candidate.profile), str(candidate.model))


def _switch_message(reason_code: str, from_profile: str, to_profile: str) -> str:
    target = profile_user_label(to_profile)
    if reason_code == "COST_OPTIMIZED":
        return f"当前任务不需要更强能力，已切换到{target}以降低成本。"
    if reason_code == "VISION_REQUIRED":
        return f"该步骤需要视觉能力，已切换到{target}。"
    if reason_code == "PRIMARY_UNAVAILABLE":
        return f"{profile_user_label(from_profile)}当前不可用，已切换到{target}。"
    if reason_code == "NO_TOOL_SUPPORT":
        return f"当前模型不支持工具调用，已切换到{target}。"
    return f"当前模型能力不足，已切换到{target}。"


def _block_message(missing: Sequence[str]) -> str:
    names = [str(item) for item in missing if item]
    if "tools" in names:
        return "当前模型不支持工具调用，无法安全执行该任务。"
    if "vision" in names:
        return "当前模型不支持图片输入，且没有可用的视觉模型。"
    if names:
        text = "、".join(_CAPABILITY_TEXT.get(name, name) for name in names)
        return f"当前模型缺少{text}能力，无法继续执行该任务。"
    return "当前没有满足任务能力要求的模型，无法继续执行该任务。"


#: 进程内共享路由器（服务端唯一入口）。
model_capability_router = ModelCapabilityRouter()


__all__ = [
    "EXCLUDED_ENTRY_KEYS",
    "FLAG",
    "MODEL_ROUTING_KEY",
    "MODEL_ROUTING_KEYS",
    "MODEL_ROUTING_NOTICE_ENTRY_ID",
    "MODEL_ROUTING_NOTICE_KEY",
    "MODEL_ROUTING_NOTICE_TITLE",
    "PROFILE_ANCHOR_ROLES",
    "PROFILE_PRICE_RANK",
    "CapabilityRequirements",
    "ModelCandidate",
    "ModelCapabilityRouter",
    "ModelRoutingConclusion",
    "attach_model_routing",
    "missing_for_capability",
    "model_capability_router",
    "model_routing_process_frame",
]
