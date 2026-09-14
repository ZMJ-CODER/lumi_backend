"""可观测性：Sentry 错误上报 + Prometheus 指标.

- Sentry：配置 SENTRY_DSN 后自动捕获未处理异常（FastAPI 集成）；
- Prometheus：/metrics 暴露 HTTP 请求量/延迟、任务结果、技能调用等指标，
  供 Prometheus + Grafana 采集。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Sequence

from loguru import logger

from app.core.config import settings

# ── Sentry ──


def init_sentry() -> None:
    """初始化 Sentry（未配置 DSN 时跳过）."""
    if not settings.SENTRY_DSN:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment="production" if not settings.DEBUG else "development",
            traces_sample_rate=0.1,
            integrations=[
                StarletteIntegration(transaction_style="endpoint"),
                FastApiIntegration(transaction_style="endpoint"),
            ],
            send_default_pii=False,
        )
        logger.info("Sentry 已启用")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Sentry 初始化失败: {}", exc)


# ── Prometheus 指标 ──

_prometheus = None
_http_requests = None
_http_duration = None
_agent_jobs = None
_skill_calls = None
_skill_routing_modes = None
_rag_searches = None
_agent_routes = None
_agent_replans = None
_agent_route_duration = None
_agent_node_duration = None
_agent_channel_wait = None
_celery_queue_ready = None
_document_pipeline_state = None
_document_pipeline_oldest_age = None
_read_view_cache = None
_read_view_stage_duration = None
# v2 统一任务画像/执行策略观测。
_policy_routes = None
_policy_route_duration = None
_policy_first_delta = None
_policy_stream_duration = None
_planner_invoked = None
_agent_invoked = None
_workspace_read_duration = None
_job_snapshot_writes = None
# 降级成本监控（方案 §5）：Token/金额按"是否降级"分开记，率由 PromQL 算。
_llm_tokens = None
_llm_cost = None
_llm_calls = None
# 在途任务数（Gauge）：单任务平均成本的分母。
_jobs_active = None
_jobs_active_scan = None
# 工具链路：最终进模型的工具数（按场景/层），用于发现"工具被截断"的趋势。
_tool_window_layers = None

#: LLM 成本/用量指标的**共享低基数维度**。新增维度前先问：它的取值集合是否可枚举？
#: 只要答案不是"是"，就不要加——这是防指标爆炸的唯一一道闸。
_LLM_LABELS: list[str] = ["scene", "provider", "model", "fallback_from", "fallback_to", "is_fallback"]

#: 在途任务扫描的上限：单次 /metrics 抓取最多扫这么多键。
#: 没有上限时，任务多起来 SCAN 会拖慢（甚至阻塞）抓取端点，成为隐性故障源。
_ACTIVE_JOB_SCAN_LIMIT = 2000
#: 单次扫描的时间预算（秒）：超时即降级为"本轮不更新"，并置 degraded=1。
_ACTIVE_JOB_SCAN_BUDGET_SECONDS = 0.25


def _ensure_metrics():
    """懒加载 prometheus-client 指标（避免未安装/未启用时阻塞启动）."""
    global _prometheus, _http_requests, _http_duration, _agent_jobs, _skill_calls, _skill_routing_modes, _rag_searches
    global _agent_routes, _agent_replans, _agent_route_duration, _agent_node_duration
    global _agent_channel_wait
    global _celery_queue_ready, _document_pipeline_state, _document_pipeline_oldest_age
    global _read_view_cache, _read_view_stage_duration
    global _policy_routes, _policy_route_duration, _policy_first_delta, _policy_stream_duration
    global _planner_invoked, _agent_invoked, _workspace_read_duration, _job_snapshot_writes
    global _llm_tokens, _llm_cost, _llm_calls, _jobs_active, _jobs_active_scan, _tool_window_layers
    if _prometheus is not None:
        return True
    if not settings.METRICS_ENABLED:
        return False
    try:
        from prometheus_client import Counter, Gauge, Histogram

        _http_requests = Counter(
            "lumi_http_requests_total", "HTTP 请求总数", ["method", "path", "status"]
        )
        _http_duration = Histogram(
            "lumi_http_request_duration_seconds",
            "HTTP 请求耗时（秒）",
            ["method", "path"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
        )
        _agent_jobs = Counter("lumi_agent_jobs_total", "多智能体任务结果", ["status"])
        _skill_calls = Counter("lumi_skill_calls_total", "技能调用次数", ["skill", "success"])
        _skill_routing_modes = Counter(
            "lumi_skill_routing_modes_total",
            "Skill 候选路由模式；lexical_fallback 表示语义索引未就绪或不可用",
            ["scene", "mode"],
        )
        _rag_searches = Counter(
            "lumi_rag_searches_total", "RAG 检索次数", ["hits"]
        )
        _agent_routes = Counter(
            "lumi_agent_routes_total", "办公任务路由结果", ["level", "mode", "cache_hit"]
        )
        _agent_replans = Counter(
            "lumi_agent_replans_total", "办公任务升级或重规划", ["from_level", "to_level", "reason"]
        )
        _agent_route_duration = Histogram(
            "lumi_agent_route_duration_seconds",
            "办公任务评估与规划耗时",
            ["level", "mode"],
            buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60),
        )
        _agent_node_duration = Histogram(
            "lumi_agent_node_duration_seconds",
            "办公原子节点执行耗时",
            ["agent", "success"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 15, 30, 60, 180, 300),
        )
        _agent_channel_wait = Histogram(
            "lumi_agent_channel_wait_seconds",
            "办公四层路由通道限流等待时间",
            ["channel"],
            buckets=(0.001, 0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 300),
        )
        _celery_queue_ready = Gauge(
            "lumi_celery_queue_ready_tasks",
            "Celery Redis broker ready-task depth (does not include in-flight tasks)",
            ["queue"],
        )
        _document_pipeline_state = Gauge(
            "lumi_document_pipeline_documents",
            "Knowledge documents grouped by durable processing state",
            ["status"],
        )
        _document_pipeline_oldest_age = Gauge(
            "lumi_document_pipeline_oldest_age_seconds",
            "Age of the oldest queued or processing document",
            ["status"],
        )
        _read_view_cache = Counter(
            "lumi_read_view_cache_total",
            "User-scoped read-view cache events",
            ["endpoint", "result"],
        )
        _read_view_stage_duration = Histogram(
            "lumi_read_view_stage_duration_seconds",
            "High-traffic read-view stage duration",
            ["endpoint", "stage"],
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        # v2 统一任务画像/执行策略观测（性能指标第 4 节）。
        _policy_routes = Counter(
            "lumi_execution_policy_route_total",
            "v2 入口画像路由结果",
            ["execution_policy", "complexity"],
        )
        _policy_route_duration = Histogram(
            "lumi_policy_route_latency_seconds",
            "入口画像/策略判定耗时",
            ["execution_policy", "complexity"],
            buckets=(0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.25, 0.5, 1, 2),
        )
        _policy_first_delta = Histogram(
            "lumi_answer_first_delta_latency_seconds",
            "回答阶段开始到首个 delta 的耗时",
            ["execution_policy", "complexity"],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 60),
        )
        _policy_stream_duration = Histogram(
            "lumi_answer_stream_duration_seconds",
            "首个 delta 到 done 的答复流时长",
            ["execution_policy", "complexity"],
            buckets=(0.01, 0.05, 0.1, 0.5, 1, 2.5, 5, 15, 30, 60, 120, 300, 600),
        )
        _planner_invoked = Counter(
            "lumi_planner_invoked_total",
            "进入 Planner/复杂编排的次数",
            ["execution_policy", "complexity"],
        )
        _agent_invoked = Counter(
            "lumi_agent_invoked_total",
            "进入 Agent/节点执行的次数",
            ["execution_policy", "complexity"],
        )
        _workspace_read_duration = Histogram(
            "lumi_workspace_read_duration_seconds",
            "ATOMIC 只读快路径的工作区受控读取耗时",
            [],
            buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 40, 60),
        )
        # Job 运行视图快照写入（FLAG DOES NOT CONTROL SNAPSHOT WRITES：
        # 写入恒发生，这里只记录"哪一版写入口、写了多少次"）。
        _job_snapshot_writes = Counter(
            "lumi_job_snapshot_writes_total",
            "Job 运行视图快照写入次数（写入口版本维度）",
            ["status", "version"],
        )
        # ── 降级成本监控（方案 §5：在 Prometheus 里算"率"，不是只记"数"）──
        # ``is_fallback`` 把"正常模型"与"降级后的模型"分成两条序列，因此
        # ``sum(rate(lumi_llm_cost_usd_total[5m])) by (is_fallback)
        #   / sum(rate(lumi_agent_jobs_total[5m])) by (is_fallback)``
        # 就能直接回答"降级那条线是不是更贵"。
        #
        # **label 基数纪律**（评审 P0：防指标爆炸）：只允许低基数、可枚举的维度——
        # scene / provider / model / fallback_from / fallback_to / is_fallback / success /
        # result / direction。**禁止** user_id / job_id / conversation_id / 完整 prompt /
        # 任何自由文本：Counter 的每个 label 组合都会常驻一条时间序列，带 user_id 的
        # label 在 1 万用户时就是 1 万条序列，直接把 Prometheus 打爆。
        _llm_tokens = Counter(
            "lumi_llm_usage_tokens_total",
            "LLM token 消耗（场景/提供方/模型/降级关系/成功与否/方向）",
            _LLM_LABELS + ["success", "direction"],
        )
        _llm_cost = Counter(
            "lumi_llm_cost_usd_total",
            "LLM 估算成本 USD（场景/提供方/模型/降级关系/结果）",
            _LLM_LABELS + ["result"],
        )
        _llm_calls = Counter(
            "lumi_llm_calls_total",
            "LLM 调用次数（场景/提供方/模型/降级关系/成功与否/结果）",
            _LLM_LABELS + ["success", "result"],
        )
        _jobs_active = Gauge(
            "lumi_agent_jobs_active",
            "在途（非终态）任务数；与 lumi_agent_jobs_total 一起算单任务平均成本",
            ["status"],
        )
        # SCAN 是隐性成本：把"是否降级/是否命中上限"暴露出来，才能回答"这个 Gauge
        # 现在还可信吗"。没有它，降级成采样之后看板会静默失真。
        _jobs_active_scan = Gauge(
            "lumi_agent_jobs_active_scan_info",
            "在途任务扫描元信息（degraded=是否降级，capped=是否命中扫描上限）",
            ["degraded", "capped"],
        )
        # 工具链路：最终进模型的工具数量（按场景/层）。趋势下降 = 有工具在被截断/过滤。
        _tool_window_layers = Gauge(
            "lumi_tool_window_final_tools",
            "最终注入模型的工具数量（按场景与链路层）",
            ["scene", "layer"],
        )
        _prometheus = True
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prometheus 指标初始化失败: {}", exc)
        _prometheus = False
        return False


def inc_http_request(method: str, path: str, status: int, duration: float) -> None:
    if not _ensure_metrics():
        return
    route = path.split("?")[0][:120]
    _http_requests.labels(method=method, path=route, status=str(status)).inc()
    _http_duration.labels(method=method, path=route).observe(duration)


def inc_agent_job(status: str) -> None:
    if _ensure_metrics():
        _agent_jobs.labels(status=status).inc()


def inc_skill_call(skill: str, success: bool) -> None:
    if _ensure_metrics():
        _skill_calls.labels(skill=skill or "unknown", success=str(bool(success))).inc()


def inc_skill_routing_mode(scene: str, mode: str) -> None:
    if _ensure_metrics():
        _skill_routing_modes.labels(
            scene=(scene or "unknown")[:40],
            mode=(mode or "unknown")[:40],
        ).inc()


def inc_rag_search(hits: int) -> None:
    if _ensure_metrics():
        _rag_searches.labels(hits="hit" if hits else "miss").inc()


def inc_agent_route(level: str, mode: str, cache_hit: bool, duration: float | None = None) -> None:
    if _ensure_metrics():
        _agent_routes.labels(
            level=level or "unknown",
            mode=mode or "unknown",
            cache_hit=str(bool(cache_hit)).lower(),
        ).inc()
        if duration is not None:
            _agent_route_duration.labels(
                level=level or "unknown", mode=mode or "unknown"
            ).observe(max(0.0, duration))


def inc_agent_replan(from_level: str, to_level: str, reason: str) -> None:
    if _ensure_metrics():
        _agent_replans.labels(
            from_level=from_level or "unknown",
            to_level=to_level or "unknown",
            reason=reason or "unknown",
        ).inc()


def observe_agent_node_duration(agent: str, success: bool, duration: float) -> None:
    if _ensure_metrics():
        _agent_node_duration.labels(
            agent=(agent or "unknown")[:80], success=str(bool(success)).lower()
        ).observe(max(0.0, duration))


def observe_agent_channel_wait(channel: str, duration: float) -> None:
    if _ensure_metrics():
        _agent_channel_wait.labels(channel=(channel or "unknown")[:80]).observe(max(0.0, duration))


def inc_read_view_cache(endpoint: str, result: str) -> None:
    """Record cache hits, misses and fail-open Redis errors for read views."""
    if _ensure_metrics():
        _read_view_cache.labels(endpoint=(endpoint or "unknown")[:80], result=(result or "unknown")[:40]).inc()


def observe_read_view_stage(endpoint: str, stage: str, duration: float) -> None:
    """Expose pool checkout, SQL, cache and response-build latency separately."""
    if _ensure_metrics():
        _read_view_stage_duration.labels(
            endpoint=(endpoint or "unknown")[:80], stage=(stage or "unknown")[:40]
        ).observe(max(0.0, duration))


def observe_policy_route(execution_policy: str, complexity: str) -> None:
    if _ensure_metrics():
        _policy_routes.labels(
            execution_policy=(execution_policy or "unknown")[:60],
            complexity=(complexity or "unknown")[:24],
        ).inc()


def observe_policy_route_latency(execution_policy: str, complexity: str, seconds: float) -> None:
    if _ensure_metrics():
        _policy_route_duration.labels(
            execution_policy=(execution_policy or "unknown")[:60],
            complexity=(complexity or "unknown")[:24],
        ).observe(max(0.0, seconds))


def observe_answer_first_delta(execution_policy: str, complexity: str, seconds: float) -> None:
    if _ensure_metrics():
        _policy_first_delta.labels(
            execution_policy=(execution_policy or "unknown")[:60],
            complexity=(complexity or "unknown")[:24],
        ).observe(max(0.0, seconds))


def observe_answer_stream_duration(execution_policy: str, complexity: str, seconds: float) -> None:
    if _ensure_metrics():
        _policy_stream_duration.labels(
            execution_policy=(execution_policy or "unknown")[:60],
            complexity=(complexity or "unknown")[:24],
        ).observe(max(0.0, seconds))


def inc_planner_invoked(execution_policy: str = "", complexity: str = "") -> None:
    if _ensure_metrics():
        _planner_invoked.labels(
            execution_policy=(execution_policy or "unknown")[:60],
            complexity=(complexity or "unknown")[:24],
        ).inc()


def inc_agent_invoked(execution_policy: str = "", complexity: str = "") -> None:
    if _ensure_metrics():
        _agent_invoked.labels(
            execution_policy=(execution_policy or "unknown")[:60],
            complexity=(complexity or "unknown")[:24],
        ).inc()


def observe_workspace_read_duration(seconds: float) -> None:
    """ATOMIC 只读快路径：工作区受控读取耗时（workspace_read_duration_ms）。"""
    if _ensure_metrics():
        _workspace_read_duration.observe(max(0.0, seconds))


def inc_job_snapshot_write(*, status: str = "written", version: int = 0) -> None:
    """记录一次 Job 运行视图快照写入（TTL 见日志行；这里按写入口版本计数）。"""
    if _ensure_metrics():
        _job_snapshot_writes.labels(status=(status or "unknown")[:40], version=str(int(version))).inc()


def record_llm_usage_metrics(
    *,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    fallback_used: bool = False,
    fallback_from: str = "",
    fallback_to: str = "",
    success: bool = True,
    cost_usd: float = 0.0,
    scene: str = "",
    provider: str = "",
    result: str = "",
) -> None:
    """记录一次 LLM 调用的 token / 金额 / 次数（**降级成本监控**，方案 §5）。

    为什么不是只加一个"总 token 计数器"：要回答的是"降级之后的单任务成本有没有变贵"，
    那就必须把 ``is_fallback`` 作为 label——PromQL 里按它分组算 rate 才能对比两条线：

        sum(rate(lumi_llm_cost_usd_total[5m])) by (is_fallback)
        / sum(rate(lumi_agent_jobs_total[5m])) by (is_fallback)

    ``fallback_from`` / ``fallback_to`` 额外回答"从哪个模型降到了哪个"，否则只知道
    "降级更贵"却不知道换回去该换哪个。

    **label 只取低基数维度**（scene/provider/model/fallback_*/is_fallback/success/result）。
    传进来的自由文本一律按"未知"处理，绝不把 ``user_id`` 这类高基数标识写进 label。
    指标失败绝不影响主流程（调用方已用 try 包住）。
    """
    if not _ensure_metrics():
        return
    # 每个维度都只接受**短且可枚举**的取值：截断 + 兜底，防止调用方顺手把 job_id 传进来。
    scene_label = _low_cardinality(scene, limit=24)
    provider_label = _low_cardinality(provider, limit=32)
    name = _low_cardinality(model, limit=64)
    origin = _low_cardinality(fallback_from, limit=64)
    target = _low_cardinality(fallback_to, limit=64)
    flag = "true" if fallback_used else "false"
    ok = "true" if success else "false"
    outcome = _low_cardinality(result, limit=24) if result else ok
    labels = {
        "scene": scene_label,
        "provider": provider_label,
        "model": name,
        "fallback_from": origin,
        "fallback_to": target,
        "is_fallback": flag,
    }
    try:
        if prompt_tokens:
            _llm_tokens.labels(**labels, success=ok, direction="prompt").inc(int(prompt_tokens))
        if completion_tokens:
            _llm_tokens.labels(**labels, success=ok, direction="completion").inc(int(completion_tokens))
        _llm_calls.labels(**labels, success=ok, result=outcome).inc()
        if cost_usd:
            _llm_cost.labels(**labels, result=outcome).inc(float(cost_usd))
    except Exception:  # noqa: BLE001 - 指标绝不影响主流程
        pass


def _low_cardinality(value: str, *, limit: int) -> str:
    """把任意字符串压成**有界**的 label 取值。

    * 空值 → ``unknown``（Prometheus 不接受空 label 值的语义歧义）；
    * 超长/含空格等自由文本特征 → 截断（避免有人把 prompt 片段当维度传进来）。
    """
    text = str(value or "").strip()
    if not text:
        return "unknown"
    return text[:limit]


def record_tool_window_snapshot(
    *,
    scene: str,
    catalog: Sequence[Any] = (),
    eligible: Sequence[Any] = (),
    ranked: Sequence[Any] = (),
    final: Sequence[Any] = (),
    layer: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    """记录一次**工具链路四层快照**（方案 §五 P0-2）。

    层次：``catalog``（系统知道）→ ``eligible``（允许用）→ ``ranked``（排序候选）→
    ``final``（真正传给模型）。四层都记 + 自动算逐层差集，于是"少了哪个工具、在哪一层
    少的"不用再靠猜。

    **只记名字与三态可见性**，绝不记参数、schema、正文或用户输入——所以可以安全落日志。
    返回快照字典，供调用方塞进过程事件或审计。
    """
    try:
        from app.agents.skills.mandatory_tools import build_snapshot

        snapshot = build_snapshot(
            scene=scene,
            limit=len(list(final or ())),
            catalog=catalog,
            eligible=eligible,
            ranked=ranked,
            final=final,
        )
        payload = snapshot.as_dict()
    except Exception as exc:  # noqa: BLE001 - 诊断失败绝不影响工具注入
        logger.debug("[tool-window] 快照构造失败: {}", str(exc)[:120])
        return {}
    logger.info(
        "[tool-window] layer={} scene={} layers={} dropped={} visibility={}",
        layer or "-",
        scene or "-",
        payload.get("counts", {}),
        payload.get("dropped_by_layer", {}),
        payload.get("visibility", {}),
    )
    try:
        if _ensure_metrics():
            _tool_window_layers.labels(scene=(scene or "unknown")[:24], layer=(layer or "unknown")[:40]).set(
                len(payload.get("final") or [])
            )
    except Exception:  # noqa: BLE001
        pass
    # 按任务落盘（可选）：前端排障面板据此回答"这次模型到底拿到了哪些工具、
    # 少了哪个、在哪一层少的"。只在**已在事件循环内**时顺手排队写入——
    # 这里不能自己造 loop（诊断代码没有资格决定并发模型），写失败一律静默。
    if job_id:
        try:
            import asyncio

            from app.services.resume_snapshot import record_tool_window

            loop = asyncio.get_running_loop()
            payload_with_job = {**payload, "job_id": str(job_id), "layer": layer or ""}
            loop.create_task(record_tool_window(str(job_id), payload_with_job))
        except Exception:  # noqa: BLE001 - 无事件循环/写失败都不影响工具注入
            pass
    return payload


def set_active_jobs_by_status(counts: dict[str, int]) -> None:
    """把"在途任务数（按状态）"写进 Gauge。

    单任务平均成本 = 成本速率 ÷ 任务速率，但"此刻有多少任务在跑"只能靠 Gauge 表达；
    没有它，Grafana 上无法区分"成本涨了"与"并发涨了"。
    """
    if not _ensure_metrics():
        return
    try:
        for status, value in (counts or {}).items():
            _jobs_active.labels(status=(status or "unknown")[:40]).set(int(value))
    except Exception:  # noqa: BLE001
        pass


async def refresh_async_dispatch_metrics() -> None:
    """Refresh cross-process Celery/document gauges just before /metrics.

    Redis LLEN represents only ready messages.  The document-state gauges and
    oldest-age gauges make in-flight and worker-lost work visible alongside it.
    Failures are intentionally isolated: observability must never break the
    scrape endpoint or the API request path.
    """
    if not _ensure_metrics():
        return
    try:
        import redis.asyncio as aioredis
        from sqlalchemy import func, select

        from app.core.database import async_session_factory
        from app.models.db_models import Document

        broker = aioredis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)
        try:
            for queue in ("durable", "best_effort", "maintenance"):
                _celery_queue_ready.labels(queue=queue).set(await broker.llen(queue))
        finally:
            await broker.aclose()

        now = time.time()
        async with async_session_factory() as session:
            for status in ("pending", "processing", "ready", "error"):
                count = (
                    await session.execute(
                        select(func.count()).select_from(Document).where(Document.status == status)
                    )
                ).scalar_one()
                _document_pipeline_state.labels(status=status).set(count)

            for status, time_column in (
                ("pending", Document.queued_at),
                ("processing", Document.processing_started_at),
            ):
                oldest = (
                    await session.execute(
                        select(func.min(time_column)).where(Document.status == status)
                    )
                ).scalar_one()
                age = 0.0 if oldest is None else max(0.0, now - oldest.timestamp())
                _document_pipeline_oldest_age.labels(status=status).set(age)
    except Exception as exc:  # noqa: BLE001
        logger.debug("异步任务指标刷新失败: {}", exc)
    await refresh_active_job_gauge()


#: 在途任务数 Gauge 的数据源：任务终态计数键 ``obs:job:{job_id}`` 里存的**当前状态**。
#: 复用这个已有键空间（原本只用于"每个任务只计一次"的去重标记），因此不需要新表、
#: 也不需要额外的 Worker 心跳——代价是一次 SCAN，只在抓取 /metrics 时发生。
_JOB_MARKER_PREFIX = "obs:job:"


async def mark_job_state(job_id: str, status: str) -> None:
    """把任务的**当前状态**写进标记键（在途任务 Gauge 的唯一数据源）。

    任务从创建/开始执行起就写，而不是等到终态：否则"刚提交还在跑"的任务在 Gauge 上
    完全不可见，而恰恰是它们占用着并发与成本。失败静默（标记只是观测）。
    """
    target = str(job_id or "")
    if not target:
        return
    try:
        from app.core.redis import get_redis

        await get_redis().set(f"{_JOB_MARKER_PREFIX}{target}", str(status or ""), ex=86400 * 7)
    except Exception as exc:  # noqa: BLE001
        logger.debug("任务状态标记写入失败 {}: {}", target[:12], exc)


async def refresh_active_job_gauge() -> dict[str, int]:
    """把"在途任务数（按状态）"写进 Gauge；返回统计结果（便于测试断言）。

    "在途" = 任务标记里的状态不是终态（completed / failed / cancelled）。
    终态任务不参与并发计数，否则 Gauge 会变成"7 天内跑过多少任务"。
    """
    counts: dict[str, int] = {}
    degraded = False
    capped = False
    try:
        from app.core.redis import get_redis

        redis = get_redis()
        scanned = 0
        deadline = time.monotonic() + _ACTIVE_JOB_SCAN_BUDGET_SECONDS
        # 三重护栏（评审 P0：SCAN 不能成为隐性故障源）：
        #   ① 标记键带 TTL（mark_job_state 里 7 天）→ 不会无限增长；
        #   ② 单次扫描条数上限 → 任务多时不会拖慢 /metrics；
        #   ③ 时间预算 → 慢 Redis 上也不会把抓取请求挂住。
        # 触到上限/超时不是错误：本轮**不更新**（保留上一次的值）并置 degraded=1，
        # 让看板能看出"这个数字现在是采样/过期的"，而不是静默失真。
        async for key in redis.scan_iter(match=f"{_JOB_MARKER_PREFIX}*", count=500):
            if scanned >= _ACTIVE_JOB_SCAN_LIMIT:
                capped = True
                degraded = True
                break
            if time.monotonic() >= deadline:
                degraded = True
                break
            scanned += 1
            try:
                status = str(await redis.get(key) or "").strip().lower()
            except Exception:  # noqa: BLE001 - 单个键读失败不影响整体
                continue
            if not status:
                continue
            counts[status] = counts.get(status, 0) + 1
        if not degraded:
            in_flight = {
                status: value
                for status, value in counts.items()
                if status not in {"completed", "failed", "cancelled"}
            }
            set_active_jobs_by_status(in_flight or {"idle": 0})
        _set_active_job_scan_info(degraded=degraded, capped=capped)
        return counts
    except Exception as exc:  # noqa: BLE001 - 指标刷新失败不影响 /metrics 其余部分
        logger.debug("在途任务 Gauge 刷新失败: {}", exc)
        _set_active_job_scan_info(degraded=True, capped=False)
        return counts


def _set_active_job_scan_info(*, degraded: bool, capped: bool) -> None:
    """记录本轮扫描是否降级/命中上限（Gauge 值本身是否可信，一目了然）。"""
    if not _ensure_metrics():
        return
    try:
        _jobs_active_scan.labels(
            degraded="true" if degraded else "false",
            capped="true" if capped else "false",
        ).set(1)
    except Exception:  # noqa: BLE001
        pass


def metrics_text() -> str:
    """生成 Prometheus 文本格式指标（/metrics 响应体）."""
    if not _ensure_metrics():
        return "# metrics disabled\n"
    from prometheus_client import generate_latest

    return generate_latest().decode("utf-8")


async def metrics_middleware(request, call_next: Callable):
    """FastAPI 中间件：记录请求量/耗时/状态."""
    method = request.method
    path = request.url.path
    start = time.perf_counter()
    try:
        response = await call_next(request)
        inc_http_request(method, path, response.status_code, time.perf_counter() - start)
        return response
    except Exception:
        inc_http_request(method, path, 500, time.perf_counter() - start)
        raise
