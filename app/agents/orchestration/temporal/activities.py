"""Temporal Activities —— 节点执行与任务清理.

Activity 是 Temporal 中做副作用的地方：LLM 调用、技能执行、DB/Redis 读写
都在这里发生，Workflow 保持确定性。节点执行语义与 legacy dag.py 一致：
worker.execute → 质检 → React 重试（最多 max_retries 次）。
"""

import asyncio
import json

from loguru import logger
from temporalio import activity
import temporalio.exceptions

from app.agents.orchestration.models import TaskNode, TaskStatus
from app.agents.orchestration.review import get_reviewer
from app.agents.orchestration.workers import WORKERS, WorkerContext
from app.agents.orchestration.temporal.client import load_byok_key


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
    cfg = payload.get("config") or {}
    timeout = int(cfg.get("node_timeout_seconds") or 300)
    max_retries = int(cfg.get("node_max_retries") or 2)

    worker = WORKERS.get(node_data.get("agent"))
    if worker is None:
        return {
            "status": "failed",
            "result": None,
            "error": f"未注册的执行 agent: {node_data.get('agent')}",
            "error_code": "AGENT_NOT_FOUND",
            "retries": 0,
        }

    node = TaskNode.model_validate(node_data)
    node.metadata = dict(node.metadata or {})
    from app.agents.orchestration.context import sanitize_dependency_result

    node.metadata["dependency_results"] = {
        str(dep_id): sanitize_dependency_result(value)
        for dep_id, value in (payload.get("dependency_results") or {}).items()
    }
    review = get_reviewer()
    llm_api_key = await load_byok_key(job_id) if job_id else None
    ctx = WorkerContext(
        user_id=user_id,
        job_id=job_id,
        scene=scene,
        user_role=user_role,
        llm_api_key=llm_api_key,
    )

    for attempt in range(max_retries + 1):
        if attempt:
            node.retries = attempt
            node.status = TaskStatus.RETRYING
        node.status = TaskStatus.RUNNING
        node.error = None
        node.error_code = None
        error = None
        error_code = None
        result = None
        try:
            result = await asyncio.wait_for(worker.execute(node, ctx), timeout=timeout)
            if isinstance(result, dict) and result.get("success") is False:
                error = str(result.get("error") or "执行失败")
                error_code = str(result.get("error_code") or "EXEC_ERROR")
        except asyncio.CancelledError:
            return {
                "status": "interrupted",
                "result": None,
                "error": "任务被用户终止",
                "error_code": "INTERRUPTED",
                "retries": attempt,
            }
        except temporalio.exceptions.CancelledError:
            return {
                "status": "interrupted",
                "result": None,
                "error": "任务被用户终止",
                "error_code": "INTERRUPTED",
                "retries": attempt,
            }
        except asyncio.TimeoutError:
            error = f"执行超时（>{timeout}s）"
            error_code = "TIMEOUT"
        except Exception as exc:  # noqa: BLE001
            logger.warning("节点 {} 执行异常: {}", node.id, exc)
            error = str(exc)
            error_code = "EXEC_ERROR"

        if error:
            if attempt < max_retries:
                continue
            return {
                "status": "failed",
                "result": None,
                "error": error,
                "error_code": error_code,
                "retries": attempt,
            }

        # 质检（不通过则重做，最多 max_retries 次）
        verdict = await review.review(node, result, ctx)
        if verdict.approved:
            # 自动沉淀任务记忆（后续节点/汇总可回顾）
            try:
                from app.agents.memory.task_memory import remember

                content = (result or {}).get("content") or (result or {}).get("output") or ""
                await remember(
                    job_id,
                    f"节点:{node.agent}",
                    f"{node.name or node.agent}：{str(content)[:300]}",
                )
            except Exception:  # noqa: BLE001
                pass
            return _json_safe({"status": "completed", "result": result, "retries": attempt})
        if attempt < max_retries:
            continue
        return {
            "status": "failed",
            "result": None,
            "error": f"质检未通过: {verdict.feedback}",
            "error_code": "REVIEW_REJECTED",
            "retries": attempt,
        }


