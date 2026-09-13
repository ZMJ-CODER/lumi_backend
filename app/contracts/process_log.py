"""执行过程日志投影：既有 Job 状态 → 安全过程条目（任务气泡刷新恢复）。

背景：``ProcessLogEntry`` 契约（``packages/contracts/.../events/process.py``）与
SSE 出口（``app/contracts/events.py::SseEventEncoder``）都已就绪，但过程只活在
流里：页面刷新后 ``GET /agents/jobs/{job_id}`` 没有任何可恢复的过程，气泡空白。
本模块补上这个**后端缺口**，只做一件事：从**既有** Job 状态派生过程条目。

数据来源（全部是 Job 上已有的安全字段，不新增采集点）：``routing["steps"]``、
``job.nodes`` 的状态/结果、``routing["plan_text"]``、``routing["execution_state"]``、
``routing["route_decision"]``（只读，绝不写 routing 策略字段）。

三条硬规则：

* **kind 由后端判定**：``derive_kind(tool_name=...)``，没有声明工具时是 thinking；
* **文案复用既有面向用户的说明**（``presentation.step_action/intent_text/
  working_text/completed_text/failed_text``），不新增第二套话术；所有文本一律过
  ``sanitize_process_text``（去绝对路径/凭据/DSML 原始载荷）；
* **过程不是正文**：绝不落原始工具参数/原始响应/模型推理；步骤的
  ``result_summary`` 只能进过程条目的 ``summary``/``detail``，**不能**成为最终
  答复——最终答复只来自 ``job.result.final_answer``（见 ``lumi_orch.run_view``）。

条目按 ``entry_id`` 去重（``plan:<revision>`` / ``step:<step_id>``），因此
保存多次、SSE 重连、刷新轮询叠加都不会重复；合并结果有界（≤200）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from lumi_contracts import (
    ProcessLogEntry,
    ProcessStatus,
    derive_kind,
    merge_process_log,
    sanitize_process_text,
)
from lumi_contracts.events.process import (
    DETAIL_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
)
from lumi_contracts.persistence.run_view import PROCESS_LOG_MAX_ENTRIES

from app.agents.orchestration.presentation import (
    completed_text,
    failed_text,
    intent_text,
    step_action,
    working_text,
)

# 规划阶段条目的展示标题（气泡里的阶段标签，不是对已完成工作的断言）。
PLAN_ENTRY_TITLE = "执行计划"

# 步骤/节点状态 → 契约过程状态。
# - waiting_approval 与 SSE 出口一致记为 running（正在推进，等待人工放行）；
# - **uncertain 必须原样保留**（方案《结果存储、检查点与恢复》§4.3）：它表示"副作用
#   可能在途、等人工确认"。既不能算 completed（会抹掉恢复最需要看见的事实），也不能
#   算 failed（会被当成明确失败而重跑）；前端对它给独立文案；
# - cancelled/skipped 记为 cancelled（用户主动终止，不是失败）；
# - interrupted 记为 failed（执行被系统中断，确实没完成）。
_STEP_STATUS_TO_PROCESS: dict[str, ProcessStatus] = {
    "pending": ProcessStatus.PENDING,
    "ready": ProcessStatus.PENDING,
    "planned": ProcessStatus.PENDING,
    "paused": ProcessStatus.PENDING,
    "waiting_approval": ProcessStatus.RUNNING,
    "running": ProcessStatus.RUNNING,
    "retrying": ProcessStatus.RUNNING,
    "escalated": ProcessStatus.RUNNING,
    "in_progress": ProcessStatus.RUNNING,
    "completed": ProcessStatus.COMPLETED,
    "succeeded": ProcessStatus.COMPLETED,
    "failed": ProcessStatus.FAILED,
    "error": ProcessStatus.FAILED,
    "interrupted": ProcessStatus.FAILED,
    "uncertain": ProcessStatus.UNCERTAIN,
    "cancelled": ProcessStatus.CANCELLED,
    "canceled": ProcessStatus.CANCELLED,
    "skipped": ProcessStatus.CANCELLED,
    "expired": ProcessStatus.EXPIRED,
}

#: 过程状态词表（未登记的状态原样透传，不硬塞进旧枚举）。
_PROCESS_STATUS_VALUES: frozenset[str] = frozenset(str(item) for item in ProcessStatus)


class _StepNode:
    """``presentation`` 文案函数所需的最小节点形状。

    过程日志可能来自只有 ``routing["steps"]`` 的快照（没有 TaskNode），此时用
    这个轻量对象兜住 ``params`` / ``name``，让既有文案函数照常工作。
    """

    __slots__ = ("id", "metadata", "name", "params")

    def __init__(self, *, step_id: str, title: str, tool: str) -> None:
        self.id = step_id
        self.name = title
        self.params = {"preferred_tool": tool} if tool else {}
        self.metadata: dict = {}


def _attr(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        value = obj.get(key, default)
    else:
        value = getattr(obj, key, default)
    return default if value is None else value


def _text(value: Any) -> str:
    return str(value or "").strip()


def _process_status(*values: Any) -> ProcessStatus | str:
    """按「步骤状态优先、节点状态兜底」判定过程状态。

    已登记的状态映射成契约枚举；**未登记但有值**的状态原样透传（例如 ``uncertain``
    的旧别名、将来新增的检查点状态）——绝不兜底成 pending/completed，否则刷新后的
    过程日志会与实时帧说两套口径。
    """
    for value in values:
        key = _text(getattr(value, "value", value)).casefold()
        if not key:
            continue
        mapped = _STEP_STATUS_TO_PROCESS.get(key)
        if mapped is not None:
            return mapped
        if key in _PROCESS_STATUS_VALUES:
            return ProcessStatus(key)
        return key
    return ProcessStatus.PENDING


def _iso_time(value: Any) -> str:
    """epoch 秒 → ISO 时间；没有时间戳时返回空串（契约允许）。"""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _step_tool(step: dict, node: Any) -> str:
    """步骤声明的工具名（``routing.steps[].tool``，其次节点 ``params.preferred_tool``）。"""
    tool = _text(step.get("tool"))
    if not tool:
        params = _attr(node, "params", {}) or {}
        if isinstance(params, dict):
            tool = _text(params.get("preferred_tool"))
    return tool


def _step_occurred_at(step: dict, node: Any) -> str:
    for value in (
        _attr(node, "completed_at", None),
        _attr(node, "started_at", None),
        _attr(node, "created_at", None),
        step.get("completed_at"),
        step.get("started_at"),
    ):
        stamp = _iso_time(value)
        if stamp:
            return stamp
    return ""


def _completed_summary(node: Any, step: dict, result: Any) -> str:
    """完成摘要：优先复用已经落地的安全文案，其次步骤级摘要，最后现算。

    ``display.completed`` 就是 ``presentation.completed_text`` 的落库副本，
    与前端既有的展示逐字一致，因此优先取它，避免同一件事两处措辞。
    """
    if isinstance(result, dict):
        display = result.get("display")
        if isinstance(display, dict) and _text(display.get("completed")):
            return _text(display["completed"])
    from_step = _text(step.get("result_summary"))
    if from_step:
        return from_step
    return completed_text(node, result if isinstance(result, dict) else None)


def _uncertain_summary(node: Any, step: dict, result: Any) -> str:
    """``uncertain`` 步骤的文案：说明"副作用可能在途、需要确认"，绝不说"已完成"。

    优先复用节点/步骤上已经落地的失败文案（``presentation.failed_text`` 会把真实原因
    说出来），再补一句"状态不确定、不会自动重跑"，让前端不必自己拼这句话。
    """
    error = _text(step.get("error")) or _text(_attr(node, "error", ""))
    head = failed_text(node, error) if error else str(_attr(node, "name", "") or "该步骤")
    return f"{head}；副作用状态不确定，不会自动重跑，请确认后再继续。"


def _step_summary(node: Any, step: dict, result: Any, status: ProcessStatus | str) -> str:
    if status is ProcessStatus.COMPLETED:
        return _completed_summary(node, step, result)
    if status is ProcessStatus.FAILED:
        error = _text(step.get("error")) or _text(_attr(node, "error", ""))
        return failed_text(node, error)
    if status is ProcessStatus.UNCERTAIN or str(status) == ProcessStatus.UNCERTAIN.value:
        return _uncertain_summary(node, step, result)
    if status is ProcessStatus.RUNNING:
        return working_text(node)
    return intent_text(node)


def _preflight_entry(job_id: str, routing: dict, occurred_at: str) -> ProcessLogEntry | None:
    """预检失败的 process 事件 → 过程条目（routing 里没有该载荷时返回 None）。

    载荷由 ``CapabilityPreflightService`` 的 ``preflight_process_notice`` 产出（canonical
    画像接入后才有事实）；``entry_id`` 与 SSE 发射点一致，刷新后与实时帧合并成同一行。
    """
    from app.agents.orchestration.capability_preflight_service import (
        PREFLIGHT_NOTICE_ENTRY_ID,
        PREFLIGHT_NOTICE_KEY,
    )

    notice = routing.get(PREFLIGHT_NOTICE_KEY)
    if not isinstance(notice, dict) or not notice:
        return None
    return ProcessLogEntry.from_event(
        {"entry_id": PREFLIGHT_NOTICE_ENTRY_ID, **notice},
        job_id=job_id,
        occurred_at=occurred_at,
    )


def _model_routing_entry(job_id: str, routing: dict, occurred_at: str) -> ProcessLogEntry | None:
    """模型路由/降级的 process 事件 → 过程条目（routing 里没有该载荷时返回 None）。

    载荷由 ``app.core.model_capability_router`` 产出（``MODEL_CAPABILITY_ROUTER_V2``
    打开且真的换档/降级/阻断时才有）；``entry_id`` 与 SSE 发射点一致，
    刷新后与实时帧合并成同一行。
    """
    from app.core.model_capability_router import (
        MODEL_ROUTING_NOTICE_ENTRY_ID,
        MODEL_ROUTING_NOTICE_KEY,
    )

    notice = routing.get(MODEL_ROUTING_NOTICE_KEY)
    if not isinstance(notice, dict) or not notice:
        return None
    return ProcessLogEntry.from_event(
        {"entry_id": MODEL_ROUTING_NOTICE_ENTRY_ID, **notice},
        job_id=job_id,
        occurred_at=occurred_at,
    )


def _route_detail(routing: dict) -> str:
    """路由/策略的**只读**审计摘要（枚举值，非自由文本，不含用户原文）。

    这里只读 ``routing``，不写回任何策略字段；``route_decision`` 仍是唯一权威来源
    （见 ``app/agents/orchestration/route_snapshot.py``）。
    """
    decision = routing.get("route_decision")
    decision = decision if isinstance(decision, dict) else {}
    profile = decision.get("task_profile")
    profile = profile if isinstance(profile, dict) else {}
    parts: list[str] = []
    for key, value in (
        ("route_mode", decision.get("route_mode") or routing.get("route_mode")),
        ("complexity", profile.get("complexity") or routing.get("complexity")),
        ("safety_action", decision.get("safety_action") or routing.get("safety_action")),
    ):
        text = _text(value)
        if text:
            parts.append(f"{key}={text}")
    return " | ".join(parts)


def _plan_entry(
    *,
    job_id: str,
    routing: dict,
    plan_text: str,
    plan_revision: int,
    canonical: str,
    occurred_at: str,
) -> ProcessLogEntry:
    """规划阶段条目：计划文本 + 路由审计摘要（都过安全摘要）。"""
    entry_id = f"plan:r{max(1, int(plan_revision or 1))}"
    # 只有仍在 planning 时才是进行中；一旦计划产出（waiting_run 起）即视为已完成。
    status = ProcessStatus.RUNNING if canonical in {"", "planning"} else ProcessStatus.COMPLETED
    return ProcessLogEntry(
        id=entry_id,
        entry_id=entry_id,
        kind=derive_kind(event_type="process"),
        title=sanitize_process_text(PLAN_ENTRY_TITLE, limit=TITLE_MAX_CHARS),
        summary=sanitize_process_text(plan_text, limit=SUMMARY_MAX_CHARS),
        detail=sanitize_process_text(_route_detail(routing), limit=DETAIL_MAX_CHARS),
        status=status,
        sequence=0,
        occurred_at=occurred_at,
        job_id=job_id,
    )


def _step_entry(
    *,
    job_id: str,
    step: dict,
    node: Any,
    sequence: int,
) -> ProcessLogEntry | None:
    """单个步骤 → 过程条目；没有稳定 step_id 时返回 None（去重键必须稳定）。"""
    step_id = _text(step.get("id")) or _text(step.get("step_id"))
    if not step_id:
        return None
    tool = _step_tool(step, node)
    display_node = node if node is not None else _StepNode(
        step_id=step_id,
        title=_text(step.get("title")),
        tool=tool,
    )
    status = _process_status(step.get("status"), _attr(node, "status", ""))
    # ``node.result`` 是节点快照里的结果摘要来源；只取已落地的安全展示字段。
    result = _attr(node, "result", None) or step.get("result")
    entry = ProcessLogEntry(
        id=f"step:{step_id}",
        entry_id=f"step:{step_id}",
        kind=(
            derive_kind(tool_name=tool)
            if tool
            else derive_kind(event_type="step_started")
        ),
        title=sanitize_process_text(step_action(display_node), limit=TITLE_MAX_CHARS),
        summary=sanitize_process_text(
            _step_summary(display_node, step, result, status),
            limit=SUMMARY_MAX_CHARS,
        ),
        # 步骤不落 detail：错误/结果原文可能夹带路径、堆栈或工具原始输出，
        # 过程日志只保留 title/summary 两级安全摘要（summary 已过净化）。
        status=status,
        step_id=step_id,
        tool_name=tool[:80],
        sequence=int(sequence),
        occurred_at=_step_occurred_at(step, node),
        job_id=job_id,
        **dispatch_labels_for_step(step, tool),
    )
    return entry


def dispatch_labels_for_step(step: dict, tool: str) -> dict[str, str]:
    """步骤 → 结构化标签（能力/资源/Provider/模型可见名）。

    优先用步骤里**已经落盘的**标签（调用方更清楚"模型当时叫什么"），缺失时按工具名
    从统一能力目录补。全部是闭集词汇；认不出工具就什么都不填（老工具/本机动作）。
    """
    labels: dict[str, str] = {}
    for key in ("capability", "resource_type", "provider_id", "provider_name", "display_name"):
        value = str(step.get(key) or "")
        if value:
            labels[key] = value
    if labels.get("capability") and labels.get("resource_type"):
        return labels
    if not tool:
        return labels
    try:
        from app.agents.capabilities.resource_dispatch import dispatch_labels

        derived = dispatch_labels(tool)
    except Exception as exc:  # noqa: BLE001 - 标签补全失败不能影响过程日志
        logger.debug("[process-log] 结构化标签补全失败 {}: {}", str(tool)[:40], str(exc)[:120])
        return labels
    for key, value in derived.items():
        labels.setdefault(key, value)
    return labels


def process_log_from_job(job: Any) -> list[ProcessLogEntry]:
    """从既有 Job 状态派生过程条目（有序、稳定 entry_id、只含安全摘要）。

    Job 可以是 ``Job`` 对象或它的快照 dict；缺失字段一律按空处理，旧快照不会报错。
    """
    job_id = _text(_attr(job, "job_id", ""))
    routing = _attr(job, "routing", {}) or {}
    routing = routing if isinstance(routing, dict) else {}
    nodes = _attr(job, "nodes", []) or []
    nodes_by_id = {
        _text(_attr(node, "id", "")): node for node in nodes if _text(_attr(node, "id", ""))
    }
    plan_text = _text(routing.get("plan_text")) or _text(_attr(job, "plan_text", ""))
    canonical = _text(routing.get("execution_state"))
    entries: list[ProcessLogEntry] = []
    if plan_text or canonical or routing.get("route_decision"):
        entries.append(
            _plan_entry(
                job_id=job_id,
                routing=routing,
                plan_text=plan_text,
                plan_revision=int(routing.get("plan_revision") or 1),
                canonical=canonical,
                occurred_at=_iso_time(_attr(job, "created_at", None)),
            )
        )
    preflight = _preflight_entry(
        job_id, routing, _iso_time(_attr(job, "created_at", None))
    )
    if preflight is not None:
        entries.append(preflight)
    model_routing = _model_routing_entry(
        job_id, routing, _iso_time(_attr(job, "created_at", None))
    )
    if model_routing is not None:
        entries.append(model_routing)
    for index, raw in enumerate(routing.get("steps") or []):
        if not isinstance(raw, dict):
            continue
        entry = _step_entry(
            job_id=job_id,
            step=raw,
            node=nodes_by_id.get(_text(raw.get("id")) or _text(raw.get("step_id"))),
            sequence=index + 1,
        )
        if entry is not None:
            entries.append(entry)
    return entries


def _usable_entries(rows: Any) -> list[ProcessLogEntry]:
    """读回持久化条目时逐条容错：一条坏数据不能让刷新整体失败。"""
    entries: list[ProcessLogEntry] = []
    for raw in rows or []:
        if isinstance(raw, ProcessLogEntry):
            entries.append(raw)
            continue
        try:
            entries.append(ProcessLogEntry.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("过程日志条目反序列化失败（已跳过）: {}", str(exc)[:120])
    return entries


def merge_job_process_log(job: Any, *, limit: int = PROCESS_LOG_MAX_ENTRIES) -> list[ProcessLogEntry]:
    """持久化条目 + 由当前 Job 状态现推导的条目 → 合并去重（≤limit）。

    持久化在前、现推导在后：同一条目后到的一方补状态/摘要（running → completed），
    因此刷新读到的永远是"最新状态 + 历史顺序"。
    """
    persisted = _usable_entries(_attr(job, "process_log", []) or [])
    return merge_process_log(persisted, process_log_from_job(job), limit=limit)


def process_log_payload(entries: Any) -> list[dict]:
    """契约条目 → JSON-safe 载荷（前端 ``process_log`` 字段的唯一序列化点）。"""
    return [entry.model_dump(mode="json", exclude_none=True) for entry in entries or []]


def persist_process_log(job: Any, *, limit: int = PROCESS_LOG_MAX_ENTRIES) -> int:
    """把派生条目合并进 ``job.process_log``（原地、去重、有界），返回条目数。

    存 Job 快照本身而**不是** routing：routing 只存路由/策略，执行过程属于
    run_view/process。返回条目数便于调用方断言/观测。
    """
    merged = merge_job_process_log(job, limit=limit)
    job.process_log = process_log_payload(merged)
    return len(merged)


__all__ = [
    "PLAN_ENTRY_TITLE",
    "merge_job_process_log",
    "persist_process_log",
    "process_log_from_job",
    "process_log_payload",
]
