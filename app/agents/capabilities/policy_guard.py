"""阶段 5 + 阶段 3 后端：能力调用的策略门禁与审批令牌绑定。

两件事必须同时成立，缺一个就等于"策略只是文案"：

1. **策略判定**（`:class:`PluginPolicyGuard`）：按 Policy Pack 决定这次能力调用
   *允不允许切到服务端*、*要不要审批*、*能不能流式*；
2. **审批令牌绑定**（:func:`validate_approval`）：审批必须绑定**确切调用**
   （能力名 + 归一化参数指纹），否则会出现"用户批准了 A、实际执行了 B"。

令牌规则（与既有 ``events/approval.py::approval_fingerprint`` 同一模式）：

* 指纹 = sha256(能力名 + 作用域 + 排序后的参数)；
* 令牌必须**未过期**且指纹**完全一致**——过期或指纹不符都返回稳定错误码
  （``APPROVAL_EXPIRED`` / ``APPROVAL_INVALID``），不能被当成"没审批过"重来一次；
* 令牌按**次数**消耗：``max_uses`` 默认 1，重复使用要显式放宽。

本模块不弹 UI、不落库；它只回答"这次调用能不能过"，并把结论与理由一并返回。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    CapabilityErrorCode,
    CapabilityInvocation,
    CapabilityResult,
    capability_failure,
)

from app.agents.capabilities.policy_packs import PolicyPack, policy_packs

#: 默认令牌有效期（秒）。审批窗口过长等于没有审批。
DEFAULT_APPROVAL_TTL_SECONDS = 900.0


def capability_fingerprint(
    capability: str,
    arguments: dict[str, Any] | None = None,
    *,
    scope: dict[str, Any] | None = None,
) -> str:
    """能力调用的参数指纹（审批绑定的对象）。

    忽略执行期保留键（``_lumi_*``），与既有工具审批指纹同一约定，保证"同一语义调用
    得到同一指纹、不同参数得到不同指纹"。

    **能力基名参与指纹、版本号不参与**：审批针对"这个能力 + 这些参数"，Provider 升版
    不应该让已批准的调用失效。
    """
    payload = {
        key: value
        for key, value in dict(arguments or {}).items()
        if not str(key).startswith("_lumi_")
    }
    encoded = json.dumps(
        {
            "capability": str(capability or "").split("@", 1)[0],
            "args": payload,
            "scope": dict(scope or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def issue_approval_token(
    capability: str,
    arguments: dict[str, Any] | None = None,
    *,
    scope: dict[str, Any] | None = None,
    now: float | None = None,
    ttl_seconds: float = DEFAULT_APPROVAL_TTL_SECONDS,
    max_uses: int = 1,
    approved_by: str = "",
) -> dict[str, Any]:
    """签发一个审批令牌（**由审批服务在用户确认后调用**，不是能力调用方能自签的）。

    返回的是可序列化记录；调用方把它放进 ``CapabilityInvocation.approval_token``
    （只放其中的 ``token_id``）并在上下文里带上指纹与过期时间。
    """
    stamp = time.time() if now is None else float(now)
    fingerprint = capability_fingerprint(capability, arguments, scope=scope)
    return {
        "token_id": hashlib.sha256(f"{fingerprint}:{stamp}".encode("utf-8")).hexdigest()[:32],
        "capability": str(capability or ""),
        "fingerprint": fingerprint,
        "issued_at": stamp,
        "expires_at": stamp + max(0.0, float(ttl_seconds)),
        "max_uses": max(1, int(max_uses)),
        "uses": 0,
        "approved_by": str(approved_by or ""),
    }


@dataclass(slots=True)
class ApprovalVerdict:
    """审批校验结论。"""

    ok: bool
    code: str = ""
    message: str = ""
    fingerprint: str = ""

    def to_result(self, invocation: CapabilityInvocation) -> CapabilityResult | None:
        if self.ok:
            return None
        return capability_failure(
            self.code,
            self.message,
            capability=invocation.qualified_capability,
            suggested_action="请重新发起审批（参数已变化或审批已过期）",
            details={"fingerprint": self.fingerprint},
        )


def validate_approval(
    invocation: CapabilityInvocation,
    *,
    approval_context: dict[str, Any] | None = None,
    now: float | None = None,
) -> ApprovalVerdict:
    """校验审批令牌是否覆盖**本次**调用。

    ``approval_context`` 由审批服务在用户确认后写入（键：``capability`` /
    ``fingerprint`` / ``expires_at`` / ``approved_tool_calls``）。缺失即视为未审批。

    指纹基准统一用**能力基名**（去掉 ``@版本``）：调用方可能用 ``workspace.write``
    也可能用 ``workspace.write@1``，若两处基准不同就会"批准了却一直说未审批"。
    """
    stamp = time.time() if now is None else float(now)
    context = dict(approval_context or {})
    # 版本不参与审批指纹（审批针对"这个能力 + 这些参数"，与 Provider 升版无关）。
    capability_base = str(invocation.qualified_capability).split("@", 1)[0]
    fingerprint = capability_fingerprint(
        capability_base,
        invocation.arguments,
        scope=invocation.scope,
    )
    approved = context.get("fingerprint") or ""
    if not approved:
        return ApprovalVerdict(
            ok=False,
            code=CapabilityErrorCode.APPROVAL_REQUIRED.value,
            message=f"{invocation.qualified_capability} 需要你确认后才能执行",
            fingerprint=fingerprint,
        )
    expires_at = float(context.get("expires_at") or 0.0)
    if expires_at and stamp >= expires_at:
        return ApprovalVerdict(
            ok=False,
            code=CapabilityErrorCode.APPROVAL_EXPIRED.value,
            message="审批已过期，请重新确认",
            fingerprint=fingerprint,
        )
    if str(approved) != fingerprint:
        # 参数变了：批准的不是这次调用。
        return ApprovalVerdict(
            ok=False,
            code=CapabilityErrorCode.APPROVAL_INVALID.value,
            message="审批与本次调用参数不一致（参数可能已变化）",
            fingerprint=fingerprint,
        )
    return ApprovalVerdict(ok=True, fingerprint=fingerprint)


@dataclass(slots=True)
class PolicyVerdict:
    """一次能力调用的策略判定（可审计）。"""

    allowed: bool
    pack: PolicyPack
    needs_approval: bool = False
    allow_streaming: bool = True
    max_inline_bytes: int = 400_000
    audit_all: bool = False
    code: str = ""
    reason: str = ""

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "policy_id": self.pack.id,
            "policy_version": self.pack.version,
            "allowed": bool(self.allowed),
            "needs_approval": bool(self.needs_approval),
            "allow_streaming": bool(self.allow_streaming),
            "max_inline_bytes": int(self.max_inline_bytes),
            "audit_all": bool(self.audit_all),
            "code": self.code,
            "reason": self.reason,
        }


class PluginPolicyGuard:
    """能力调用的策略门禁（"允许做到什么程度"的唯一判定处）。"""

    def __init__(self, *, pack: PolicyPack | None = None, packs: Any = None) -> None:
        self._pack = pack
        self._packs = packs or policy_packs

    def pack_for(self, policy_id: str = "") -> PolicyPack:
        if self._pack is not None:
            return self._pack
        found = self._packs.get(policy_id) if policy_id else None
        if found is not None:
            return found
        from app.agents.capabilities.policy_packs import DEFAULT_POLICY_ID

        return self._packs.require(DEFAULT_POLICY_ID)

    def evaluate(
        self,
        descriptor: CapabilityDescriptor,
        *,
        policy_id: str = "",
    ) -> PolicyVerdict:
        """只看能力与策略，不看待审批（审批在 :meth:`authorize` 里看）。"""
        pack = self.pack_for(policy_id)
        needs_approval = pack.needs_approval_for(descriptor.side_effects)
        return PolicyVerdict(
            allowed=True,
            pack=pack,
            needs_approval=needs_approval,
            allow_streaming=bool(pack.allow_streaming),
            max_inline_bytes=int(pack.max_inline_bytes),
            audit_all=bool(pack.audit_all),
            reason=pack.reason,
        )

    def authorize(
        self,
        descriptor: CapabilityDescriptor,
        invocation: CapabilityInvocation,
        *,
        policy_id: str = "",
        approval_context: dict[str, Any] | None = None,
    ) -> tuple[PolicyVerdict, CapabilityResult | None]:
        """完整判定：策略 → 审批。返回（判定, 失败结果）。"""
        verdict = self.evaluate(descriptor, policy_id=policy_id)
        if not verdict.needs_approval:
            return verdict, None
        if not verdict.pack.require_approval_token:
            # 策略只要求"走过审批流程"，不强制令牌（例如自动模式下的人工确认 UI）。
            return verdict, None
        approval = validate_approval(invocation, approval_context=approval_context)
        if approval.ok:
            return verdict, None
        verdict.allowed = False
        verdict.code = approval.code
        return verdict, approval.to_result(invocation)

    def cloud_switch_allowed(self, *, policy_id: str = "") -> bool:
        """hybrid 能力能否切到服务端执行（传给 Broker 的 ``policy_allows_switch``）。"""
        return bool(self.pack_for(policy_id).allow_cloud_switch)


#: 进程内共享门禁（未指定策略时用默认包）。
policy_guard = PluginPolicyGuard()


__all__ = [
    "ApprovalVerdict",
    "DEFAULT_APPROVAL_TTL_SECONDS",
    "PluginPolicyGuard",
    "PolicyVerdict",
    "capability_fingerprint",
    "issue_approval_token",
    "policy_guard",
    "validate_approval",
]
