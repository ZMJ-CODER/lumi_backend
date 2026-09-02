"""模板与模式规划策略。"""

from __future__ import annotations

from loguru import logger

from app.agents.orchestration.models import TaskNode
from app.agents.orchestration.planning.contracts import PlannerModelError, TaskTree
from app.agents.orchestration.planning.template_params import template_default_params
from app.agents.skills.recovery import classify_model_error


class PlannerStrategies:
    async def template(
        self, *, user_id: str, request: str, office_docs: list[dict] | None, llm_api_key: str | None,
        force_template: str | None = None, prior_summaries: str = "", llm_config: dict | None = None,
    ) -> TaskTree | None:
        from app.agents.langchain.planning import invoke_json_object
        from app.agents.orchestration.planning.templates import get_template, template_catalog_text

        if force_template and (template := get_template(force_template)):
            nodes = template.build(request, template_default_params(force_template, request), office_docs)
            if nodes:
                logger.info("[Planner] 模板 {} 构建 DAG（规则抽参，{} 节点）", force_template, len(nodes))
                return TaskTree(nodes=[TaskNode(**node) for node in nodes], plan_text=f"按模板 {force_template} 执行：{request}")
        doc_desc = "、".join(f"{item.get('filename')}(doc_id={item.get('doc_id')})" for item in office_docs or [] if item.get("doc_id"))
        prompt = (
            "你是办公流程规划器。根据用户请求，从模板库选择最匹配的模板并抽取参数。\n可用模板：\n"
            + template_catalog_text() + f"\n当前上传的文档：{doc_desc or '（无）'}\n"
            + (f"\n此前已完成的任务摘要（延续上下文用）：\n{prior_summaries}\n" if prior_summaries else "")
            + "只输出 JSON（不要 Markdown 围栏、不要解释）：{\"template\": \"模板名或null\", \"params\": {\"参数名\": 值}}\n没有合适模板时 template 为 null。"
        )
        try:
            kwargs = {"user_id": user_id, "api_key": llm_api_key, "max_tokens": 2000}
            if llm_config is not None:
                kwargs["llm_config"] = llm_config
            data = await invoke_json_object(prompt, **kwargs)
            template = get_template(str(data.get("template") or "")) if data else None
            if template is None or not (nodes := template.build(request, dict(data.get("params") or {}), office_docs)):
                return None
            logger.info("[Planner] 模板 {} 构建 DAG（{} 节点）", data.get("template"), len(nodes))
            return TaskTree(nodes=[TaskNode(**node) for node in nodes])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Planner] 模板规划失败，回退自由规划: {}", exc)
            self._raise_model_error(exc)
            return None

    async def pattern(
        self, *, user_id: str, request: str, office_docs: list[dict] | None, llm_api_key: str | None,
        prior_summaries: str = "", llm_config: dict | None = None,
    ) -> TaskTree | None:
        from app.agents.langchain.planning import invoke_json_object
        from app.agents.orchestration.planning.patterns import build_pattern, pattern_catalog_text

        prompt = (
            "你是办公流程规划器。用户请求是带条件的半结构任务，请选择一个模式并抽取参数。\n"
            + pattern_catalog_text() + "\n只输出 JSON（不要 Markdown 围栏）：{\"pattern\": \"模式名\", \"params\": {\"参数名\": 值}}\n"
            + (f"此前已完成的任务摘要（延续上下文用）：\n{prior_summaries}\n" if prior_summaries else "")
        )
        try:
            kwargs = {"user_id": user_id, "api_key": llm_api_key, "max_tokens": 2000}
            if llm_config is not None:
                kwargs["llm_config"] = llm_config
            data = await invoke_json_object(prompt + f"\n用户请求：{request}", **kwargs)
            if not data:
                return None
            name = str(data.get("pattern") or "")
            nodes = build_pattern(name, request, dict(data.get("params") or {}), office_docs)
            if not nodes:
                return None
            logger.info("[Planner] 模式 {} 构建 DAG（{} 节点）", name, len(nodes))
            return TaskTree(nodes=[TaskNode(**node) for node in nodes], plan_text=f"按模式 {name} 执行：{request}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Planner] 模式规划失败，回退自由规划: {}", exc)
            self._raise_model_error(exc)
            return None

    @staticmethod
    def _raise_model_error(exc: Exception) -> None:
        code, message = classify_model_error(exc)
        if code in {"MODEL_INSUFFICIENT_BALANCE", "MODEL_AUTH_ERROR", "MODEL_NOT_FOUND", "MODEL_CONFIG_ERROR", "MODEL_PROVIDER_UNAVAILABLE", "MODEL_CONNECTION_ERROR", "MODEL_UNAVAILABLE"}:
            raise PlannerModelError(code, message) from exc


__all__ = ["PlannerStrategies"]
