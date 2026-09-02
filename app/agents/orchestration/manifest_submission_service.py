"""为任务提交准备已显式授权的任务清单。

这是 ``ManifestContinuationService`` 在提交时的对应组件。它负责授权、安全加载
来源、确定性解析，以及对用户已授权清单的可选模型补充；绝不创建 Job 或向执行
后端提交工作。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.agents.orchestration.task_manifest import (
    authorize_manifest_source,
    extract_natural_language_manifest,
    has_unsafe_manifest_instruction,
    manifest_progress,
    materialize_manifest_batch,
    new_manifest,
    parse_task_manifest,
    reconcile_structured_manifest,
)
from app.core.config import settings


@dataclass(slots=True)
class ManifestSubmissionResult:
    """One explicit checklist outcome, or ``None`` when not a manifest."""

    tree: Any
    routing: dict[str, Any]


class ManifestSubmissionService:
    """Turn an authorized user checklist into a bounded first execution batch."""

    def __init__(self, workers: dict | None = None) -> None:
        self._workers = workers or {}

    async def prepare(
        self,
        *,
        user_id: str,
        request: str,
        office_docs: list[dict] | None,
        llm_api_key: str | None,
        llm_config: dict | None,
        routing_model: dict,
    ) -> ManifestSubmissionResult | None:
        # A declared A/B parallel -> C -> D read-only workflow is a DAG
        # contract, not a checklist. Let the dedicated compiler preserve its
        # dependencies before the generic manifest parser attempts model-based
        # item extraction.
        from app.agents.orchestration.planning.read_only_dag import (
            build_explicit_read_only_dag,
        )

        if build_explicit_read_only_dag(request) is not None:
            return None
        authorization = authorize_manifest_source(request, office_docs)
        if authorization is None:
            return None

        from app.agents.orchestration.planner import TaskTree

        manifest_source: dict[str, str] = {}
        if authorization.clarification:
            return ManifestSubmissionResult(
                tree=TaskTree(nodes=[], clarification=authorization.clarification),
                routing={
                    "llm": routing_model,
                    "level": "manifest",
                    "mode": "manifest_clarification",
                    "cache_hit": False,
                    "plan_revision": 1,
                    "manifest_source": manifest_source,
                },
            )

        source_text = request
        source_label = "用户消息"
        clarification = ""
        if authorization.source == "office_document":
            selected = authorization.document or {}
            selected_doc_id = str(selected.get("doc_id") or "")
            try:
                from app.core.executors import run_in_compute
                from app.services.office_docs import ensure_session, extract_full_text

                meta = await ensure_session(user_id, selected_doc_id)
                expected_name = str(selected.get("filename") or "")
                actual_name = str(meta.get("filename") or "")
                if expected_name and actual_name and expected_name.casefold() != actual_name.casefold():
                    raise ValueError("附件名称与服务端会话记录不一致")
                source_text = await run_in_compute(
                    extract_full_text, user_id, selected_doc_id
                )
                source_label = f"用户指定附件《{actual_name}》"
                manifest_source = {
                    "type": "office_document",
                    "doc_id": selected_doc_id,
                    "filename": actual_name,
                }
            except (LookupError, ValueError) as exc:
                clarification = (
                    f"无法读取指定的清单附件：{exc}。请重新上传或确认文件名后再试。"
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("读取已授权清单附件失败: {}", exc)
                clarification = "指定的清单附件暂时无法读取，请稍后重试或将清单粘贴到消息中。"
        else:
            manifest_source = {"type": "user_message"}

        if clarification:
            return self._clarification(clarification, routing_model, manifest_source)
        if has_unsafe_manifest_instruction([source_text]):
            return self._clarification(
                "清单中包含试图改变系统规则、访问敏感数据或越权资源的内容，"
                "因此未启动执行。请移除该内容后重新提交合法任务。",
                routing_model,
                manifest_source,
            )

        parsed_items = parse_task_manifest(source_text)
        if parsed_items:
            # Numbered/bulleted manifests are already an explicit, authorized
            # control-plane input.  Do not spend an LLM call rephrasing them:
            # the deterministic parser remains authoritative and preserves
            # every item and its written order.
            structured_items = []
        else:
            try:
                structured_items = await extract_natural_language_manifest(
                    source_text,
                    user_id=user_id,
                    api_key=llm_api_key,
                    llm_config=llm_config,
                    source_label=source_label,
                )
            except Exception as exc:  # noqa: BLE001
                from app.agents.skills.recovery import (
                    classify_model_error,
                    is_terminal_model_error_code,
                )

                error_code, user_error = classify_model_error(exc)
                if is_terminal_model_error_code(error_code) and error_code != "MODEL_UNAVAILABLE":
                    logger.warning("清单模型不可用，办公任务停止: {}", user_error)
                    return ManifestSubmissionResult(
                        tree=TaskTree(nodes=[], error=user_error, error_code=error_code),
                        routing={
                            "llm": routing_model,
                            "level": "manifest",
                            "mode": "manifest_model_error",
                            "cache_hit": False,
                            "plan_revision": 1,
                            "manifest_source": manifest_source,
                        },
                    )
                logger.info("清单结构化模型不可用，按显式顺序保守执行: {}", exc)
                structured_items = []

        manifest_items, manifest_cleaning = reconcile_structured_manifest(
            parsed_items, structured_items
        )
        if not manifest_items:
            return self._clarification(
                "无法可靠识别该自然语言清单。请将任务改为编号或项目符号列表后重试，"
                "我会严格按顺序执行。",
                routing_model,
                manifest_source,
            )
        instructions = [
            str(item.get("instruction") or "") if isinstance(item, dict) else str(item)
            for item in manifest_items
        ]
        if has_unsafe_manifest_instruction(instructions):
            return self._clarification(
                "清单中包含试图改变系统规则、访问敏感数据或越权资源的内容，"
                "因此未启动执行。请移除该内容后重新提交合法任务。",
                routing_model,
                manifest_source,
            )

        manifest = new_manifest(manifest_items, source=manifest_source)
        if manifest["estimated_tokens"] > settings.AGENT_MANIFEST_TOKEN_BUDGET:
            return ManifestSubmissionResult(
                tree=TaskTree(
                    nodes=[],
                    clarification=(
                        f"这份清单预估会消耗约 {manifest['estimated_tokens']} token，超过当前单次任务预算。"
                        "请拆分清单后重试，或明确回复“确认执行该清单”以授权较高预算。"
                    ),
                ),
                routing={
                    "llm": routing_model,
                    "level": "manifest",
                    "mode": "manifest_budget_confirmation",
                    "cache_hit": False,
                    "plan_revision": 1,
                    "manifest_source": manifest_source,
                    "estimated_tokens": manifest["estimated_tokens"],
                    "manifest_cleaning": manifest_cleaning,
                },
            )
        nodes = materialize_manifest_batch(manifest)
        # Deployments and tests may intentionally expose only the bounded
        # react worker.  Apply the same compatibility adaptation to the first
        # rolling window as continuation batches; otherwise routed direct/rag
        # nodes remain without a worker and the manifest cannot progress.
        from app.agents.orchestration.planning.normalizer import adapt_unavailable_manifest_workers

        adapt_unavailable_manifest_workers(nodes, self._workers)
        return ManifestSubmissionResult(
            tree=TaskTree(
                nodes=nodes,
                plan_text=(
                    f"已识别 {len(manifest_items)} 项清单；每项将按直接生成、脚本、检索或智能体通道执行，"
                    f"每批 {manifest['batch_size']} 项。"
                ),
            ),
            routing={
                "llm": routing_model,
                "level": "manifest",
                "mode": "four_channel_manifest",
                "cache_hit": False,
                "plan_revision": 1,
                "manifest": manifest,
                "manifest_progress": manifest_progress(manifest),
                "estimated_tokens": manifest["estimated_tokens"],
            },
        )

    @staticmethod
    def _clarification(
        message: str,
        routing_model: dict,
        manifest_source: dict[str, str],
    ) -> ManifestSubmissionResult:
        from app.agents.orchestration.planner import TaskTree

        return ManifestSubmissionResult(
            tree=TaskTree(nodes=[], clarification=message),
            routing={
                "llm": routing_model,
                "level": "manifest",
                "mode": "manifest_clarification",
                "cache_hit": False,
                "plan_revision": 1,
                "manifest_source": manifest_source,
            },
        )
