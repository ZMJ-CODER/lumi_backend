"""统一**资源能力层**（方案《资源能力层》Phase 1：兼容能力目录）。

## 它解决什么问题

今天"模型能调什么"由四套并行体系共同决定，各自有名称、映射和注入逻辑：

```text
Office 工具 / Workspace 工具 / MCP 工具 / 内部 Skill 工具
```

于是同一个动作在链路上有 4 个名字（``workspace_write`` / ``workspace_stage_write`` /
``mcp__lumi_client__workspace_write`` / ``office_doc_edit``），每加一个 Provider 就要在
Router、Preflight、ChatGraph、ReActRunner、Broker 静态映射里各改一处。

本模块引入**一个**中间抽象：模型只表达"对某种资源做某个操作"，
由 Provider 决定"具体用哪个原子工具"。

```text
resource.read / resource.write / resource.edit / resource.move / resource.delete
code.execute / artifact.create            ← 统一能力（模型与编排只认这一层）
        │
        ├── resource_type = workspace       → workspace_provider → workspace_write
        ├── resource_type = office_document → office_document_provider → office_doc_edit
        ├── resource_type = knowledge       → knowledge_provider（尚未注册实现）
        └── resource_type = artifact        → artifact_provider → create_office_document
```

**Phase 1 只做"兼容目录"**：给现有工具补上 ``capability`` / ``resource_type`` /
``provider`` 元数据，并把旧工具名映射到新能力。**不改任何执行路径**——
真正的切换（预检、工具窗口、Broker 派发）在后面几个 Phase，且都会带开关与旧表兜底。

## 三条不变量

1. **不认识就不猜**：没有绑定关系的工具返回 ``source="unknown"`` 且能力为空，
   绝不"按名字里有没有 write 猜一个"（猜错的代价是把写操作当只读派发）；
2. **旧能力名仍然是合法输入**：兼容层接受 ``workspace.read`` 这类旧名，
   统一能力名是它的上层表达，不是替换（客户端协议冻结在旧名上）；
3. **Provider 声明只是候选**：同一 (能力, 资源类型) 可以有多个 Provider
   （工作区写入既有 workspace_provider 也有 git_provider），
   选择权在 Broker（Phase 3），这里只给出**有序候选**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from loguru import logger

# ── 统一能力词表 ────────────────────────────────────────────

UNIFIED_RESOURCE_READ = "resource.read"
UNIFIED_RESOURCE_WRITE = "resource.write"
UNIFIED_RESOURCE_EDIT = "resource.edit"
UNIFIED_RESOURCE_MOVE = "resource.move"
UNIFIED_RESOURCE_DELETE = "resource.delete"
UNIFIED_CODE_EXECUTE = "code.execute"
UNIFIED_ARTIFACT_CREATE = "artifact.create"

#: 全部统一能力（模型与编排只认这一层）。
UNIFIED_CAPABILITIES: frozenset[str] = frozenset(
    {
        UNIFIED_RESOURCE_READ,
        UNIFIED_RESOURCE_WRITE,
        UNIFIED_RESOURCE_EDIT,
        UNIFIED_RESOURCE_MOVE,
        UNIFIED_RESOURCE_DELETE,
        UNIFIED_CODE_EXECUTE,
        UNIFIED_ARTIFACT_CREATE,
    }
)

#: 方案里出现过、但与既有能力是**同一件事**的写法。
#: ``sandbox.run`` 就是既有 ``code.execute``（沙箱执行），保留别名而不是加第二个概念。
UNIFIED_CAPABILITY_ALIASES: dict[str, str] = {
    "sandbox.run": UNIFIED_CODE_EXECUTE,
    "resource.search": UNIFIED_RESOURCE_READ,  # 检索是读取的一种，不是新能力
}

# ── 资源类型词表 ────────────────────────────────────────────

RESOURCE_WORKSPACE = "workspace"
RESOURCE_OFFICE_DOCUMENT = "office_document"
RESOURCE_KNOWLEDGE = "knowledge"
RESOURCE_ARTIFACT = "artifact"
#: 任务内工作记忆（服务端 Redis）：Phase 6 的验收资源类型——**新增资源类型不改任何静态映射**。
RESOURCE_MEMORY = "memory"

RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        RESOURCE_WORKSPACE,
        RESOURCE_OFFICE_DOCUMENT,
        RESOURCE_KNOWLEDGE,
        RESOURCE_ARTIFACT,
        RESOURCE_MEMORY,
    }
)

# ── 旧能力 → 统一能力 + 资源类型（**唯一**的兼容映射表）────────
#
# 旧能力名来自 `capabilities/catalog.py` 的九张能力声明（客户端协议冻结在它们上面），
# 这张表是"旧名 → 新名"的**唯一**落点：以后新增 Provider 只加这张表与 Provider 声明，
# 不再回头改 Router/Preflight/Broker。
LEGACY_CAPABILITY_BINDINGS: dict[str, tuple[str, str]] = {
    "workspace.read": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "workspace.write": (UNIFIED_RESOURCE_WRITE, RESOURCE_WORKSPACE),
    "workspace.edit": (UNIFIED_RESOURCE_EDIT, RESOURCE_WORKSPACE),
    "workspace.move": (UNIFIED_RESOURCE_MOVE, RESOURCE_WORKSPACE),
    "workspace.delete": (UNIFIED_RESOURCE_DELETE, RESOURCE_WORKSPACE),
    # 代码骨架扫描读的是工作区文件（只给骨架不给正文）→ 读取。
    "code.scan": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "code.execute": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    # git 操作按"提交/推送是对工作区的写"归类；只读的 `workspace_diff` 由工具级例外纠正。
    "git.operations": (UNIFIED_RESOURCE_WRITE, RESOURCE_WORKSPACE),
    # 产物落在用户的产物目录：资源类型是产物本身，不是工作区。
    "artifact.create": (UNIFIED_ARTIFACT_CREATE, RESOURCE_ARTIFACT),
}

#: **工具级例外**：能力级映射不够精确的地方。
#: 只写"能力级答案会被它纠正"的条目，并各自说明理由。
TOOL_BINDINGS: dict[str, tuple[str, str]] = {
    # ``git.operations`` 里既有写（commit）也有纯读（diff）：diff 不该按写治理。
    "workspace_diff": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    # 办公文档：读取/分析/显式抽取都是读；编辑是写。资源类型是**文档**而不是工作区。
    "office_doc_read": (UNIFIED_RESOURCE_READ, RESOURCE_OFFICE_DOCUMENT),
    "office_doc_analyze": (UNIFIED_RESOURCE_READ, RESOURCE_OFFICE_DOCUMENT),
    "read_document": (UNIFIED_RESOURCE_READ, RESOURCE_OFFICE_DOCUMENT),
    "inspect_document_set": (UNIFIED_RESOURCE_READ, RESOURCE_OFFICE_DOCUMENT),
    "office_doc_edit": (UNIFIED_RESOURCE_WRITE, RESOURCE_OFFICE_DOCUMENT),
    # 客户端原子工具（Claude-Code 风格的名字，桌面端直接广告）：
    # 它们就是"模型可见层"的既有实现，显式登记以免落到 unknown。
    "read": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "filestat": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "openfile": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "glob": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "grep": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "write": (UNIFIED_RESOURCE_WRITE, RESOURCE_WORKSPACE),
    "edit": (UNIFIED_RESOURCE_EDIT, RESOURCE_WORKSPACE),
    "notebookedit": (UNIFIED_RESOURCE_EDIT, RESOURCE_WORKSPACE),
    "rename": (UNIFIED_RESOURCE_MOVE, RESOURCE_WORKSPACE),
    "delete": (UNIFIED_RESOURCE_DELETE, RESOURCE_WORKSPACE),
    "bash": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "shell": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "bashoutput": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "killshell": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "python_exec": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "run_in_sandbox": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "sandbox_run": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    # 代码/工程类工具：动的都是工作区里的文件。
    "apply_patch": (UNIFIED_RESOURCE_EDIT, RESOURCE_WORKSPACE),
    "run_static_check": (UNIFIED_CODE_EXECUTE, RESOURCE_WORKSPACE),
    "check_new_dependencies": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    "install_new_dependencies": (UNIFIED_RESOURCE_WRITE, RESOURCE_WORKSPACE),
    "rollback_dependency_manifests": (UNIFIED_RESOURCE_EDIT, RESOURCE_WORKSPACE),
    "get_project_context": (UNIFIED_RESOURCE_READ, RESOURCE_WORKSPACE),
    # 知识库读取：这是知识资源目前**唯一**的真实读取入口（Provider 尚未独立注册）。
    "query_knowledge": (UNIFIED_RESOURCE_READ, RESOURCE_KNOWLEDGE),
    # 办公产物渲染：与 ``artifact.create`` 同资源类型。
    "create_office_document": (UNIFIED_ARTIFACT_CREATE, RESOURCE_ARTIFACT),
}


# ── Provider 声明 ───────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class ResourceProviderSpec:
    """一个**资源 Provider** 的声明（Phase 1：只声明，不参与路由）。"""

    name: str
    #: 物理 Provider（既有 `CapabilityRegistry` 里的 provider_id）。
    provider_id: str
    resource_types: tuple[str, ...]
    capabilities: tuple[str, ...]
    #: server / client / sandbox（与 `ToolCapability.environment` 同一套词表）。
    execution_env: str = "client"
    #: 是否已经有实现注册。``False`` = 只有声明（Phase 6 会补上真 Provider）。
    registered: bool = True
    note: str = ""

    def supports(self, capability: str, resource_type: str) -> bool:
        return capability in self.capabilities and resource_type in self.resource_types


#: Provider 候选的**有序**清单：同一 (能力, 资源类型) 命中多个时，靠前者优先。
#: Broker（Phase 3）按这个顺序选，而不是各自再写一遍 if。
RESOURCE_PROVIDERS: tuple[ResourceProviderSpec, ...] = (
    ResourceProviderSpec(
        name="workspace_provider",
        provider_id="lumi.local.workspace",
        resource_types=(RESOURCE_WORKSPACE,),
        capabilities=(
            UNIFIED_RESOURCE_READ,
            UNIFIED_RESOURCE_WRITE,
            UNIFIED_RESOURCE_EDIT,
            UNIFIED_RESOURCE_MOVE,
            UNIFIED_RESOURCE_DELETE,
        ),
        execution_env="client",
        note="本地工作区：数据不出本机，必须由客户端租约承载",
    ),
    ResourceProviderSpec(
        name="git_provider",
        provider_id="lumi.local.git",
        resource_types=(RESOURCE_WORKSPACE,),
        capabilities=(UNIFIED_RESOURCE_READ, UNIFIED_RESOURCE_WRITE),
        execution_env="client",
        note="git 域：diff 只读、commit/push 写；与 workspace_provider 并列候选",
    ),
    ResourceProviderSpec(
        name="code_provider",
        provider_id="lumi.local.code",
        # 代码执行的**数据**依赖客户端在线（工作区里的代码），因此资源类型仍是 workspace。
        resource_types=(RESOURCE_WORKSPACE,),
        capabilities=(UNIFIED_CODE_EXECUTE,),
        execution_env="sandbox",
        note="沙箱执行（方案里的 sandbox.run 与此同义）",
    ),
    ResourceProviderSpec(
        name="office_document_provider",
        provider_id="lumi.client.forwarder",
        resource_types=(RESOURCE_OFFICE_DOCUMENT,),
        capabilities=(
            UNIFIED_RESOURCE_READ,
            UNIFIED_RESOURCE_WRITE,
            UNIFIED_RESOURCE_EDIT,
        ),
        execution_env="client",
        note="办公文档内容在用户设备上，编辑必须经客户端",
    ),
    ResourceProviderSpec(
        name="artifact_provider",
        provider_id="lumi.server.artifact",
        resource_types=(RESOURCE_ARTIFACT,),
        capabilities=(UNIFIED_ARTIFACT_CREATE,),
        execution_env="server",
        note="服务端渲染产物：客户端离线也能产出",
    ),
    ResourceProviderSpec(
        name="knowledge_provider",
        provider_id="",
        resource_types=(RESOURCE_KNOWLEDGE,),
        capabilities=(UNIFIED_RESOURCE_READ, UNIFIED_RESOURCE_WRITE),
        execution_env="server",
        registered=False,
        note="**只有声明**：知识库能力尚未 Provider 化，Phase 6 验收会补实现",
    ),
    ResourceProviderSpec(
        name="memory_provider",
        provider_id="lumi.server.memory",
        resource_types=(RESOURCE_MEMORY,),
        capabilities=(UNIFIED_RESOURCE_READ, UNIFIED_RESOURCE_WRITE),
        execution_env="server",
        note="任务内工作记忆（服务端 Redis）：不依赖客户端在线，也不需要工作区租约",
    ),
)

PROVIDERS_BY_NAME: dict[str, ResourceProviderSpec] = {item.name: item for item in RESOURCE_PROVIDERS}

#: 资源类型 → 默认 Provider（候选清单里的第一个）。只用于展示与兜底，
#: 真正的选择在 Broker。
DEFAULT_PROVIDER_BY_RESOURCE: dict[str, str] = {}
for _spec in RESOURCE_PROVIDERS:
    for _rtype in _spec.resource_types:
        DEFAULT_PROVIDER_BY_RESOURCE.setdefault(_rtype, _spec.name)
del _spec, _rtype

#: **刻意不接入**资源能力层的工具族（Phase 1 的边界，写下来而不是"忘了"）：
#:
#: * UI/编排类（``AskUserQuestion`` / ``EnterPlanMode`` / ``Task`` / ``TodoWrite`` /
#:   ``Skill`` / ``SlashCommand`` / ``Calculator`` …）：它们不操作"资源"，
#:   是编排与交互原语；硬给一个资源类型只会让 Broker 多一条无意义的候选；
#: * 外部服务类（``send_email`` / ``calendar_manager`` / ``web_search`` / ``web_fetch`` /
#:   ``curl``）：资源在**外部**，需要 ``external_service`` / ``web`` 这类资源类型——
#:   那是 Phase 2+ 的显式决定（新增资源类型要连带 Provider 与审批语义），
#:   不在"给现有工具补元数据"的范围里偷偷做掉。
#:
#: 记忆类（``task_memory``）原本也在这里，**Phase 6 已把它接入**
#: （``memory_provider`` + ``resource_type=memory``，工具自己声明能力与资源类型）。
DEFERRED_TOOL_FAMILIES: dict[str, tuple[str, ...]] = {
    "ui_orchestration": (
        "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "Task", "TodoWrite", "todo_manager",
        "Skill", "SlashCommand", "Calculator", "DateTime", "SystemInfo",
        "OpenApp", "OpenUrl", "desktop_open_app", "desktop_open_url",
        "ProcessList", "ProcessSignal", "collect_results", "extract_code_blocks",
        "user_clarify",
    ),
    "external_service": (
        "send_email", "calendar_manager", "web_search", "web_fetch", "curl",
    ),
}


# ── 兼容映射查询 ────────────────────────────────────────────

#: 三态可见性（**注册 / 可见 / 可用**）+ 两个"不可用"的细分。
#: ``unregistered`` 与 ``unavailable`` 必须分开：前者是"系统没有这个东西"，
#: 后者是"有，但此刻调不到"——混在一起会让模型以为工具不存在而去编替代做法。
STATE_UNREGISTERED = "unregistered"
STATE_REGISTERED = "registered"
STATE_VISIBLE = "visible"
STATE_AVAILABLE = "available"
STATE_UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ResourceBinding:
    """一个工具在**统一资源能力层**里的身份。"""

    tool: str
    capability: str = ""
    resource_type: str = ""
    provider: str = ""
    provider_id: str = ""
    #: 兼容层的旧能力名（``workspace.read`` 这类；客户端协议冻结在它上面）。
    legacy_capability: str = ""
    #: 绑定来源：``tool`` / ``capability`` / ``provider`` / ``unknown``（排障用）。
    source: str = "unknown"

    @property
    def known(self) -> bool:
        return bool(self.capability and self.resource_type)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "capability": self.capability,
            "resource_type": self.resource_type,
            "provider": self.provider,
            "provider_id": self.provider_id,
            "legacy_capability": self.legacy_capability,
            "source": self.source,
            "known": self.known,
        }


def normalize_unified_capability(name: str) -> str:
    """别名 → 规范统一能力名；不认识的原样返回（调用方自己决定怎么处理）。"""
    text = str(name or "").strip()
    if not text:
        return ""
    return UNIFIED_CAPABILITY_ALIASES.get(text, text)


def is_unified_capability(name: str) -> bool:
    return normalize_unified_capability(name) in UNIFIED_CAPABILITIES


def _static_legacy_capability(tool_name: str) -> str:
    """**纯静态**的工具 → 旧能力（与 ``capability_for_tool`` 关闭开关时同源）。

    刻意不调 ``tool_registry``：本模块要能被注册表在**条目构造**里调用，
    反向依赖会形成递归（``条目 → 绑定 → 能力 → 条目``）。
    """
    requested = str(tool_name or "").strip()
    if not requested:
        return ""
    try:
        from app.agents.capabilities.builtin import TOOL_CAPABILITY_MAP
    except Exception:  # noqa: BLE001
        return ""
    for candidate in (requested, requested.split("__")[-1], requested.rsplit(".", 1)[-1]):
        if candidate in TOOL_CAPABILITY_MAP:
            return str(TOOL_CAPABILITY_MAP[candidate] or "")
        for known, capability in TOOL_CAPABILITY_MAP.items():
            if known.casefold() == candidate.casefold():
                return str(capability or "")
    try:
        from app.agents.capabilities.catalog import IMPLEMENTATION_MAP

        return str(IMPLEMENTATION_MAP.get(requested) or "")
    except Exception:  # noqa: BLE001
        return ""


def _tool_key(tool_name: str) -> str:
    """工具名归一化：去掉 ``mcp__server__`` 前缀与命名空间，统一小写用于查表。"""
    return str(tool_name or "").strip().split("__")[-1].rsplit(".", 1)[-1].casefold()


def _declared_binding(tool_name: str) -> tuple[str, str] | None:
    """工具**自述**的 ``(统一能力, 资源类型)``；读不到返回 ``None``。

    只读 ``ToolRegistry`` 里的工具类属性，不查注册表条目（本函数会被条目构造调用，
    反向依赖会形成递归）。静态表优先由调用方保证。
    """
    try:
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get(_tool_key(tool_name))
    except Exception:  # noqa: BLE001
        return None
    if tool is None:
        return None
    raw_capability = str(getattr(tool, "capability", "") or "").strip()
    raw_resource = str(getattr(tool, "resource_type", "") or "").strip().casefold()
    if not raw_capability or not raw_resource:
        return None
    # 能力声明可以是旧名（workspace.read）也可以是统一名（resource.read）
    unified = normalize_unified_capability(raw_capability.split("@", 1)[0])
    if unified not in UNIFIED_CAPABILITIES:
        mapped = LEGACY_CAPABILITY_BINDINGS.get(raw_capability.split("@", 1)[0])
        if mapped is None:
            return None
        unified = mapped[0]
    if raw_resource not in RESOURCE_TYPES:
        return None
    return unified, raw_resource


def providers_for(capability: str, resource_type: str) -> tuple[ResourceProviderSpec, ...]:
    """(能力, 资源类型) → **有序候选 Provider**（空 = 没有 Provider 能承接）。"""
    unified = normalize_unified_capability(capability)
    if not unified or not resource_type:
        return ()
    return tuple(
        spec for spec in RESOURCE_PROVIDERS if spec.supports(unified, resource_type)
    )


def binding_for_tool(
    tool_name: str,
    *,
    legacy_capability: str = "",
    resource_type: str = "",
) -> ResourceBinding:
    """工具 → 统一资源绑定（**唯一**入口；不认识就返回 ``unknown``，绝不猜）。

    ``legacy_capability`` / ``resource_type`` 由调用方显式传入时优先：
    注册表在条目构造里已经算出了（可能是**插件声明的**）能力名，
    这里再查一遍静态表就会把声明丢掉。
    """
    requested = str(tool_name or "").strip()
    if not requested:
        return ResourceBinding(tool="")
    key = _tool_key(requested)

    # 1) 工具级例外（最具体，优先于能力级）。
    tool_binding = TOOL_BINDINGS.get(key)
    if tool_binding is not None:
        unified, rtype = tool_binding

    # 2) 显式传入的能力（注册表派生 / 插件声明）。
    else:
        capability = str(legacy_capability or "").strip()
        if capability and resource_type and capability in UNIFIED_CAPABILITIES:
            # 调用方直接给了统一能力（新注册的工具声明用统一名）
            unified, rtype = capability, str(resource_type)
        else:
            # 静态表优先：认识的工具不能被声明改写归属（路由/租约/审批都建立在表上）。
            legacy = capability or _static_legacy_capability(requested)
            mapped = LEGACY_CAPABILITY_BINDINGS.get(str(legacy).split("@", 1)[0])
            if mapped is not None:
                unified, rtype = mapped
                if resource_type:
                    rtype = str(resource_type)
            else:
                # 3) 静态表**不认识**这个工具时才读它的自述声明（插件新工具走这条）。
                declared = _declared_binding(requested)
                if declared is None:
                    return ResourceBinding(
                        tool=requested, legacy_capability=legacy, source="unknown"
                    )
                unified, rtype = declared
                if resource_type:
                    rtype = str(resource_type)

    spec = providers_for(unified, rtype)
    return ResourceBinding(
        tool=requested,
        capability=unified,
        resource_type=rtype,
        provider=spec[0].name if spec else "",
        provider_id=spec[0].provider_id if spec else "",
        legacy_capability=str(legacy_capability or _static_legacy_capability(requested)),
        source="tool" if tool_binding is not None else "capability",
    )


def resource_visibility(tool_name: str, *, capability: Any = None) -> str:
    """工具在当前上下文的**三态**：``registered`` / ``visible`` / ``available``。

    * 没有绑定的工具 → ``unregistered``（**不复用** eligible：把"系统不认识"读成
      "允许使用"是最危险的一类误读）；
    * 有绑定但这一轮不在候选池里 → ``registered``；
    * 在候选池里 → 复用 ``mandatory_tools.visibility_state`` 的三态结果：
      eligible → ``visible``，available → ``available``，unavailable → ``unavailable``。
    """
    binding = binding_for_tool(tool_name)
    if not binding.known:
        return STATE_UNREGISTERED
    if capability is None:
        return STATE_REGISTERED
    try:
        from app.agents.skills.mandatory_tools import (
            STATE_AVAILABLE,
            STATE_CATALOG,
            STATE_ELIGIBLE,
            STATE_UNAVAILABLE,
            visibility_state,
        )

        state = str(visibility_state(capability) or "")
    except Exception:  # noqa: BLE001 - 可见性探测失败不能谎报"可用"
        return STATE_REGISTERED
    if state == STATE_AVAILABLE:
        return STATE_AVAILABLE
    if state == STATE_UNAVAILABLE:
        return STATE_UNAVAILABLE
    if state in {STATE_ELIGIBLE, STATE_CATALOG}:
        return STATE_VISIBLE
    return STATE_REGISTERED


def catalog_snapshot() -> dict[str, Any]:
    """目录快照（管理端与测试用；纯数据，不含用户输入）。"""
    return {
        "unified_capabilities": sorted(UNIFIED_CAPABILITIES),
        "aliases": dict(UNIFIED_CAPABILITY_ALIASES),
        "resource_types": sorted(RESOURCE_TYPES),
        "legacy_bindings": {
            key: {"capability": value[0], "resource_type": value[1]}
            for key, value in sorted(LEGACY_CAPABILITY_BINDINGS.items())
        },
        "tool_bindings": {
            key: {"capability": value[0], "resource_type": value[1]}
            for key, value in sorted(TOOL_BINDINGS.items())
        },
        "providers": [
            {
                "name": spec.name,
                "provider_id": spec.provider_id,
                "resource_types": list(spec.resource_types),
                "capabilities": list(spec.capabilities),
                "execution_env": spec.execution_env,
                "registered": spec.registered,
                "note": spec.note,
            }
            for spec in RESOURCE_PROVIDERS
        ],
        "default_provider_by_resource": dict(DEFAULT_PROVIDER_BY_RESOURCE),
    }


def unbound_tools(tool_names: Iterable[str]) -> list[str]:
    """还没接入资源能力层的工具（排障/影子用：**先看得见，再决定怎么办**）。"""
    unknown: list[str] = []
    for name in tool_names:
        text = str(name or "")
        if not text:
            continue
        if not binding_for_tool(text).known:
            unknown.append(text)
    return sorted(set(unknown))


#: 未接入工具的**名字归一化集合**（用于"边界是被验证的，而不是愿望"）。
DEFERRED_TOOL_KEYS: frozenset[str] = frozenset(
    name.casefold() for names in DEFERRED_TOOL_FAMILIES.values() for name in names
)


def deferred_family_of(tool_name: str) -> str:
    """未接入工具属于哪个"刻意推迟"的族；不在任何族里说明**漏登记了**。"""
    key = _tool_key(tool_name)
    for family, names in DEFERRED_TOOL_FAMILIES.items():
        if key in {name.casefold() for name in names}:
            return family
    return ""


def log_binding_gaps(tool_names: Iterable[str]) -> list[str]:
    """把未绑定的工具记一条日志（只记录、不改行为）。"""
    unknown = unbound_tools(tool_names)
    if unknown:
        logger.info(
            "[resource-catalog] 尚未接入资源能力层的工具 {} 个: {}",
            len(unknown),
            ", ".join(unknown[:8]) + ("…" if len(unknown) > 8 else ""),
        )
    return unknown


__all__ = [
    "DEFAULT_PROVIDER_BY_RESOURCE",
    "LEGACY_CAPABILITY_BINDINGS",
    "PROVIDERS_BY_NAME",
    "RESOURCE_ARTIFACT",
    "RESOURCE_KNOWLEDGE",
    "RESOURCE_OFFICE_DOCUMENT",
    "RESOURCE_PROVIDERS",
    "RESOURCE_TYPES",
    "RESOURCE_WORKSPACE",
    "ResourceBinding",
    "ResourceProviderSpec",
    "STATE_AVAILABLE",
    "STATE_REGISTERED",
    "STATE_UNAVAILABLE",
    "STATE_UNREGISTERED",
    "STATE_VISIBLE",
    "TOOL_BINDINGS",
    "UNIFIED_ARTIFACT_CREATE",
    "UNIFIED_CAPABILITIES",
    "UNIFIED_CAPABILITY_ALIASES",
    "UNIFIED_CODE_EXECUTE",
    "UNIFIED_RESOURCE_DELETE",
    "UNIFIED_RESOURCE_EDIT",
    "UNIFIED_RESOURCE_MOVE",
    "UNIFIED_RESOURCE_READ",
    "UNIFIED_RESOURCE_WRITE",
    "binding_for_tool",
    "catalog_snapshot",
    "deferred_family_of",
    "DEFERRED_TOOL_FAMILIES",
    "DEFERRED_TOOL_KEYS",
    "is_unified_capability",
    "log_binding_gaps",
    "normalize_unified_capability",
    "providers_for",
    "resource_visibility",
    "unbound_tools",
]
