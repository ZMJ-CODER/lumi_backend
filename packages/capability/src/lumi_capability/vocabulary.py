"""能力词表：统一能力名、别名与资源类型（**纯数据 + 纯函数**，backend-neutral）。

从 ``app/agents/capabilities/catalog/resource.py`` 抽出来（结构重构 P3 第一批）。
这一段是"协议"，不是"业务"：

* **统一能力**是模型与编排唯一认的一层（``resource.read/write/edit/move/delete``、
  ``code.execute``、``artifact.create``）；
* **资源类型**描述"这件事作用在什么东西上"（工作区/办公文档/知识/产物/记忆）；
* **别名**把方案里出现过、但本质是同一件事的写法收敛掉（``sandbox.run`` 就是
  ``code.execute``，``resource.search`` 就是读取）。

刻意**不在这里**的东西：旧能力名到统一能力的绑定表、工具绑定表、Provider 声明——
那些是应用侧配置（"这个仓库里有哪些工具、挂在哪个 Provider 上"），
`app/agents/capabilities/catalog/resource.py` 仍然持有它们。
"""

from __future__ import annotations

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
#: 任务内工作记忆（服务端）：新增资源类型**不该**要求改任何静态映射表。
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


# ── 归一 ────────────────────────────────────────────────────


def normalize_unified_capability(name: str) -> str:
    """别名 → 规范统一能力名；不认识的原样返回（调用方自己决定怎么处理）。"""
    text = str(name or "").strip()
    if not text:
        return ""
    return UNIFIED_CAPABILITY_ALIASES.get(text, text)


def is_unified_capability(name: str) -> bool:
    """是不是**规范**统一能力名（别名也算，未归一的不算）。"""
    return normalize_unified_capability(name) in UNIFIED_CAPABILITIES


__all__ = [
    "RESOURCE_ARTIFACT",
    "RESOURCE_KNOWLEDGE",
    "RESOURCE_MEMORY",
    "RESOURCE_OFFICE_DOCUMENT",
    "RESOURCE_TYPES",
    "RESOURCE_WORKSPACE",
    "UNIFIED_ARTIFACT_CREATE",
    "UNIFIED_CAPABILITIES",
    "UNIFIED_CAPABILITY_ALIASES",
    "UNIFIED_CODE_EXECUTE",
    "UNIFIED_RESOURCE_DELETE",
    "UNIFIED_RESOURCE_EDIT",
    "UNIFIED_RESOURCE_MOVE",
    "UNIFIED_RESOURCE_READ",
    "UNIFIED_RESOURCE_WRITE",
    "is_unified_capability",
    "normalize_unified_capability",
]
