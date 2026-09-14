"""阶段 5：服务端 Policy Pack（"允许做到什么程度"）。

与既有策略层的关系（不重复发明）：

* ``lumi_orch.execution_policy`` 决定**路径**（direct_stream / planner_dag / react…）；
* ``app.agents.skills.approval_policy`` 决定**单次工具调用的 A/B/C 档**；
* 本模块决定**能力调用的边界**：能否切到服务端执行（hybrid 的核心开关）、哪些副作用
  必须先审批、流式/输出预算、以及"客户端本地拒止"的最小集。

内置四个包（方案第五节）：

===============  ==========  ============  ==================================
包               云端切换     写/执行审批   适用
===============  ==========  ============  ==================================
cost_saver       允许        否            小任务、把本地读取也放到服务端省钱
high_precision   允许        是            要求准确率，多数动作先确认
manual_commit    禁止        是            默认：写盘必须人工提交
enterprise_audit 禁止        是 + 审计     合规场景：更小预算 + 全量审计
===============  ==========  ============  ==================================

**客户端只保留官方签名的本地拒止策略**（方案原文）：因此这里额外声明
``client_deny_kinds``——客户端可以拒止的副作用类别；服务端授权不能替用户扩大本机
权限，客户端拒止优先（``LOCAL_POLICY_DENIED``）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.plugins import SideEffectKind

#: 与既有工作区审批模式对齐（``workspace_context`` 的词表）。
APPROVAL_MODE_AUTO = "auto_routine"
APPROVAL_MODE_CONFIRM = "manual_commit"

#: 内置包 id（前端/审计按它读版本）。
POLICY_COST_SAVER = "cost_saver"
POLICY_HIGH_PRECISION = "high_precision"
POLICY_MANUAL_COMMIT = "manual_commit"
POLICY_ENTERPRISE_AUDIT = "enterprise_audit"

BUILTIN_POLICY_IDS: tuple[str, ...] = (
    POLICY_COST_SAVER,
    POLICY_HIGH_PRECISION,
    POLICY_MANUAL_COMMIT,
    POLICY_ENTERPRISE_AUDIT,
)

POLICY_PACK_VERSION = "1.0.0"

#: 客户端本地拒止的默认类别（官方签名的最小集）。
_DEFAULT_CLIENT_DENY: tuple[str, ...] = (
    SideEffectKind.DELETE.value,
    SideEffectKind.EXTERNAL.value,
)


@dataclass(frozen=True, slots=True)
class PolicyPack:
    """一个服务端策略包（不可变；``to_snapshot`` 进 Job 快照）。"""

    id: str
    version: str = POLICY_PACK_VERSION
    #: hybrid 能力是否允许切到服务端执行（False = 一律留在本地）。
    allow_cloud_switch: bool = False
    #: 哪些副作用必须先审批（空集合 = 全部自动）。
    approval_required_side_effects: frozenset[str] = frozenset()
    #: 要求审批时，是否**必须**携带有效 approval_token（否则 APPROVAL_REQUIRED）。
    require_approval_token: bool = True
    #: 单次结果进入上下文的最大字节（超出应转 Artifact）。
    max_inline_bytes: int = 400_000
    #: 是否允许流式（stream_cursor）；关闭时整段返回。
    allow_streaming: bool = True
    #: 是否记录全量审计事件（企业合规）。
    audit_all: bool = False
    #: 客户端可以拒止的副作用类别（本机最终否决权）。
    client_deny_side_effects: frozenset[str] = field(default_factory=lambda: frozenset(_DEFAULT_CLIENT_DENY))
    reason: str = ""

    def needs_approval_for(self, side_effects: Any) -> bool:
        """给定副作用集合是否需要审批。"""
        effects = {str(getattr(item, "value", item)) for item in (side_effects or ())}
        if not effects:
            return False
        return bool(effects & set(self.approval_required_side_effects))

    def client_may_deny(self, side_effects: Any) -> bool:
        effects = {str(getattr(item, "value", item)) for item in (side_effects or ())}
        return bool(effects & set(self.client_deny_side_effects))

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "allow_cloud_switch": bool(self.allow_cloud_switch),
            "approval_required_side_effects": sorted(self.approval_required_side_effects),
            "require_approval_token": bool(self.require_approval_token),
            "max_inline_bytes": int(self.max_inline_bytes),
            "allow_streaming": bool(self.allow_streaming),
            "audit_all": bool(self.audit_all),
            "client_deny_side_effects": sorted(self.client_deny_side_effects),
            "reason": self.reason,
        }

    def digest(self) -> str:
        blob = json.dumps(self.to_snapshot(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _writing_effects() -> frozenset[str]:
    return frozenset(
        {
            SideEffectKind.WRITE.value,
            SideEffectKind.DELETE.value,
            SideEffectKind.EXECUTE.value,
            SideEffectKind.EXTERNAL.value,
        }
    )


#: 内置包定义。
_BUILTIN: dict[str, PolicyPack] = {
    POLICY_COST_SAVER: PolicyPack(
        id=POLICY_COST_SAVER,
        allow_cloud_switch=True,
        approval_required_side_effects=frozenset({SideEffectKind.EXTERNAL.value}),
        max_inline_bytes=800_000,
        reason="成本优先：允许把可云端执行的工作放到服务端，减少本地往返",
    ),
    POLICY_HIGH_PRECISION: PolicyPack(
        id=POLICY_HIGH_PRECISION,
        allow_cloud_switch=True,
        approval_required_side_effects=_writing_effects(),
        max_inline_bytes=1_200_000,
        reason="精度优先：所有写/执行类动作先确认，允许为准确率付出往返成本",
    ),
    POLICY_MANUAL_COMMIT: PolicyPack(
        id=POLICY_MANUAL_COMMIT,
        allow_cloud_switch=False,
        approval_required_side_effects=_writing_effects(),
        max_inline_bytes=400_000,
        reason="默认：本地数据不出本机，写盘/执行必须人工提交",
    ),
    POLICY_ENTERPRISE_AUDIT: PolicyPack(
        id=POLICY_ENTERPRISE_AUDIT,
        allow_cloud_switch=False,
        approval_required_side_effects=_writing_effects(),
        max_inline_bytes=200_000,
        allow_streaming=False,
        audit_all=True,
        reason="合规：禁止数据出本机、全量审计、更小的输出预算",
    ),
}


class PolicyPackRegistry:
    """策略包注册表（服务端；客户端只保留官方签名的最小拒止策略）。"""

    def __init__(self, packs: dict[str, PolicyPack] | None = None) -> None:
        self._packs: dict[str, PolicyPack] = dict(packs or _BUILTIN)

    def ids(self) -> tuple[str, ...]:
        return tuple(self._packs)

    def get(self, pack_id: str) -> PolicyPack | None:
        return self._packs.get(str(pack_id or "").strip())

    def require(self, pack_id: str) -> PolicyPack:
        found = self.get(pack_id)
        if found is None:
            raise KeyError(f"未知策略包：{pack_id}")
        return found

    def register(self, pack: PolicyPack, *, replace: bool = False) -> PolicyPack:
        if pack.id in self._packs and not replace:
            raise ValueError(f"策略包已存在：{pack.id}")
        self._packs[pack.id] = pack
        return pack

    def all(self) -> list[PolicyPack]:
        return sorted(self._packs.values(), key=lambda item: item.id)

    def to_snapshot(self) -> list[dict[str, Any]]:
        return [item.to_snapshot() for item in self.all()]


#: 进程内共享注册表。
policy_packs = PolicyPackRegistry()

#: 默认包：没有更强信号时用 manual_commit（与既有工作区默认审批模式一致）。
DEFAULT_POLICY_ID = POLICY_MANUAL_COMMIT


def select_policy_id(
    *,
    requested: str = "",
    approval_mode: str = "",
    risk_level: str = "",
    side_effects: Any = None,
) -> str:
    """选择策略包 id（显式指定 > 合规/高危 > 审批模式 > 默认）。

    这里只做**选择**，不做判定；判定在 ``PluginPolicyGuard``。选择结果会进 Job 快照，
    因此每次都必须是确定性的。
    """
    explicit = str(requested or "").strip()
    if explicit and policy_packs.get(explicit) is not None:
        return explicit
    mode = str(approval_mode or "").strip().casefold()
    risk = str(risk_level or "").strip().upper()
    effects = {str(getattr(item, "value", item)) for item in (side_effects or ())}
    if risk in {"HIGH_RISK", "CRITICAL"}:
        return POLICY_HIGH_PRECISION
    if mode == APPROVAL_MODE_AUTO:
        # 自动模式仍不允许把本地数据搬到云上做"省钱优化"：预算/审计按需，云端切换仍关。
        return POLICY_COST_SAVER if SideEffectKind.EXTERNAL.value in effects else POLICY_MANUAL_COMMIT
    if mode == APPROVAL_MODE_CONFIRM:
        return POLICY_MANUAL_COMMIT
    return DEFAULT_POLICY_ID


__all__ = [
    "APPROVAL_MODE_AUTO",
    "APPROVAL_MODE_CONFIRM",
    "BUILTIN_POLICY_IDS",
    "DEFAULT_POLICY_ID",
    "POLICY_COST_SAVER",
    "POLICY_ENTERPRISE_AUDIT",
    "POLICY_HIGH_PRECISION",
    "POLICY_MANUAL_COMMIT",
    "POLICY_PACK_VERSION",
    "PolicyPack",
    "PolicyPackRegistry",
    "policy_packs",
    "select_policy_id",
]
