"""工具与工作流 Skill 的基础抽象。

``Tool`` 是可审计的原子执行能力；``WorkflowSkill`` 是编排层选择的
组合流程说明。两者绝不能混入同一个 Function Calling 候选池：

  - 智能体（LLM）只能调用 Tool，并传入受 Schema 约束的参数
  - Workflow Skill 声明一类任务如何受控地组合多个 Tool
  - 沙箱（Sandbox）提供隔离的执行环境

技能元数据（供场景过滤 / 权限治理 / 管理分组）:
  - category:      功能域（filesystem / shell / process / system / network / devtools / desktop / mcp）
  - environment:   执行环境（server=后端直接执行 / sandbox=本地子进程隔离 / client=推送用户端执行）
  - permission:    权限级别（user / admin）
  - requires_confirmation: 高危操作，执行前需用户确认（client 通道二期实现）
  - scenes:        可用场景白名单（空 = 全场景）
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.skills.output_contract import ToolOutput


# 仅保留名称别名，避免旧业务导入破坏；运行时所有结果都是同一个
# ToolOutput 类型，MCP/LangGraph/DAG 不再需要区分两套结果对象。
SkillResult = ToolOutput


class SkillProgress(BaseModel):
    """跨 MCP、Skill、DAG 与 SSE 的稳定进度事件契约。"""

    task_id: str = ""
    job_id: str = ""
    node_id: str | None = None
    skill_name: str
    phase: Literal["started", "awaiting_confirmation", "executing", "completed", "failed", "cancelled"]
    percentage: float | None = None
    message: str = ""


@dataclass
class SkillContext:
    """技能执行上下文（技能需要的用户/会话信息，由执行器注入，不来自 LLM 参数）."""

    user_id: str = ""
    scene: str = "chat"
    conversation_id: str = ""
    # Correlation id of the currently executing DAG job.  Direct chat skills
    # may leave it empty; the executor then uses ``conversation_id`` as the
    # stable fallback.  Keeping both avoids conflating a user conversation
    # with a retried/resumed office job.
    job_id: str = ""
    # BYOK：用户自备 API key（由执行器透传，仅本次调用临时使用，不落库）
    llm_api_key: str | None = None
    llm_config: dict[str, Any] | None = None
    # 进度通知回调（如"正在请求访问本地文件…"），流式模式下展示给用户
    on_notify: Callable[[str], None] | None = None
    # Async callback receiving generated text deltas (office text only).
    on_output: Callable[[str], object] | None = None
    # Executor-only policy.  Never hydrate this from tool arguments, document
    # text or persisted conversation state.
    execution_policy: dict | None = None
    # Optional declarative Workflow Skill prompt loaded from Prompt-as-Code.
    # It is advisory business guidance and never replaces system safety rules.
    skill_prompt: str = ""
    # Server-injected documents authorized for this single task. Tool
    # arguments can narrow this set but can never expand it.
    office_doc_ids: tuple[str, ...] = ()
    # Server-injected project scope. Project/code tools must never infer this
    # from the user prompt or tool arguments.
    authorized_project_ids: tuple[str, ...] = ()


class Tool(ABC):
    """原子工具基类：每次调用只完成一个可审计动作。

    工具可以是纯读、受确认保护的写操作或客户端能力，但不能把“检索 →
    推理 → 再调用另一个工具”的业务工作流藏在 ``execute`` 内。那类逻辑必须
    由 ``WorkflowSkill`` 声明，并经 Skill Runner 调用本执行器。
    """

    name: str = ""
    description: str = ""
    # Tool 是可被计划缓存和长期 Job 引用的能力 API，而不是一次性的 Python
    # 函数。版本和状态用于在插件热更新后维持恢复/灰度的确定性。
    version: str = "1.0.0"
    status: str = "stable"  # experimental / stable / deprecated / disabled
    replacement_skill_id: str = ""
    category: str = "general"           # 功能域，用于分组/过滤
    resource: str = ""                   # 根资源，用于 L1 域分组
    environment: str = "server"         # server / sandbox / client
    permission: str = "user"            # user / admin
    requires_confirmation: bool = False  # 高危操作需用户确认（client 通道）
    scenes: list[str] = []              # 可用场景白名单，空 = 全场景
    write_op: bool = False              # 是否写操作（发消息/改文件/装依赖等外部副作用；渐进开放时隐藏）
    idempotent: bool = True              # 相同参数重复执行是否安全
    # 是否允许被普通用户的声明式 Workflow Skill 引用。默认关闭；即使某个
    # Tool 本身可执行，也不能自动成为用户可组合的能力。
    user_workflow_allowed: bool = False
    resource_templates: list[str] = []   # 如 project:{project_id}:file:{path}
    # Planner/TCA 可选能力元数据；默认值保持已有 Tool 声明的向后兼容。
    cost_estimate: float = 1.0
    success_rate: float | None = None
    requires: list[str] = []
    produces: list[str] = []
    deterministic: bool = False
    fallback_group: str = ""
    # P1：供调度器缩小工具命名空间的语义元数据。空值保持历史插件兼容，
    # 未迁移插件可由集中目录补齐，不把路由规则散落进每个调用点。
    domain: str = ""
    intent_tags: list[str] = []
    conflicts_with: list[str] = []
    preferred_over: list[str] = []
    # 面向模型的选择契约：不仅描述“能做什么”，还明确排除相邻能力。
    use_when: list[str] = []
    do_not_use_when: list[str] = []
    selection_examples: list[str] = []
    result_contract: str = ""
    # Machine-checkable relationships; prose boundaries remain model-facing.
    handoff_to: list[str] = []
    bootstrap_intents: list[str] = []
    bootstrap_until: str = ""  # ISO date; empty means no bootstrap bypass
    # 参数 JSON Schema（LLM 调用时校验参数用），空 dict 表示无参数
    parameters_schema: dict = Field(default_factory=dict)
    # 直接执行契约：DAG Planner 已选定工具后，执行器可用原子步骤的完整
    # instruction 填充到该参数，而无需再发起 Function Calling。空字符串表示
    # 此工具只接受 Planner 给出的显式 inputs。
    direct_instruction_field: str = ""
    # ``parameters_schema.required`` 是 Function Calling 的通用契约；部分
    # 工具（如写作）支持一个 instruction 覆盖多个结构字段。这里声明直接
    # 执行时真正需要的字段，空列表时沿用 JSON Schema 的 required。
    direct_required_fields: list[str] = []
    # 兼容 Planner 内部字段与实际工具字段的受控映射，例如 analyze_mode -> mode。
    # 只允许声明的映射，执行器不会猜测或注入未知参数。
    direct_input_aliases: dict[str, str] = {}
    # 规划时必须明确提供、不能从 instruction 或上游文本安全推断的对象标识。
    # 缺失时由编译器要求澄清，不能等到执行中让模型猜测目标。
    plan_required_fields: list[str] = []
    # 工具只声明事实；执行引擎负责按预算、状态和敏感字段规则渲染。
    output_contract: dict[str, Any] = {
        "content_type": "text",
        "budget": 500,
        "truncate_strategy": "head_tail",
        "on_overflow": "compress",
    }

    @abstractmethod
    async def execute(self, params: dict, context: SkillContext | None = None) -> ToolOutput:
        """执行一个原子工具调用。"""
        ...

    @property
    def requires_sandbox(self) -> bool:
        """是否需要在隔离沙箱中执行（environment == sandbox）."""
        return self.environment == "sandbox"

    def supports_scene(self, scene: str) -> bool:
        """是否支持指定场景."""
        if not self.scenes:
            return True
        return scene in self.scenes

    def requires_confirmation_for(self, params: dict | None = None) -> bool:
        """Return whether this invocation needs confirmation.

        Most skills have a fixed confirmation policy.  Skills exposing both
        read and write actions can override this method so the policy follows
        the concrete action instead of blocking harmless reads.
        """
        return bool(self.requires_confirmation)

    def is_write_operation(self, params: dict | None = None) -> bool:
        """Return whether this concrete invocation can change external state."""
        return bool(self.write_op)

    def to_tool_definition(self) -> dict:
        """转成 OpenAI/Qwen 兼容的 function calling 工具定义."""
        desc = self.description
        if self.requires_confirmation:
            desc += "（高危操作：执行前需要用户确认）"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": desc,
                "parameters": self.parameters_schema,
            },
        }

    def capability_metadata(self) -> dict:
        """Return scheduler metadata without exposing implementation paths or secrets."""
        return {
            "name": self.name,
            "version": self.version,
            "status": self.status,
            "replacement_skill_id": self.replacement_skill_id,
            "schema_fingerprint": self.schema_fingerprint,
            "category": self.category,
            "environment": self.environment,
            "permission": self.permission,
            "write_op": self.write_op,
            "idempotent": self.idempotent,
            "cost_estimate": self.cost_estimate,
            "success_rate": self.success_rate,
            "requires": list(self.requires),
            "produces": list(self.produces),
            "deterministic": self.deterministic,
            "fallback_group": self.fallback_group,
            "domain": self.domain,
            "intent_tags": list(self.intent_tags),
            "conflicts_with": list(self.conflicts_with),
            "preferred_over": list(self.preferred_over),
            "use_when": list(self.use_when),
            "do_not_use_when": list(self.do_not_use_when),
            "selection_examples": list(self.selection_examples),
            "result_contract": self.result_contract,
            "handoff_to": list(self.handoff_to),
            "bootstrap_intents": list(self.bootstrap_intents),
            "bootstrap_until": self.bootstrap_until,
            "direct_instruction_field": self.direct_instruction_field,
            "direct_required_fields": list(self.direct_required_fields),
            "direct_input_aliases": dict(self.direct_input_aliases),
            "plan_required_fields": list(self.plan_required_fields),
            "output_contract": dict(self.get_output_contract()),
        }

    def get_output_contract(self) -> dict[str, Any]:
        """返回声明式输出契约，供执行引擎读取。"""
        return dict(self.output_contract)

    @property
    def schema_fingerprint(self) -> str:
        """Stable identity for the callable contract used by persisted plans."""
        payload = {
            "name": self.name,
            "version": self.version,
            "parameters": self.parameters_schema if isinstance(self.parameters_schema, dict) else {},
            "environment": self.environment,
        }
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

    def validate_lifecycle(self) -> None:
        if not self.name:
            raise ValueError("Tool name 不能为空")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(self.version or "")):
            raise ValueError(f"Tool {self.name} 的 version 必须是 semver")
        if self.status not in {"experimental", "stable", "deprecated", "disabled"}:
            raise ValueError(f"Tool {self.name} 的 status 无效: {self.status}")

    def __repr__(self) -> str:
        return (
            f"<Tool: {self.name} cat={self.category} env={self.environment} "
            f"permission={self.permission} scenes={self.scenes or 'all'}>"
        )


class WorkflowSkill:
    """不可直接 Function Calling 的组合工作流定义。

    ``allowed_tools`` 是工作流的最小工具可见面。Skill Runner 在每次实际
    调用前仍委托统一工具执行器完成授权、审计、确认、副作用日志与互斥锁，
    因而 Skill 不能借由组合逻辑绕过 Tool 的安全边界。
    """

    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    status: str = "stable"
    category: str = "general"
    scenes: list[str] = []
    allowed_tools: list[str] = []
    environment: str = "server"
    write_op: bool = False
    idempotent: bool = True
    permission: str = "user"
    use_when: list[str] = []
    do_not_use_when: list[str] = []
    handoff_to: list[str] = []
    conflicts_with: list[str] = []
    preferred_over: list[str] = []
    intent_tags: list[str] = []
    bootstrap_intents: list[str] = []
    bootstrap_until: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    # ``None`` 表示开发者预置的公共 Skill；用户自建 Skill 必须绑定 UUID。
    owner_user_id: str | None = None
    visibility: str = "public"  # public / private
    source: str = "developer"   # developer / user
    # Prompt-as-Code: optional external Markdown body for developer skills.
    # The Python class remains the compatibility/runtime adapter while the
    # business procedure can be edited without changing orchestration code.
    prompt_body: str = ""
    prompt_version: str = ""
    prompt_file: str = ""

    def supports_scene(self, scene: str) -> bool:
        return not self.scenes or scene in self.scenes

    def effective_prompt(self) -> str:
        """Return the declarative prompt body, if one was supplied."""
        return str(self.prompt_body or "").strip()

    def validate_lifecycle(self) -> None:
        if not self.name:
            raise ValueError("WorkflowSkill name 不能为空")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(self.version or "")):
            raise ValueError(f"WorkflowSkill {self.name} 的 version 必须是 semver")
        if self.status not in {"experimental", "stable", "deprecated", "disabled"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 status 无效: {self.status}")
        if self.visibility not in {"public", "private"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 visibility 无效")
        if self.source not in {"developer", "user"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 source 无效")
        if self.source == "user" and not self.owner_user_id:
            raise ValueError(f"用户 Skill {self.name} 必须绑定 owner_user_id")
        if self.source == "user" and self.visibility != "private":
            raise ValueError(f"用户 Skill {self.name} 只能是 private")

    async def run(self, params: dict, context: SkillContext, invoke_tool: Callable[..., Any]) -> ToolOutput:
        """组合流程入口；迁移中的旧实现可暂时通过 ``execute`` 适配。"""
        execute = getattr(self, "execute", None)
        if execute is None:
            raise NotImplementedError(f"WorkflowSkill {self.name} 未实现 run")
        return await execute(params, context)
