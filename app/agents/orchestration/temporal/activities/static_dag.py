"""_install_node_deadline _json_safe _execute_node_activity_inner execute_node_activity（activities 的 static_dag 族）。"""

import asyncio
import hashlib
import json
from temporalio import activity
import temporalio.exceptions
from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.execution.review import get_reviewer
from app.agents.orchestration.execution.workers import WORKERS, WorkerContext
from app.agents.orchestration.temporal.client import load_job_llm_config


def _install_node_deadline(timeout: float):
    """给一次节点 Activity 装上任务级预算（返回还原凭证；失败一律不影响执行）。

    ``timeout`` 与 Temporal 的 ``start_to_close_timeout`` 同源（节点超时），因此
    "内部调用按预算收尾"会**早于**"Temporal 硬掐 Activity"，两者不会互相打架。
    """
    try:
        from app.platform.runtime.deadline import install_job_deadline

        return install_job_deadline(budget=float(timeout), source="job.temporal_activity")
    except Exception:  # noqa: BLE001 - 预算装配失败不能阻止节点执行
        return None


def _json_safe(obj):
    """保证 Activity 返回值可被 Temporal JSON 数据转换器序列化."""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return {
            "status": "failed",
            "result": None,
            "error": "活动结果序列化失败",
            "error_code": "EXEC_ERROR",
            "retries": 0,
        }


