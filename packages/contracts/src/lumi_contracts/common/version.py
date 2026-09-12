"""显式契约版本：``lumi.<name>@<n>``。

为什么不用 ``SkillOutputV2(SkillOutputV1)`` 这类继承表达版本：继承会把"版本"
伪装成类型层级，导致下游只能靠 isinstance 猜测兼容性，且无法表达"同名字段语义
变了"。这里用**可比较、可解析、可协商**的版本串：

* ``ContractVersion.parse("lumi.tool_response@2")`` → name/version；
* ``ContractVersion.is_compatible_with("lumi.tool_response@1")`` 判断兼容；
* 无法转换时由 Adapter 抛 ``UNSUPPORTED_CONTRACT_VERSION``，**不静默丢字段**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION_RE = re.compile(r"^(?P<name>lumi\.[a-z0-9_.]+)@(?P<version>\d+)$")


@dataclass(frozen=True, slots=True, order=True)
class ContractVersion:
    """一个具名契约版本。``order=True`` 让"是否支持 >= 某版本"可直接比较。"""

    name: str
    version: int

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("契约版本缺少 name")
        if int(self.version) < 1:
            raise ValueError("契约版本号必须 >= 1")

    @classmethod
    def parse(cls, value: str | "ContractVersion") -> "ContractVersion":
        if isinstance(value, ContractVersion):
            return value
        match = _VERSION_RE.match(str(value or "").strip())
        if match is None:
            raise ValueError(f"非法契约版本串：{value!r}（期望形如 lumi.tool_response@2）")
        return cls(name=match.group("name"), version=int(match.group("version")))

    def is_compatible_with(self, other: str | "ContractVersion") -> bool:
        """同名且版本不高于本版本即视为可读（向后兼容）。"""
        try:
            target = ContractVersion.parse(other)
        except ValueError:
            return False
        return self.name == target.name and target.version <= self.version

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"


def contract_version(name: str, version: int = 1) -> ContractVersion:
    """构造契约版本；``name`` 可写 ``tool_response`` 或完整 ``lumi.tool_response``。"""
    raw = str(name or "").strip()
    if not raw.startswith("lumi."):
        raw = f"lumi.{raw}"
    return ContractVersion(name=raw, version=int(version))


# ── 当前生效的契约版本（新增契约时在这里登记，Adapter 按登记表协商）──
TOOL_REQUEST = contract_version("tool_request", 1)
TOOL_RESPONSE = contract_version("tool_response", 1)
TOOL_SPEC = contract_version("tool_spec", 1)
EXECUTION_RESULT = contract_version("execution_result", 1)
SKILL_RESULT = contract_version("skill_result", 1)
STREAM_EVENT = contract_version("stream_event", 1)
JOB_RUN_VIEW = contract_version("job_run_view", 1)
TASK_PROFILE = contract_version("task_profile", 1)
ROUTE_DECISION = contract_version("route_decision", 1)
# ── 持久化（结果引用 / 步骤检查点）──
RESULT_REF = contract_version("result_ref", 1)
STEP_CHECKPOINT = contract_version("step_checkpoint", 1)
# ── 插件化/能力化（Capability Provider / Plugin）──
PLUGIN_MANIFEST = contract_version("plugin_manifest", 1)
CAPABILITY_DESCRIPTOR = contract_version("capability_descriptor", 1)
CAPABILITY_INVOCATION = contract_version("capability_invocation", 1)
CAPABILITY_RESULT = contract_version("capability_result", 1)
PROVIDER_LEASE = contract_version("provider_lease", 1)
PLUGIN_SNAPSHOT = contract_version("plugin_snapshot", 1)
VIEW_CONTRIBUTION = contract_version("view_contribution", 1)

KNOWN_CONTRACTS: frozenset[str] = frozenset(
    str(item)
    for item in (
        TOOL_REQUEST,
        TOOL_RESPONSE,
        TOOL_SPEC,
        EXECUTION_RESULT,
        SKILL_RESULT,
        STREAM_EVENT,
        JOB_RUN_VIEW,
        TASK_PROFILE,
        ROUTE_DECISION,
        RESULT_REF,
        STEP_CHECKPOINT,
        PLUGIN_MANIFEST,
        CAPABILITY_DESCRIPTOR,
        CAPABILITY_INVOCATION,
        CAPABILITY_RESULT,
        PROVIDER_LEASE,
        PLUGIN_SNAPSHOT,
        VIEW_CONTRIBUTION,
    )
)


__all__ = [
    "CAPABILITY_DESCRIPTOR",
    "CAPABILITY_INVOCATION",
    "CAPABILITY_RESULT",
    "ContractVersion",
    "EXECUTION_RESULT",
    "JOB_RUN_VIEW",
    "KNOWN_CONTRACTS",
    "PLUGIN_MANIFEST",
    "PLUGIN_SNAPSHOT",
    "PROVIDER_LEASE",
    "RESULT_REF",
    "ROUTE_DECISION",
    "SKILL_RESULT",
    "STEP_CHECKPOINT",
    "STREAM_EVENT",
    "TASK_PROFILE",
    "TOOL_REQUEST",
    "TOOL_RESPONSE",
    "TOOL_SPEC",
    "VIEW_CONTRIBUTION",
    "contract_version",
]
