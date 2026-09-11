"""``CapabilityInvocation``：一次能力调用的完整请求（阶段 0 冻结）。

字段分四组，各自的"谁有权写"不同，代码里必须按组对待：

* **调用方意图**：``capability`` / ``arguments`` / ``scope`` / ``deadline`` —— 由 Skill
  或执行节点提出，参数是能力自己的结构化 Schema（不是字符串字典）；
* **服务端注入的关联标识**：``request_id`` / ``trace_id`` / ``session_binding`` ——
  调用方不得伪造，Broker 用它们做会话/工作区/设备绑定校验；
* **授权与幂等**：``approval_token`` / ``idempotency_key`` —— 审批令牌短期有效、
  不可重放；幂等键保证重试不重复产生副作用；
* **流式与取消**：``stream_cursor`` / 取消由 ``request_id`` 承担（见 Broker）。

``deadline`` 是**单调时钟**上的绝对秒数（``time.monotonic()`` 口径），不是"还剩多少
秒"：用相对时间会在排队/重试时被反复续期，等于没有上界。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lumi_contracts.plugins.manifest import CAPABILITY_NAME_RE


class SessionBinding(BaseModel):
    """能力查找的最小绑定集：不能只按 ``user_id`` 找 Provider。

    同一用户可能有多个设备/多个工作区/多个会话；只按用户匹配会把 A 工作区的读取
    路由到 B 工作区的 Provider（越权读文件）。
    """

    model_config = ConfigDict(extra="forbid")

    user_id: str = ""
    conversation_id: str = ""
    workspace_id: str = ""
    device_id: str = ""
    session_id: str = ""

    def matches(self, other: "SessionBinding", *, strict: bool = True) -> bool:
        """本绑定是否覆盖 ``other`` 的全部非空字段（默认要求严格相等）。"""
        for field in ("user_id", "conversation_id", "workspace_id", "device_id", "session_id"):
            mine = str(getattr(self, field) or "")
            theirs = str(getattr(other, field) or "")
            if not theirs:
                continue
            if mine != theirs:
                return False
        if strict and str(other.user_id or "") and str(self.user_id or "") != str(other.user_id):
            return False
        return True

    def key(self) -> str:
        """绑定指纹（用于租约索引与审计，不含正文）。"""
        parts = [
            str(self.user_id or ""),
            str(self.conversation_id or ""),
            str(self.workspace_id or ""),
            str(self.device_id or ""),
            str(self.session_id or ""),
        ]
        return "|".join(parts)


class CapabilityInvocation(BaseModel):
    """一次能力调用（跨服务端 ↔ 客户端 Provider 的唯一请求形状）。"""

    model_config = ConfigDict(extra="forbid")

    # ── 调用方意图 ──
    capability: str
    contract_version: int = Field(default=1, ge=1)
    #: 结构化参数：由能力自己的 ``input_schema`` 校验，禁止用字符串字典兜底。
    arguments: dict[str, Any] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    #: ``time.monotonic()`` 口径的绝对截止时间；0 表示由 Broker 按策略补默认值。
    deadline: float = 0.0
    #: 期望执行位置（""=由 Broker 按 Provider 注册情况选择）。
    deployment: str = ""

    # ── 服务端注入（调用方不得伪造）──
    request_id: str = ""
    trace_id: str = ""
    session_binding: SessionBinding = Field(default_factory=SessionBinding)
    job_id: str = ""
    node_id: str = ""

    # ── 授权与幂等 ──
    approval_token: str = ""
    idempotency_key: str = ""

    # ── 流式 ──
    stream_cursor: str = ""
    #: 调用方声明的超时预算（秒，>0）；Broker 会与策略上限取小。
    timeout_seconds: float = 0.0

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        """只校验能力名，**保留** ``@<n>`` 给 model_validator 收进版本。

        如果这里就把 ``@n`` 丢掉，``workspace.read@2`` 会被静默当成 ``@1``——
        调用方以为在请求版本 2，实际执行的是版本 1（版本漂移且无告警）。
        """
        text = str(value or "").strip()
        base = text.split("@", 1)[0]
        if not CAPABILITY_NAME_RE.match(base):
            raise ValueError(f"非法能力名：{text!r}")
        return text

    @model_validator(mode="after")
    def _normalize(self) -> "CapabilityInvocation":
        # ``capability`` 带 ``@n`` 时把版本收进 contract_version，保持寻址键唯一。
        raw = str(self.capability)
        if "@" in raw:
            base, _, version = raw.partition("@")
            self.capability = base.strip()
            if version.strip().isdigit() and int(version) > 0:
                self.contract_version = int(version)
        if not self.idempotency_key:
            # 幂等键缺省用 request_id：重试必须复用同一个键才有意义。
            self.idempotency_key = str(self.request_id or "")
        if self.timeout_seconds < 0:
            self.timeout_seconds = 0.0
        return self

    @property
    def qualified_capability(self) -> str:
        return f"{self.capability}@{self.contract_version}"

    def to_wire(self) -> dict[str, Any]:
        """跨进程传输载荷（客户端 Provider 收到的就是它）。"""
        return self.model_dump(mode="json", exclude_none=True)


__all__ = ["CapabilityInvocation", "SessionBinding"]