async def _execute_node_activity_inner(payload: dict) -> dict:
    """执行单个任务节点：定位 worker → 执行 → 质检 → React 重试."""
    node_data = payload.get("node") or {}
    job_id = str(payload.get("job_id") or "")
    user_id = str(payload.get("user_id") or "")
    scene = str(payload.get("scene") or "office")
    user_role = str(payload.get("user_role") or "user")
    user_request = str(payload.get("user_request") or "")
    cfg = payload.get("config") or {}
    node = TaskNode.model_validate(node_data)
    from app.agents.orchestration.policy.runtime import node_timeout_seconds

    timeout = node.execution.timeout_seconds or node_timeout_seconds(node, int(cfg.get("node_timeout_seconds") or 300))
    policy_attempts = node.execution.retry.max_attempts
    max_retries = max(0, int(policy_attempts) - 1) if policy_attempts is not None else int(cfg.get("node_max_retries") or 2)

    worker = WORKERS.get(node.agent)
    if worker is None:
        return {
            "status": "failed",
            "result": None,
            "error": f"未注册的执行 agent: {node.agent}",
            "error_code": "AGENT_NOT_FOUND",
            "retries": 0,
        }

    node.metadata = dict(node.metadata or {})
    node.metadata.setdefault("tool_index", 0)
    from app.agents.orchestration.execution.context import sanitize_dependency_result

    node.metadata["dependency_results"] = {
        str(dep_id): sanitize_dependency_result(value)
        for dep_id, value in (payload.get("dependency_results") or {}).items()
    }
    # Continued long DAGs carry only completed-node result references in the
    # Workflow input. Resolve them here, inside the Activity boundary, after
    # verifying their owner-scoped hash; result bodies never return to history.
    dependency_refs = (node.metadata or {}).pop("temporal_dependency_refs", {})
    if isinstance(dependency_refs, dict):
        from app.agents.orchestration.execution.lineage import resolve_result_ref

        for dep_id, ref in dependency_refs.items():
            if str(dep_id) in node.metadata["dependency_results"]:
                continue
            resolved = await resolve_result_ref(user_id, ref if isinstance(ref, dict) else None)
            if resolved:
                node.metadata["dependency_results"][str(dep_id)] = sanitize_dependency_result(resolved)
            else:
                node.metadata["dependency_results"][str(dep_id)] = {
                    "summary": "[前序结果引用不可用，需重新执行该前序步骤]",
                    "error_code": "RESULT_REF_EXPIRED",
                }
    dependency_bodies = node.metadata["dependency_results"]
    if dependency_bodies:
        raw = json.dumps(dependency_bodies, ensure_ascii=False, sort_keys=True, default=str)
        node.metadata["approval_upstream_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    review = get_reviewer()
    llm_config = await load_job_llm_config(job_id) if job_id else None
    llm_api_key = (llm_config or {}).get("api_key")

    # Temporal Activities run in a separate execution boundary from the API
    # SSE coroutine.  Publish text deltas to the same short-lived Redis stream
    # used by the legacy DAG path so office writing remains truly streaming in
    # both runtimes.
    async def on_output(text: str) -> None:
        from app.office.api import push_delta

        await push_delta(job_id, node.id, text)

    ctx = WorkerContext(
        user_id=user_id,
        job_id=job_id,
        scene=scene,
        user_role=user_role,
        llm_api_key=llm_api_key,
        llm_config=llm_config,
        user_request=user_request,
        confirmed_tools=frozenset(
            str(value) for value in ((node.metadata or {}).get("confirmed_tools") or [])
        ),
        confirmed_tool_calls=frozenset(
            str(value) for value in ((node.metadata or {}).get("confirmed_tool_calls") or [])
        ),
        approval_context_sha256=str((node.metadata or {}).get("approval_upstream_sha256") or ""),
        office_doc_ids=tuple(
            str(value) for value in (payload.get("office_doc_ids") or []) if str(value).strip()
        ),
        authorized_project_ids=tuple(
            str(value) for value in (payload.get("authorized_project_ids") or []) if str(value).strip()
        ),
        workspace_id=str(payload.get("workspace_id") or "").strip(),
        on_output=on_output,
    )

    heartbeat_seconds = max(5, int(cfg.get("activity_heartbeat_seconds") or 15))

    async def keep_alive() -> None:
        """让长检索/模型调用在 Worker 重启检测窗口内持续上报。"""
        while True:
            await asyncio.sleep(heartbeat_seconds)
            try:
                activity.heartbeat({"node_id": node.id, "agent": node.agent})
            except RuntimeError:
                # 单元测试会直接调用 Activity 函数，此时不存在 Temporal 的
                # Activity 上下文；执行结果不应因此被后台心跳影响。
                return

    heartbeat_task = asyncio.create_task(keep_alive())

    from app.agents.orchestration.execution.node_runtime import NodeExecutionRunner
    from app.agents.orchestration.execution.safety import is_effectful
    from app.agents.orchestration.execution.telemetry import LumiExecutionTelemetry

    try:
        outcome = await NodeExecutionRunner(
            worker=worker,
            node=node,
            ctx=ctx,
            review=review,
            timeout_seconds=timeout,
            max_retries=0 if is_effectful(node) else max_retries,
            effectful=is_effectful(node),
            telemetry=LumiExecutionTelemetry(),
        ).run()
    except (asyncio.CancelledError, temporalio.exceptions.CancelledError):
        return {
            "status": "interrupted",
            "result": None,
            "error": "任务被用户终止",
            "error_code": "INTERRUPTED",
            "retries": node.retries,
        }
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)

    if not outcome.success:
        return {
            "status": "escalated" if outcome.escalation else "failed",
            "result": None,
            "error": outcome.error,
            "error_code": outcome.error_code,
            "retries": outcome.retries,
            "recovery": outcome.recovery,
            "escalation": outcome.escalation,
        }
    # 自动沉淀任务记忆（后续节点/汇总可回顾）
    try:
        from app.agents.memory.task_memory import remember

        content = (outcome.result or {}).get("content") or (outcome.result or {}).get("output") or ""
        await remember(job_id, f"节点:{node.agent}", f"{node.name or node.agent}：{str(content)[:300]}")
    except Exception:  # noqa: BLE001
        pass
    from app.agents.orchestration.execution.presentation import attach_display_result

    return _json_safe(
        {
            "status": "completed",
            "result": attach_display_result(node, outcome.result or {}),
            "retries": outcome.retries,
        }
    )


