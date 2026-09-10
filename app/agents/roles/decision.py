"""受控 DecisionNode：只返回编排决策信号，不执行工具或修改计划。"""

from app.agents.core.base import WorkerAgent, WorkerContext


class DecisionNodeAgent(WorkerAgent):
    name = "decision_node"
    description = "受控阶段决策：请求领域、澄清、继续或结束，不直接调用工具"
    params_help = '{"decision":"request_domain|clarify|continue|finish","domain":"可选领域"}'

    async def execute(self, node, ctx: WorkerContext) -> dict:
        params = node.params or {}
        decision = str(params.get("decision") or "").strip().casefold()
        allowed = {"request_domain", "clarify", "continue", "finish"}
        if decision not in allowed:
            return {"success": False, "error": "DecisionNode 决策类型无效", "error_code": "DECISION_KIND"}
        if decision == "request_domain":
            domain = str(params.get("domain") or "").strip().casefold()
            aliases = {"network": "research", "web": "research", "file": "document", "files": "document", "code": "development"}
            domain = aliases.get(domain, domain)
            if domain not in {"research", "document", "data", "development", "system", "desktop", "schedule", "communication", "writing"}:
                return {"success": False, "error": "DecisionNode 缺少 domain", "error_code": "DECISION_DOMAIN"}
            return {
                "success": True,
                "content": f"请求进入领域：{domain}",
                "output": f"请求进入领域：{domain}",
                "decision": decision,
                "domain": domain,
                "step_title": "阶段决策",
            }
        if decision == "clarify":
            question = str(params.get("question") or "").strip()
            if not question:
                return {"success": False, "error": "DecisionNode 缺少澄清问题", "error_code": "DECISION_QUESTION"}
            return {"success": True, "content": question, "output": question, "decision": decision, "user_action_required": True, "step_title": "等待澄清"}
        return {"success": True, "content": "阶段决策完成", "output": "阶段决策完成", "decision": decision, "step_title": "阶段决策"}
