"""任务画像影子比对契约（方案 4 §5）：新旧逻辑并行记录差异的**结构化形状**。

影子期的目标是"先并行记录、后切换、最后删除"：

* 新旧判定同时对每个请求运行；
* 记录 ``legacy_requires_orchestration`` / ``profile_requires_orchestration`` 与
  **差异类型**（旧读新写 / 旧写新读 / 复杂度分歧 / 一致）；
* 记录实际采用哪一方（影子期仍走旧逻辑）与耗时/超时事实；
* 达标（误判差异率收敛、超时率可接受）后才切换；切换后旧词表只留诊断兜底。

**只记录枚举与原因码**：不含用户原文、不含模型推理。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class DiscrepancyType(StrEnum):
    """旧判定与新画像的差异类型（收敛口径，不做二次发明）。"""

    #: 两边一致。
    NONE = "none"
    #: 旧：只读 / 新：写入 —— **最重要回归场景**，切画像后必须进入受控编排并注入写工具。
    LEGACY_READ_PROFILE_WRITE = "legacy_read_profile_write"
    #: 旧：写入 / 新：只读 —— 以新画像为准，不注入写工具（消除误触发写入）。
    LEGACY_WRITE_PROFILE_READ = "legacy_write_profile_read"
    #: 旧：简单 / 新：复杂 —— 以新画像为准，进入编排。
    LEGACY_SIMPLE_PROFILE_COMPLEX = "legacy_simple_profile_complex"
    #: 旧：复杂 / 新：简单。
    LEGACY_COMPLEX_PROFILE_SIMPLE = "legacy_complex_profile_simple"
    #: 分类器超时 / 输出非法 JSON → 降级 heuristic。
    ASSESSOR_FALLBACK = "assessor_fallback"


class RouteSource(StrEnum):
    """实际采用哪一方（影子期固定 ``legacy``；验证期/切换后为 ``profile``）。"""

    LEGACY = "legacy"
    PROFILE = "profile"


class ShadowRecord(BaseModel):
    """一次请求的影子比对记录（可落 Job 快照 / 可聚合统计）。"""

    trace_id: str = ""
    job_id: str = ""
    legacy_requires_orchestration: bool = False
    profile_requires_orchestration: bool = False
    discrepancy_type: DiscrepancyType = DiscrepancyType.NONE
    selected_route_source: RouteSource = RouteSource.LEGACY
    #: 新旧模式（稳定枚举串；便于统计"旧读新写"落在哪个模式）。
    legacy_route_mode: str = ""
    profile_route_mode: str = ""
    #: 原因码（两侧各自的稳定原因码）。
    legacy_reason_code: str = ""
    profile_reason_code: str = ""
    #: 分类来源与耗时（毫秒）；超时/非法 JSON 时 ``confidence_source=heuristic``。
    confidence_source: str = ""
    assessor_ms: int = 0
    assessor_timed_out: bool = False
    #: 结论是否以新画像为准（影子期为 False；切换后为 True）。
    profile_authoritative: bool = False

    @property
    def differs(self) -> bool:
        return self.discrepancy_type is not DiscrepancyType.NONE

    def as_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=True)


#: 需要在切换前收敛的差异类型（写入相关的误判比"复杂度分歧"更严重）。
CRITICAL_DISCREPANCIES: frozenset[str] = frozenset(
    {
        DiscrepancyType.LEGACY_READ_PROFILE_WRITE.value,
        DiscrepancyType.LEGACY_WRITE_PROFILE_READ.value,
    }
)


def classify_discrepancy(
    *,
    legacy_requires_orchestration: bool,
    profile_requires_orchestration: bool,
    legacy_complex: bool | None = None,
    profile_complex: bool | None = None,
) -> DiscrepancyType:
    """按"写入差异优先、其次复杂度差异"分类（顺序即优先级）。"""
    if legacy_requires_orchestration and not profile_requires_orchestration:
        return DiscrepancyType.LEGACY_WRITE_PROFILE_READ
    if profile_requires_orchestration and not legacy_requires_orchestration:
        return DiscrepancyType.LEGACY_READ_PROFILE_WRITE
    if legacy_complex is not None and profile_complex is not None and legacy_complex != profile_complex:
        return (
            DiscrepancyType.LEGACY_SIMPLE_PROFILE_COMPLEX
            if profile_complex
            else DiscrepancyType.LEGACY_COMPLEX_PROFILE_SIMPLE
        )
    return DiscrepancyType.NONE


def discrepancy_should_use_profile(discrepancy: DiscrepancyType | str) -> bool:
    """该差异是否**必须以新画像为准**（切换后的判定；影子期只记录不改变行为）。

    任何非 ``none`` 的差异都以新画像为准：四类差异都是"旧词表与画像不一致"，而画像
    是唯一意图事实源。"旧读新写"是"用户要求创建文件、模型只拿到读取工具"的根因，
    "旧写新读"则是误触发写入的风险点——两者都不能让旧词表赢。
    """
    return str(discrepancy) != DiscrepancyType.NONE.value


def is_critical_discrepancy(discrepancy: DiscrepancyType | str) -> bool:
    """是否属于"写入相关"的关键差异（切换前必须先收敛到 0）。"""
    return str(discrepancy) in CRITICAL_DISCREPANCIES


class ShadowReport(BaseModel):
    """影子期聚合报告（方案 §5.2：达标才切换）。"""

    total: int = 0
    diff_total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    legacy_orchestration_total: int = 0
    profile_orchestration_total: int = 0
    assessor_timeout_total: int = 0
    #: 平均分类耗时（毫秒）。
    assessor_ms_avg: int = 0
    profile_authoritative: bool = False

    @property
    def diff_rate(self) -> float:
        return (self.diff_total / self.total) if self.total else 0.0

    @property
    def timeout_rate(self) -> float:
        return (self.assessor_timeout_total / self.total) if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            **self.model_dump(mode="json", exclude_none=True),
            "diff_rate": round(self.diff_rate, 4),
            "timeout_rate": round(self.timeout_rate, 4),
        }


def build_shadow_report(
    records: list[ShadowRecord],
    *,
    profile_authoritative: bool = False,
) -> ShadowReport:
    """记录列表 → 聚合报告（纯函数，可直接单测）。"""
    by_type: dict[str, int] = {}
    timeouts = 0
    legacy_total = 0
    profile_total = 0
    elapsed = 0
    for record in records or ():
        kind = str(record.discrepancy_type)
        by_type[kind] = by_type.get(kind, 0) + 1
        legacy_total += 1 if record.legacy_requires_orchestration else 0
        profile_total += 1 if record.profile_requires_orchestration else 0
        timeouts += 1 if record.assessor_timed_out else 0
        elapsed += max(0, int(record.assessor_ms or 0))
    total = len(records or ())
    return ShadowReport(
        total=total,
        diff_total=sum(count for kind, count in by_type.items() if kind != DiscrepancyType.NONE.value),
        by_type=by_type,
        legacy_orchestration_total=legacy_total,
        profile_orchestration_total=profile_total,
        assessor_timeout_total=timeouts,
        assessor_ms_avg=(elapsed // total) if total else 0,
        profile_authoritative=profile_authoritative,
    )


__all__ = [
    "CRITICAL_DISCREPANCIES",
    "DiscrepancyType",
    "RouteSource",
    "ShadowRecord",
    "ShadowReport",
    "build_shadow_report",
    "classify_discrepancy",
    "discrepancy_should_use_profile",
    "is_critical_discrepancy",
]
