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
    # The active desktop workspace is selected by the authenticated request,
    # never by a model tool argument.  Workspace workflows use this value to
    # bind every Electron MCP call to one local project.
    workspace_id: str = ""
    # 阶段 4 收口：本次执行属于哪个**插件**（由服务端注入，绝不来自 LLM 参数/文档
    # 内容）。空 = 内置执行：系统默认上限，不按插件配额治理。
    # 带 plugin_id 时，执行必须经 ``PluginManager.worker_for`` 拿到配额边界；
    # 拿不到就按稳定码阻断（``PLUGIN_WORKER_UNAVAILABLE`` 等），不回退进程内执行。
    plugin_id: str = ""
    # 可选的服务端注入 Manifest（缺省时由 PluginManager 从注册表取，唯一权威仍是注册表）。
    plugin_manifest: Any = None


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
    # 审批档位 / 审批策略的**声明位**（空 = 交给统一工具注册表派生）。
    # 插件作者在这里声明一次，审批引擎与注册表读的就是同一份事实；这比"新增一个工具
    # 就要回来改 approval_policy.py 的私有词表"更不容易漏（实测漏过一次：
    # workspace_code_scan 掉进了 workspace_ 前缀兜底）。
    risk_tier: str = ""                 # auto / routine / critical
    approval_policy: str = ""           # none / confirm
    #: **能力归属声明**（形如 ``workspace.read``）：空 = 由静态映射表派生。
    #: 静态表认识的工具不受它影响（表优先）；它存在的意义是让**新工具**不用回来
    #: 改 ``TOOL_CAPABILITY_MAP`` 也能接上路由/审批/动作窗口。
    capability: str = ""
    #: **资源类型声明**（``workspace`` / ``memory`` …）：空 = 由能力绑定推导。
    #: 统一能力（``resource.write``）跨多种资源，因此"新资源"必须能自己声明类型——
    #: Phase 6 的 ``memory_provider`` 验收就是靠它做到"不改任何静态映射"。
    resource_type: str = ""
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

    def declared_risk_tier(self) -> str:
        """工具声明的审批档位（``auto``/``routine``/``critical``）；其余一律视为未声明。

        非法值不能"就近取一个"：档位是安全边界，写错的声明必须退回**派生**路径，
        而不是悄悄变成某个更容易放行的档位。
        """
        value = str(self.risk_tier or "").strip().casefold()
        return value if value in {"auto", "routine", "critical"} else ""

    def declared_approval_policy(self) -> str:
        """工具声明的审批策略（``none``/``confirm``）；空或未知 = 未声明。"""
        value = str(self.approval_policy or "").strip().casefold()
        return value if value in {"none", "confirm"} else ""

    def declared_capability(self) -> str:
        """工具声明的能力归属（``workspace.read`` 这类点分名）；非法一律视为未声明。

        只接受点分能力名：能力名是要拿去查目录、定路由、判租约的键，写错的声明必须
        退回派生，而不是变成一个查不到的"能力"。
        """
        value = str(self.capability or "").strip()
        if not value or value != value.casefold() or "." not in value:
            return ""
        if not value[0].isalpha() or not all(ch.isalnum() or ch in "._" for ch in value):
            return ""
        return value

    def declared_resource_type(self) -> str:
        """工具声明的资源类型（``workspace`` / ``memory`` …）；非法一律视为未声明。

        与能力声明同一条原则：写错的资源类型必须退回派生，而不是变成一个查不到的"资源"。
        """
        value = str(self.resource_type or "").strip().casefold()
        if not value or not value[0].isalpha():
            return ""
        if not all(ch.isalnum() or ch in "_." for ch in value):
            return ""
        return value

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
    # Capability-first dispatcher contract. Legacy Skills can be migrated
    # gradually; absent fields receive a conservative derived capability.
    provided_goals: list[str] = []
    provided_sources: list[str] = []
    safety_level: str = "READ_ONLY"
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
    # Declarative execution manifest.  Legacy Skills derive dependencies from
    # allowed_tools, so introducing the manifest does not invalidate persisted
    # jobs or developer plugins that have not migrated yet.
    dependencies: dict[str, Any] = {}
    execution_scope: str = "backend"
    availability_policy: str = "fail_if_missing"
    fallback_policy: str = "clarify"
    approval_policy: str = "none"
    # ── 统一资源能力声明（方案《资源能力层》Phase 4）──
    # 迁移后的 Skill **只声明能力**，不再逐条列出底层 MCP 名字：
    # ``workspace_code_change`` 从 14 个 ``mcp__lumi_client__…`` 名收敛成
    # "对 workspace 做 resource.read / resource.write / resource.edit / code.execute"。
    # 空列表 = 走旧 ``allowed_tools`` 兼容路径（旧 Skill 与用户自建 Skill 不受影响）。
    required_capabilities: list[str] = []
    resource_types: list[str] = []
    providers: list[str] = []

    def declared_capabilities(self) -> list[str]:
        """归一化后的**统一能力**声明（别名归一 + 去重 + 拒非法）。

        非法值一律丢掉而不是"就近取一个"：能力名是路由/审批/租约的键，
        写错的能力声明必须退回旧路径，不能悄悄变成另一个能力。
        """
        from app.agents.capabilities.catalog.resource import (
            is_unified_capability,
            normalize_unified_capability,
        )

        out: list[str] = []
        for item in self.required_capabilities or ():
            name = normalize_unified_capability(str(item or "").strip())
            if name and is_unified_capability(name) and name not in out:
                out.append(name)
        return out

    def legacy_tool_capabilities(self) -> list[str]:
        """旧 ``allowed_tools``（MCP 原子名）→ 统一能力（**兼容解析**）。

        这就是"旧 MCP 依赖 → 新能力依赖"的转换：老 Skill 不改一行代码也能被
        能力层理解，从而在 Phase 5 收敛工具面时不至于被漏掉。
        """
        from app.agents.capabilities.catalog.resource import binding_for_tool

        out: list[str] = []
        for name in self.allowed_tools or ():
            binding = binding_for_tool(str(name or ""))
            if binding.known and binding.capability not in out:
                out.append(binding.capability)
        return out

    def effective_capabilities(self) -> list[str]:
        """生效能力集合：**显式声明优先**，未声明时从 ``allowed_tools`` 推导。"""
        declared = self.declared_capabilities()
        return declared if declared else self.legacy_tool_capabilities()

    def effective_resource_types(self) -> list[str]:
        """生效资源类型：显式声明优先，否则从工具绑定推导。

        **歧义不猜**：只声明了 ``resource.write`` 而没有资源类型时，它可能落在
        workspace / office_document / … 上，这里返回空（由调用方决定要不要澄清），
        而不是随便挑一个资源。
        """
        from app.agents.capabilities.catalog.resource import (
            RESOURCE_TYPES,
            binding_for_tool,
            providers_for,
        )

        declared = [
            str(item or "").strip()
            for item in (self.resource_types or ())
            if str(item or "").strip()
        ]
        if declared:
            return list(dict.fromkeys(declared))
        if self.declared_capabilities():
            types: list[str] = []
            for capability in self.declared_capabilities():
                matches = [rtype for rtype in sorted(RESOURCE_TYPES) if providers_for(capability, rtype)]
                if len(matches) == 1 and matches[0] not in types:
                    types.append(matches[0])
            return types
        types = []
        for name in self.allowed_tools or ():
            binding = binding_for_tool(str(name or ""))
            if binding.known and binding.resource_type not in types:
                types.append(binding.resource_type)
        return types

    def effective_providers(self) -> list[str]:
        """生效 Provider：显式声明 ∪ 由能力推导的候选（去重保序）。"""
        from app.agents.capabilities.catalog.resource import providers_for

        out = [str(item or "").strip() for item in (self.providers or ()) if str(item or "").strip()]
        out = list(dict.fromkeys(out))
        for capability in self.effective_capabilities():
            for resource_type in self.effective_resource_types():
                for spec in providers_for(capability, resource_type):
                    if spec.name not in out:
                        out.append(spec.name)
        return out

    def capability_dependencies(self) -> dict[str, Any]:
        """能力视图（落 Job 快照 / API / 排障用；不含任何参数与正文）。"""
        return {
            "capabilities": self.effective_capabilities(),
            "resource_types": self.effective_resource_types(),
            "providers": self.effective_providers(),
            "declared": bool(self.declared_capabilities()),
        }

    def effective_dependencies(self) -> dict[str, Any]:
        manifest = dict(self.dependencies or {})
        tools = list(manifest.get("tools") or [])
        declared = {str(item.get("name") or "") for item in tools if isinstance(item, dict)}
        for name in self.allowed_tools:
            if name and name not in declared:
                tools.append({"name": name, "min_version": "0.0.0", "required": True, "provider": "any"})
        manifest["tools"] = tools
        # Phase 4：能力声明与工具依赖**并存**。旧 ``allowed_tools`` 行原样保留
        # （一个版本周期的兼容），能力派生的工具行只**补充**、不替换——
        # 否则"迁移到能力声明"会变成"依赖检查变松"，那是回归。
        capability_view = self.capability_dependencies()
        manifest.setdefault("capabilities", capability_view["capabilities"])
        manifest.setdefault("resource_types", capability_view["resource_types"])
        for row in self.capability_tool_rows():
            if row["name"] not in declared:
                tools.append(row)
                declared.add(row["name"])
        providers = list(manifest.get("providers") or [])
        for item in capability_view["providers"]:
            if item not in providers:
                providers.append(item)
        manifest["providers"] = providers
        manifest.setdefault("sources", [])
        return manifest

    def capability_tool_rows(self) -> list[dict[str, Any]]:
        """能力声明 → 依赖行（``resolve_dependencies`` 吃的形状）。

        只有**显式声明了能力**才派生：老 Skill 的依赖检查口径保持不变。
        """
        if not self.declared_capabilities():
            return []
        from app.agents.capabilities.policy.resource_workflow import tools_for_capabilities

        rows: list[dict[str, Any]] = []
        for name in tools_for_capabilities(
            self.effective_capabilities(), self.effective_resource_types()
        ):
            rows.append(
                {
                    "name": name,
                    "min_version": "0.0.0",
                    "required": False,
                    "provider": "any",
                    "via": "capability",
                }
            )
        return rows

    def supports_scene(self, scene: str) -> bool:
        return not self.scenes or scene in self.scenes

    def effective_prompt(self) -> str:
        """Return the declarative prompt body, if one was supplied.

        模型可见面收敛（Phase 5）打开时，把提示词里的**实现层工具名**翻译成对外名，
        保证"提示词说的名字"与"schema 里的名字"一致；关闭时逐字返回原文。
        业务文本本身不改——SOP 由提示词负责人维护。
        """
        text = str(self.prompt_body or "").strip()
        if not text:
            return text
        try:
            from app.agents.capabilities.views.resource_surface import translate_prompt_names

            return translate_prompt_names(text)
        except Exception:  # noqa: BLE001 - 翻译失败用原文，绝不因为改名丢掉提示词
            return text

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
        if self.execution_scope not in {"backend", "client", "backend_orchestrates_client"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 execution_scope 无效")
        if self.availability_policy not in {"require_online_client", "allow_server_fallback", "fail_if_missing"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 availability_policy 无效")
        if self.fallback_policy not in {"fail", "clarify", "direct_answer", "alternate_tool"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 fallback_policy 无效")
        if self.approval_policy not in {"none", "before_write", "before_submit"}:
            raise ValueError(f"WorkflowSkill {self.name} 的 approval_policy 无效")

    async def run(self, params: dict, context: SkillContext, invoke_tool: Callable[..., Any]) -> ToolOutput:
        """组合流程入口；迁移中的旧实现可暂时通过 ``execute`` 适配。"""
        execute = getattr(self, "execute", None)
        if execute is None:
            raise NotImplementedError(f"WorkflowSkill {self.name} 未实现 run")
        return await execute(params, context)
