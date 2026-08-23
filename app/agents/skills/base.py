"""技能抽象 —— 智能体可调用的能力单元.

技能层只定义"智能体能做什么"（名称 / 描述 / 参数 / 执行），
不关心"在哪里执行"——执行环境由沙箱层负责，两者解耦：

  - 智能体（LLM）决定调用哪个技能、传什么参数
  - 技能（Skill）声明能力契约
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


class SkillResult(BaseModel):
    """技能执行结果."""

    success: bool
    output: str = ""
    error: str | None = None
    # 标准错误码：SKILL_NOT_FOUND / INVALID_ARGS / NEEDS_CONFIRMATION /
    #              EXEC_ERROR / TIMEOUT / REJECTED
    error_code: str | None = None
    # 网络/瞬时类错误可重试（True 时 LLM 可再调一次），参数类错误不可重试
    retryable: bool = False
    metadata: dict = Field(default_factory=dict)

    def decision_signals(self) -> dict:
        """Small, model-facing summary for deciding the next tool step."""
        metadata = self.metadata if isinstance(self.metadata, dict) else {}
        signals = metadata.get("decision_signals")
        if isinstance(signals, dict):
            normalized = dict(signals)
            hint = normalized.get("confidence_hint")
            # A confidence label without an observable basis is worse than no
            # label: it can make the model over-trust an arbitrary string.
            if not (
                isinstance(hint, dict)
                and hint.get("level") in {"low", "medium", "high"}
                and isinstance(hint.get("basis"), list)
                and any(str(item).strip() for item in hint["basis"])
            ):
                normalized.pop("confidence_hint", None)
            return normalized
        return {
            "result_count": metadata.get("result_count"),
            "more_available": bool(metadata.get("more_available", False)),
            "truncated": bool(metadata.get("truncated", False)),
        }


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
    # Server-injected documents authorized for this single task. Tool
    # arguments can narrow this set but can never expand it.
    office_doc_ids: tuple[str, ...] = ()


class Skill(ABC):
    """技能基类：新增技能时继承并注册到 SkillRegistry."""

    name: str = ""
    description: str = ""
    # Skill 是可被计划缓存和长期 Job 引用的能力 API，而不是一次性的 Python
    # 函数。版本和状态用于在插件热更新后维持恢复/灰度的确定性。
    version: str = "1.0.0"
    status: str = "stable"  # experimental / stable / deprecated / disabled
    replacement_skill_id: str = ""
    category: str = "general"           # 功能域，用于分组/过滤
    environment: str = "server"         # server / sandbox / client
    permission: str = "user"            # user / admin
    requires_confirmation: bool = False  # 高危操作需用户确认（client 通道）
    scenes: list[str] = []              # 可用场景白名单，空 = 全场景
    write_op: bool = False              # 是否写操作（发消息/改文件/装依赖等外部副作用；渐进开放时隐藏）
    idempotent: bool = True              # 相同参数重复执行是否安全
    resource_templates: list[str] = []   # 如 project:{project_id}:file:{path}
    # Planner/TCA 可选能力元数据；默认值保持所有已有 Skill 向后兼容。
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

    @abstractmethod
    async def execute(self, params: dict, context: SkillContext | None = None) -> SkillResult:
        """执行技能."""
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
        }

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
            raise ValueError("Skill name 不能为空")
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", str(self.version or "")):
            raise ValueError(f"Skill {self.name} 的 version 必须是 semver")
        if self.status not in {"experimental", "stable", "deprecated", "disabled"}:
            raise ValueError(f"Skill {self.name} 的 status 无效: {self.status}")

    def __repr__(self) -> str:
        return (
            f"<Skill: {self.name} cat={self.category} env={self.environment} "
            f"permission={self.permission} scenes={self.scenes or 'all'}>"
        )