@activity.defn
async def execute_node_activity(payload: dict) -> dict:
    """带资源互斥和副作用幂等保护的节点 Activity。"""
    from app.agents.orchestration.effects import begin_effect, finish_effect
    from app.agents.orchestration.resources import resource_coordinator
    from app.agents.orchestration.safety import is_effectful, prepare_node_safety

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

    if effectful and node.idempotency_key:
        created, existing = await begin_effect(node.idempotency_key)
        if not created:
            if str((existing or {}).get("status")) == "committed":
                return {
                    "status": "completed",
                    "result": (existing or {}).get("result"),
                    "retries": 0,
                    "effect_status": "committed",
                }
            return {
                "status": "failed",
                "result": None,
                "error": "副作用步骤已开始但结果不确定，已停止自动重试以避免重复执行",
                "error_code": "EFFECT_UNCERTAIN",
                "retries": 0,
                "effect_status": "uncertain",
            }

    timeout = int(cfg.get("node_timeout_seconds") or 300)
    try:
        async with resource_coordinator.claim(node.resource_claims, ttl=max(60, timeout + 60)):
            out = await _execute_node_activity_inner(payload)
    except BaseException:
        if effectful and node.idempotency_key:
            await finish_effect(node.idempotency_key, "uncertain")
        raise

    if effectful and node.idempotency_key:
        if out.get("status") == "completed":
            await finish_effect(node.idempotency_key, "committed", out.get("result"))
            out["effect_status"] = "committed"
        else:
            await finish_effect(node.idempotency_key, "uncertain")
            out["effect_status"] = "uncertain"
    return out


@activity.defn
async def synthesize_final_answer_activity(payload: dict) -> dict:
    """任务收尾：把用户请求 + 各节点产出合成为最终交付答案（纯干活不交付的问题）."""
    user_id = str(payload.get("user_id") or "")
    job_id = str(payload.get("job_id") or "")
    request = str(payload.get("request") or "")
    nodes = payload.get("nodes") or []
    # 保存成功案例（Few-Shot 规划参考；失败静默）
    try:
        from app.agents.orchestration.cases import save_success_case

        await save_success_case(user_id, request, nodes)
    except Exception:  # noqa: BLE001
        pass
    if not nodes:
        return {"final_answer": ""}
    blocks = "\n\n".join(
        f"【{n.get('title') or n.get('agent')}】\n{str(n.get('content') or '')}"
        for n in nodes[:8]
    )
    # 任务记忆：把任务过程中的关键决策/已读文件一并交给汇总
    try:
        from app.agents.memory.task_memory import format_memory, recall

        mem_text = format_memory(await recall(job_id))
        if mem_text:
            blocks += f"\n\n【任务记忆】\n{mem_text}"
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core.llm import LLMClient
        from app.services.usage import CATEGORY_SKILL

        llm = LLMClient()
        reply = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "你是办公助手。根据用户请求和下面各步骤的结果，直接输出最终交付内容"
                        "（如总结、邮件正文、分析结论、待办清单等）。"
                        "不要提及'步骤/agent'，不要重复过程，直接给出对用户有用的最终答案；"
                        "如果用户请求无法从结果中得到答案，如实说明。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"用户请求：{request}\n\n各步骤结果：\n{blocks[:60000]}",
                },
            ],
            scene="office",
            max_tokens=6000,
            temperature=0.3,
            usage_user_id=user_id or None,
            usage_category=CATEGORY_SKILL,
            disable_reasoning_effort=True,
        )
        return {"final_answer": (reply or "").strip()}
    except Exception:  # noqa: BLE001
        return {"final_answer": ""}


@activity.defn
async def cleanup_job_secrets_activity(job_id: str) -> None:
    """任务正常结束时删除 BYOK 临时 key（取消/中断路径由 TTL 兜底清理）."""
    if job_id:
        from app.agents.orchestration.temporal.client import delete_byok_key

        await delete_byok_key(job_id)
