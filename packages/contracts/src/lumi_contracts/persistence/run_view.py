"""持久化契约：``JobRunView`` 等可恢复快照。

要求（方案第四、八节）：

* ``JobRunView`` 是**持久化快照**，不要直接拿 ``ExecutionResult`` 代替它；
* 刷新（GET /agents/jobs/{id}）后仍能恢复：状态、步骤、下一步动作、最终答复；
* 只放恢复所需的最小信息，正文按引用解析。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from lumi_contracts.events.lifecycle import RunState
from lumi_contracts.events.process import ProcessLogEntry

# 过程日志在快照里的条数上限（滚动窗口，避免状态库无界增长）。
PROCESS_LOG_MAX_ENTRIES = 200

# 快照里**单条**过程日志/步骤展示文本的字节上限（超出的更早内容走归档引用）。
PROCESS_LOG_ENTRY_MAX_BYTES = 2_048

# 单个 Job 快照的字节上限：超过即告警 + 强制收缩（方案 §5.3 三条硬规则之一）。
SNAPSHOT_MAX_BYTES = 256 * 1024

# 最终答复在快照里的长度上限（可展示即可，正文另有引用）。
FINAL_ANSWER_MAX_CHARS = 20000


def clip_utf8(text: object, max_bytes: int = PROCESS_LOG_ENTRY_MAX_BYTES) -> str:
    """按 **UTF-8 字节**截断文本（不切坏多字节字符；超限补省略号）。"""
    raw = str(text or "")
    if not raw:
        return ""
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return raw
    budget = max(0, max_bytes - len("…".encode("utf-8")))
    return encoded[:budget].decode("utf-8", errors="ignore").rstrip() + "…"


def _dump_size(payload: Any) -> int:
    import json

    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _bound_entry(entry: Any) -> ProcessLogEntry:
    """硬规则②：单条过程日志的 ``summary`` / ``detail`` 按字节截断（正文走引用）。"""
    if not isinstance(entry, ProcessLogEntry):
        entry = ProcessLogEntry.model_validate(entry)
    summary = clip_utf8(entry.summary)
    detail = clip_utf8(entry.detail)
    if summary == entry.summary and detail == entry.detail:
        return entry
    return entry.model_copy(update={"summary": summary, "detail": detail})


class StepView(BaseModel):
    """单步展示与恢复信息。"""

    id: str = ""
    title: str = ""
    status: str = ""
    runtime_status: str = ""
    tool: str = ""
    # 展示用短输出；完整结果按 result_ref 解析。
    output: str = ""
    error: str | None = None
    error_code: str | None = None
    depends_on: tuple[str, ...] = ()
    resource_claims: tuple[str, ...] = ()
    effect_status: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    duration_ms: int | None = None
    result_ref: dict[str, Any] | None = None


class JobRunView(BaseModel):
    """一次运行的持久化快照。"""

    job_id: str = ""
    conversation_id: str = ""
    status: RunState = RunState.PENDING
    # 计划优先：下一步动作（run_next 等），刷新后据此恢复。
    next_action: str = ""
    plan_text: str = ""
    plan_revision: int = 1
    steps: list[StepView] = Field(default_factory=list)
    # 覆盖度/路由等审计摘要（不含用户原文与正文）。
    routing: dict[str, Any] = Field(default_factory=dict)
    # 执行过程日志：安全摘要 + 去重键 + 状态；刷新后据此恢复过程气泡。
    # 只放过程（不含原始参数/原始响应/模型内部推理），正文走 result_ref/artifact。
    process_log: list[ProcessLogEntry] = Field(default_factory=list)
    final_answer: str = ""
    # 事件顺序水位：与 SSE ``seq`` 同源，前端据此判断"快照是否已覆盖到我的 lastSeq"。
    last_seq: int = 0
    # 产物引用（只放引用与元数据，不放下载地址/令牌/正文）：
    # 刷新后据此恢复 Artifact 卡片，下载仍走受权限保护的下载接口。
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    # 声明式视图引用（``view_id`` + ``data_ref``；数据本身按需再取）。
    views: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    updated_at: float = 0.0
    version: int = 1
    # 过程日志归档（更早的日志转存为 Artifact，快照只留引用 + 条数）。
    log_archive_ref: str = ""
    log_archive_count: int = 0
    # 快照是否被强制收缩过（超过 SNAPSHOT_MAX_BYTES 时置位）。
    truncated: bool = False

    @field_validator("final_answer")
    @classmethod
    def _bound_final_answer(cls, value: str) -> str:
        text = str(value or "")
        return text[:FINAL_ANSWER_MAX_CHARS]

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED, RunState.INTERRUPTED
        }

    def with_process_log(self, entries: list[Any] | None) -> "JobRunView":
        """按去重键合并过程日志（SSE / 轮询 / 刷新叠加都不重复），并做滚动窗口。

        同时执行硬规则②：单条 ``summary`` / ``detail`` 超过
        :data:`PROCESS_LOG_ENTRY_MAX_BYTES` 时按 UTF-8 安全截断——快照里永远不留长正文。
        """
        from lumi_contracts.events.process import merge_process_log

        merged = merge_process_log(self.process_log, entries, limit=PROCESS_LOG_MAX_ENTRIES)
        bounded = [_bound_entry(entry) for entry in merged]
        return self.model_copy(update={"process_log": bounded})

    def roll_process_log(
        self, entries: list[Any] | None, *, keep: int = PROCESS_LOG_MAX_ENTRIES
    ) -> tuple["JobRunView", list[Any]]:
        """合并日志并**把窗口外的更早日志交还给调用方归档**。

        硬规则①：快照最多留最近 ``keep`` 条，更早的由调用方转存为
        ``process_log_archive`` Artifact，再用 :meth:`with_log_archive` 记引用与条数。
        返回 ``(新视图, 需要归档的更早条目)``。
        """
        from lumi_contracts.events.process import merge_process_log

        # limit=0 → 不截断（先拿到全量，再自己切"保留窗口 / 归档"两段）。
        merged = merge_process_log(self.process_log, entries, limit=0)
        if len(merged) <= keep:
            return self.model_copy(update={"process_log": [_bound_entry(item) for item in merged]}), []
        archived = merged[: len(merged) - keep]
        kept = [_bound_entry(item) for item in merged[len(merged) - keep :]]
        previous = int(self.log_archive_count or 0)
        return (
            self.model_copy(
                update={
                    "process_log": kept,
                    "log_archive_count": previous + len(archived),
                }
            ),
            archived,
        )

    def with_log_archive(self, ref: str, *, count: int | None = None) -> "JobRunView":
        """记录归档引用（``process_log_archive`` 的 Artifact 引用 + 归档条数）。"""
        update: dict[str, Any] = {"log_archive_ref": str(ref or "")}
        if count is not None:
            update["log_archive_count"] = max(int(self.log_archive_count or 0), int(count))
        return self.model_copy(update=update)

    def note_seq(self, seq: int) -> "JobRunView":
        """推进事件水位（只增不减；乱序/重复的快照不得把水位拉回去）。"""
        try:
            value = int(seq)
        except (TypeError, ValueError):
            return self
        if value <= int(self.last_seq or 0):
            return self
        return self.model_copy(update={"last_seq": value})

    def with_artifacts(self, refs: list[Any] | None, *, limit: int = 50) -> "JobRunView":
        """按 ``artifact_id`` 去重合并产物引用（保序、有界）。"""
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for source in (self.artifact_refs or [], refs or []):
            for raw in source or []:
                item = raw if isinstance(raw, dict) else (
                    raw.model_dump(mode="json", exclude_none=True) if hasattr(raw, "model_dump") else None
                )
                if not isinstance(item, dict):
                    continue
                key = str(item.get("artifact_id") or item.get("ref_id") or item.get("filename") or "")
                if not key:
                    continue
                if key not in merged:
                    order.append(key)
                merged[key] = {**merged.get(key, {}), **item}
        rows = [merged[key] for key in order]
        return self.model_copy(update={"artifact_refs": rows[-limit:] if limit else rows})

    def to_snapshot(self) -> dict[str, Any]:
        """可写库的快照：**已经过体积收缩**（超限自动收缩并告警）。"""
        return self.bounded_snapshot()

    # ── 快照膨胀硬规则（方案 §5.3）────────────────────────

    def snapshot_size_bytes(self) -> int:
        """当前快照（序列化后）的字节数。"""
        return _dump_size(self.to_snapshot_unbounded())

    def to_snapshot_unbounded(self) -> dict[str, Any]:
        """不做收缩的原始快照（仅供体积度量/排障，不要直接写库）。"""
        return self.model_dump(mode="json", exclude_none=True)

    def shrink(self) -> "JobRunView":
        """硬规则③：体积超限时的**强制收缩**（老步骤只留 status/error_code/result_ref）。

        策略（从"最不伤恢复"到"最伤"，逐级做，直到回到阈值内）：
        1. 丢掉较早步骤的 ``output`` / ``title``（保留 status/error_code/result_ref/depends_on）；
        2. 过程日志只留最近 50 条，其余计入 ``log_archive_count``（由调用方归档）；
        3. 仍然超限则丢掉过程日志的 ``detail``。
        """
        view = self
        if view.snapshot_size_bytes() <= SNAPSHOT_MAX_BYTES:
            return view
        steps = [
            (
                step.model_copy(update={"output": "", "title": ""})
                if index < max(0, len(view.steps) - 10)
                else step
            )
            for index, step in enumerate(view.steps)
        ]
        view = view.model_copy(update={"steps": steps, "truncated": True})
        if view.snapshot_size_bytes() <= SNAPSHOT_MAX_BYTES:
            return view
        from lumi_contracts.events.process import merge_process_log

        merged = merge_process_log(view.process_log, [], limit=0)
        if len(merged) > 50:
            view = view.model_copy(
                update={
                    "process_log": merged[-50:],
                    "log_archive_count": int(view.log_archive_count or 0) + (len(merged) - 50),
                    "truncated": True,
                }
            )
        if view.snapshot_size_bytes() <= SNAPSHOT_MAX_BYTES:
            return view
        thinned = [entry.model_copy(update={"detail": ""}) for entry in view.process_log]
        return view.model_copy(update={"process_log": thinned, "truncated": True})

    def bounded_snapshot(self, *, warn: bool = True) -> dict[str, Any]:
        """**写入路径唯一入口**：先量体积，超限则收缩 + 告警，再返回可写库的快照。"""
        raw = self.to_snapshot_unbounded()
        size = _dump_size(raw)
        if size <= SNAPSHOT_MAX_BYTES:
            return raw
        shrunk = self.shrink()
        bounded = shrunk.to_snapshot_unbounded()
        new_size = _dump_size(bounded)
        if warn:
            try:
                from loguru import logger

                logger.warning(
                    "[job-run-view] 快照超限已收缩 job={} {}B → {}B（老日志请按 log_archive_ref 加载）",
                    str(self.job_id)[:12],
                    size,
                    new_size,
                )
            except Exception:  # noqa: BLE001 - 告警失败不影响写入
                pass
        return bounded


__all__ = [
    "FINAL_ANSWER_MAX_CHARS",
    "PROCESS_LOG_ENTRY_MAX_BYTES",
    "PROCESS_LOG_MAX_ENTRIES",
    "SNAPSHOT_MAX_BYTES",
    "JobRunView",
    "StepView",
    "clip_utf8",
]