@activity.defn
async def execute_node_activity(payload: dict) -> dict:
    """带资源互斥和副作用幂等保护的节点 Activity。"""
    from app.agents.orchestration.runtime.effects import (
        EffectJournalUnavailable,
        effect_guard,
        effect_intent_for_node,
    )
    from app.agents.orchestration.policy.runtime import node_timeout_seconds
    from app.agents.resource_coordination import (
        WriteResourceCoordinationUnavailable,
        resource_coordinator,
    )
    from app.agents.orchestration.execution.safety import is_effectful, prepare_node_safety

    node = TaskNode.model_validate(payload.get("node") or {})
    job_id = str(payload.get("job_id") or "")
    user_id = str(payload.get("user_id") or "")
    cfg = payload.get("config") or {}
    prepare_node_safety(node, user_id, job_id)
    payload = {**payload, "node": node.model_dump()}
    effectful = is_effectful(node)
    if effectful:
        # 副作用工具只执行一次；崩溃后的 Temporal 级重试由 effect journal 拦截。
        cfg = {**cfg, "node_max_retries": 0}
        payload = {**payload, "config": cfg}

    # Fail closed for writes before an effect reservation is recorded. The
    # workflow reschedules this non-terminal state after a bounded backoff;
    # read-only claims retain their local fail-open behavior.
    if not await resource_coordinator.write_coordination_available(node.resource_claims):
        return {
            "status": "waiting_resources",
            "result": None,
            "error": "写资源协调服务暂不可用，任务将自动等待恢复",
            "error_code": "RESOURCE_COORDINATION_UNAVAILABLE",
            "retries": 0,
        }

    if effectful and node.idempotency_key:
        try:
            existing = await effect_guard.reserve(
                node.idempotency_key,
                effect_intent_for_node(job_id=job_id, node=node),
            )
        except EffectJournalUnavailable:
            return {
                "status": "failed",
                "result": None,
                "error": "副作用安全日志不可用，已阻止执行以避免重复操作",
                "error_code": "EFFECT_JOURNAL_UNAVAILABLE",
                "retries": 0,
                "effect_status": "pending",
            }
        except RuntimeError:
            return {
                "status": "failed",
                "result": None,
                "error": "副作用步骤已开始但结果不确定，已停止自动重试以避免重复执行",
                "error_code": "EFFECT_UNCERTAIN",
                "retries": 0,
                "effect_status": "uncertain",
            }
        if existing is not None:
            return {
                "status": "completed",
                "result": (existing or {}).get("result"),
                "retries": 0,
                "effect_status": "committed",
            }

    timeout = node_timeout_seconds(node, int(cfg.get("node_timeout_seconds") or 300))
    # 任务级预算，与 Temporal 的 `start_to_close_timeout` **同源**（都来自节点超时）：
    # Activity 被硬超时掐死之前，内部的 LLM/MCP 调用先一步按预算收尾——否则"被 Temporal
    # 掐死"会留下半个节点与不一致的副作用状态。装在并发闸门之外：等锁的时间不算预算。
    deadline_token = _install_node_deadline(timeout)
    try:
        from app.agents.orchestration.admission.channel_limits import channel_limiter

        channel = "node_execution"
        if node.agent in {"direct_llm", "atomic_step", "react_step", "collect_results", "office_text", "office_research"}:
            channel = "llm_provider"
        async with channel_limiter.claim(channel, lease_seconds=max(60, timeout + 60)):
            async with resource_coordinator.claim(node.resource_claims, ttl=max(60, timeout + 60)):
                out = await _execute_node_activity_inner(payload)
    except WriteResourceCoordinationUnavailable:
        # This race happens while acquiring the lease, before the tool body.
        # Drop the fresh intent instead of falsely treating it as uncertain.
        if effectful and node.idempotency_key:
            try:
                await effect_guard.abandon_pending(node.idempotency_key)
            except (EffectJournalUnavailable, RuntimeError):
                return {
                    "status": "failed",
                    "result": None,
                    "error": "副作用日志无法清理，已停止自动重试",
                    "error_code": "EFFECT_JOURNAL_UNAVAILABLE",
                    "retries": 0,
                    "effect_status": "uncertain",
                }
        return {
            "status": "waiting_resources",
            "result": None,
            "error": "写资源协调服务暂不可用，任务将自动等待恢复",
            "error_code": "RESOURCE_COORDINATION_UNAVAILABLE",
            "retries": 0,
        }
    except BaseException:
        if effectful and node.idempotency_key:
            try:
                await effect_guard.mark_uncertain(node.idempotency_key, "activity_interrupted")
            except (EffectJournalUnavailable, RuntimeError):
                pass
        raise
    finally:
        if deadline_token is not None:
            deadline_token.reset()

    if effectful and node.idempotency_key:
        if out.get("status") == "completed":
            try:
                await effect_guard.confirm(node.idempotency_key, out.get("result"))
                out["effect_status"] = "committed"
            except EffectJournalUnavailable:
                return {
                    "status": "failed",
                    "result": None,
                    "error": "副作用已执行但安全日志确认失败，已停止自动重试",
                    "error_code": "EFFECT_JOURNAL_UNAVAILABLE",
                    "retries": 0,
                    "effect_status": "uncertain",
                }
        else:
            try:
                await effect_guard.mark_uncertain(
                    node.idempotency_key,
                    str(out.get("error_code") or "execution_failed"),
                )
            except (EffectJournalUnavailable, RuntimeError):
                out["error_code"] = "EFFECT_JOURNAL_UNAVAILABLE"
                out["error"] = "副作用执行状态无法写入安全日志，已停止自动重试"
            out["effect_status"] = "uncertain"
    return out
