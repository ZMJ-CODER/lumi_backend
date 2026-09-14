"""阶段 1：内置能力目录（现有能力的 Provider 化声明）。

第一批**不新增任何能力**，只把已经在跑的东西声明成能力：能力名 + 契约版本 + 输入/
输出 Schema + 数据本地性 + 副作用 + 稳定错误码。三件事因此确定下来：

* 模型侧仍然只看到少量聚合工具（``workspace_navigator`` 等），**不会**因为 Provider
  化而把每个能力都暴露给模型；
* 每个能力在哪一侧执行（``data_locality``）是**声明**，不是运行时猜测；
* Provider 注册时提交的 ``CapabilityDescriptor`` 与目录里的声明必须完全一致
  （见 ``CapabilityRegistry.assert_catalog_consistent``），防止"注册时把本地能力
  悄悄说成云能力"。

目录里的映射（现有实现 → 能力）::

    workspace_navigator / workspace_reader  → workspace.read@1
    Electron MCP workspace_stage_*          → workspace.write@1
    本地沙箱 / Electron sandbox_*            → code.execute@1
    GitSkill(plugins/tools/devtools/git.py) → git.operations@1
    create_office_document                  → artifact.create@1
"""

from __future__ import annotations

from typing import Any

from lumi_contracts.plugins import (
    CapabilityDescriptor,
    DataLocality,
    SideEffectKind,
)

# ── 能力名常量（调用方不要写字符串字面量）──────────────────────────
# ── [配置加载] 能力名常量与实现映射表
CAPABILITY_WORKSPACE_READ = "workspace.read"
CAPABILITY_WORKSPACE_WRITE = "workspace.write"
CAPABILITY_WORKSPACE_EDIT = "workspace.edit"
CAPABILITY_WORKSPACE_MOVE = "workspace.move"
CAPABILITY_WORKSPACE_DELETE = "workspace.delete"
CAPABILITY_CODE_EXECUTE = "code.execute"
CAPABILITY_CODE_SCAN = "code.scan"
CAPABILITY_GIT_OPERATIONS = "git.operations"
CAPABILITY_ARTIFACT_CREATE = "artifact.create"

#: 四个工作区修改类能力（统一走 OperationResult + 版本校验 + 审批）。
WORKSPACE_OPERATION_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_WORKSPACE_WRITE,
    CAPABILITY_WORKSPACE_EDIT,
    CAPABILITY_WORKSPACE_MOVE,
    CAPABILITY_WORKSPACE_DELETE,
)

#: 现有实现 → 能力的映射（文档化 + 测试断言用）。
IMPLEMENTATION_MAP: dict[str, str] = {
    "workspace_navigator": CAPABILITY_WORKSPACE_READ,
    "workspace_reader": CAPABILITY_WORKSPACE_READ,
    "workspace_code_scan": CAPABILITY_CODE_SCAN,
    "workspace_write": CAPABILITY_WORKSPACE_WRITE,
    "workspace_stage_write": CAPABILITY_WORKSPACE_WRITE,
    "workspace_stage_delete": CAPABILITY_WORKSPACE_WRITE,
    "workspace_commit": CAPABILITY_WORKSPACE_WRITE,
    "workspace_rollback": CAPABILITY_WORKSPACE_WRITE,
    "workspace_edit": CAPABILITY_WORKSPACE_EDIT,
    "code_edit": CAPABILITY_WORKSPACE_EDIT,
    "workspace_move": CAPABILITY_WORKSPACE_MOVE,
    "workspace_delete": CAPABILITY_WORKSPACE_DELETE,
    "python_exec": CAPABILITY_CODE_EXECUTE,
    "run_in_sandbox": CAPABILITY_CODE_EXECUTE,
    "sandbox_run": CAPABILITY_CODE_EXECUTE,
    "sandbox_reset": CAPABILITY_CODE_EXECUTE,
    "sandbox_commit": CAPABILITY_CODE_EXECUTE,
    "git": CAPABILITY_GIT_OPERATIONS,
    "create_office_document": CAPABILITY_ARTIFACT_CREATE,
}

# ── [配置加载] 输入/输出 JSON schema（校验用，纯数据）
_READ_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # scan 也归在工作区读取域里（它读的是同一个本地文件，只是给骨架不给正文）。
        "action": {"type": "string", "enum": ["list", "search", "read", "scan"]},
        "path": {"type": "string"},
        "query": {"type": "string"},
        "cursor": {"type": "string"},
        "max_chars": {"type": "integer"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
    },
    "required": ["action"],
}

_READ_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "action": {"type": "string"},
        "summary": {"type": "string"},
        "data": {"type": "object"},
        "has_more": {"type": "boolean"},
        "cursor": {"type": "string"},
    },
    "required": ["status"],
}

#: ``code.scan``：给路径就够；其余都是可选收窄（按类型过滤、按行区间、按名字查找）。
_SCAN_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "kind": {"type": "string", "enum": ["class", "function", "method", "import"]},
        "find": {"type": "string"},
        "start_line": {"type": "integer"},
        "end_line": {"type": "integer"},
        "max_symbols": {"type": "integer"},
        "include_imports": {"type": "boolean"},
    },
    "required": ["path"],
}

_SCAN_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "summary": {"type": "string"},
        "data": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "language": {"type": "string"},
                "symbols": {"type": "array"},
                "imports": {"type": "array"},
                "stats": {"type": "object"},
                "partial": {"type": "boolean"},
                "truncated": {"type": "boolean"},
                "found": {"type": "object"},
            },
        },
    },
    "required": ["status"],
}

_WRITE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["stage_write", "stage_delete", "commit", "rollback"],
        },
        "path": {"type": "string"},
        "content": {"type": "string"},
        #: 覆盖已有文件时必须与当前版本一致（唯一算法见 app/workspace/write/revision.py）。
        "expected_revision": {"type": "string"},
        #: 空内容默认拒绝；确需清空文件时显式打开。
        "allow_empty": {"type": "boolean"},
        #: 自动创建父目录（创建的目录会记进 created_dirs）。
        "create_parents": {"type": "boolean"},
        #: 换行风格：preserve（沿用已有文件，默认）/ lf / crlf。
        "newline": {"type": "string", "enum": ["preserve", "lf", "crlf"]},
        #: 只预览不落盘（结果 dry_run=true，状态 no_change）。
        "dry_run": {"type": "boolean"},
        "base_version": {"type": "integer"},
        "idempotency_key": {"type": "string"},
    },
    "required": ["operation"],
}

_EDIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "old_str": {"type": "string"},
        "new_str": {"type": "string"},
        "expected_revision": {"type": "string"},
        #: 0 = 必须唯一匹配（默认）；N>0 = 只替换第 N 处。
        "occurrence": {"type": "integer"},
        "replace_all": {"type": "boolean"},
        "normalize_newlines": {"type": "boolean"},
        "dry_run": {"type": "boolean"},
    },
    "required": ["path", "old_str", "new_str"],
}

_MOVE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_path": {"type": "string"},
        "target_path": {"type": "string"},
        #: 文件=内容版本；目录=目录快照版本（dir1:...）。
        "expected_revision": {"type": "string"},
        "overwrite": {"type": "boolean"},
        "create_parents": {"type": "boolean"},
        "dry_run": {"type": "boolean"},
    },
    "required": ["source_path", "target_path"],
}

_DELETE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        #: 默认 False：非空目录必须显式递归（且目录删除当前需客户端支持）。
        "recursive": {"type": "boolean"},
        #: 默认 False = 移入回收站（可恢复）；True = 永久删除（高危审批）。
        "permanent": {"type": "boolean"},
        "expected_revision": {"type": "string"},
        "dry_run": {"type": "boolean"},
    },
    "required": ["path"],
}

#: 四个操作能力共用的输出 Schema（OperationResult 的统一形状）。
_OPERATION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {"type": "string"},
        "status": {
            "type": "string",
            "enum": [
                "success",
                "no_change",
                "pending_approval",
                "denied",
                "failed",
                "already_absent",
            ],
        },
        "logical_path": {"type": "string"},
        "target_path": {"type": "string"},
        "revision": {"type": "string"},
        "old_revision": {"type": "string"},
        "new_revision": {"type": "string"},
        "workspace_version": {"type": "integer"},
        "approval_state": {"type": "string"},
        "rollback_available": {"type": "boolean"},
        "changed_files": {"type": "array", "items": {"type": "string"}},
        "changes": {"type": "object"},
        "stats": {"type": "object"},
        "dry_run": {"type": "boolean"},
        "error": {"type": "object"},
    },
    "required": ["operation", "status"],
}

_CODE_EXECUTE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "language": {"type": "string"},
        "code": {"type": "string"},
        "command": {"type": "string"},
        "cwd": {"type": "string"},
        "timeout_seconds": {"type": "number"},
        "project_id": {"type": "string"},
    },
}

_GIT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["status", "diff", "commit"]},
        "cwd": {"type": "string"},
        "message": {"type": "string"},
        "project_id": {"type": "string"},
    },
    "required": ["action"],
}

_ARTIFACT_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string"},
        "title": {"type": "string"},
        "blocks": {"type": "array", "items": {"type": "object"}},
        "output_name": {"type": "string"},
    },
    "required": ["kind"],
}

#: 第一批内置能力（顺序即展示顺序）。
# ── [配置加载] 内置能力描述符（数据本地性/副作用/权限/契约版本）
_BUILTIN_DESCRIPTORS: tuple[CapabilityDescriptor, ...] = (
    CapabilityDescriptor(
        name=CAPABILITY_WORKSPACE_READ,
        contract_version=1,
        summary="读取用户工作区（目录/定位/正文，分页与分块由服务端处理）",
        input_schema=_READ_INPUT_SCHEMA,
        output_schema=_READ_OUTPUT_SCHEMA,
        side_effects=[SideEffectKind.READ],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["workspace.read"],
        streamable=True,
        artifact_when_large=True,
        error_codes=[
            "WORKSPACE_NOT_BOUND",
            "WORKSPACE_DEVICE_OFFLINE",
            "PATH_OUTSIDE_WORKSPACE",
            "INVALID_ACTION",
            "CONTENT_TOO_LARGE",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_CODE_SCAN,
        contract_version=1,
        summary="扫描代码骨架（类/函数/方法/导入 + 行号区间），不返回函数体",
        input_schema=_SCAN_INPUT_SCHEMA,
        output_schema=_SCAN_OUTPUT_SCHEMA,
        # 只读：与 workspace.read 同级，不需要审批；本地性同源（代码不出本机）。
        #
        # 执行位置：**服务端聚合**。客户端只把原文交给 workspace_read，骨架由
        # ``app/knowledge/code/code_structure.py``（纯函数）解析；客户端不实现第二套扫描，
        # 因此客户端只登记描述、不广告 ``code.scan@1`` 租约（见客户端 CAPABILITY_BRIDGE.md §2.1）。
        # 没有租约时按只读能力回退到 ``workspace_navigator(action=scan)`` 的同一实现。
        side_effects=[SideEffectKind.READ],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["workspace.read"],
        streamable=False,
        artifact_when_large=True,
        error_codes=[
            "WORKSPACE_NOT_BOUND",
            "WORKSPACE_DEVICE_OFFLINE",
            "WORKSPACE_PATH_NOT_FOUND",
            "WORKSPACE_UNSUPPORTED_FORMAT",
            "INVALID_PARAMS",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_WORKSPACE_WRITE,
        contract_version=1,
        summary="写入用户工作区（版本校验 → 暂存/提交 → 原子落盘由客户端执行，幂等 + 审批）",
        input_schema=_WRITE_INPUT_SCHEMA,
        output_schema=_OPERATION_OUTPUT_SCHEMA,
        side_effects=[SideEffectKind.WRITE],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["workspace.write"],
        # 本地写盘必须由用户在本机再确认一次（服务端授权不能替用户扩大本机权限）。
        needs_local_confirmation=True,
        error_codes=[
            "WORKSPACE_NOT_BOUND",
            "WORKSPACE_DEVICE_OFFLINE",
            "PATH_OUTSIDE_WORKSPACE",
            "PROTECTED_PATH",
            "EMPTY_CONTENT_REJECTED",
            "CONTENT_TOO_LARGE",
            "REVISION_REQUIRED",
            "REVISION_MISMATCH",
            "APPROVAL_REQUIRED",
            "WRITE_FAILED",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_WORKSPACE_EDIT,
        contract_version=1,
        summary="按 old_str→new_str 严格匹配编辑（默认唯一匹配，复用写入的原子提交）",
        input_schema=_EDIT_INPUT_SCHEMA,
        output_schema=_OPERATION_OUTPUT_SCHEMA,
        side_effects=[SideEffectKind.WRITE],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["workspace.write"],
        needs_local_confirmation=True,
        error_codes=[
            "WORKSPACE_NOT_BOUND",
            "WORKSPACE_PATH_NOT_FOUND",
            "REVISION_REQUIRED",
            "REVISION_MISMATCH",
            "OLD_TEXT_NOT_FOUND",
            "OLD_TEXT_NOT_UNIQUE",
            "PROTECTED_PATH",
            "APPROVAL_REQUIRED",
            "EDIT_FAILED",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_WORKSPACE_MOVE,
        contract_version=1,
        summary="移动/重命名（同一文件系统内单次 rename：文件与目录都原子；跨设备一律拒绝）",
        input_schema=_MOVE_INPUT_SCHEMA,
        output_schema=_OPERATION_OUTPUT_SCHEMA,
        side_effects=[SideEffectKind.WRITE, SideEffectKind.DELETE],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["workspace.write"],
        needs_local_confirmation=True,
        error_codes=[
            "WORKSPACE_NOT_BOUND",
            "WORKSPACE_PATH_NOT_FOUND",
            "ALREADY_EXISTS",
            "TARGET_INSIDE_SOURCE",
            "REVISION_MISMATCH",
            "CROSS_DEVICE_UNSUPPORTED",
            "NOT_SUPPORTED_BY_PROVIDER",
            "APPROVAL_REQUIRED",
            "MOVE_FAILED",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_WORKSPACE_DELETE,
        contract_version=1,
        summary="删除到工作区回收站（rename 原子移入、可恢复、含目录/二进制）；永久删除走高危审批",
        input_schema=_DELETE_INPUT_SCHEMA,
        output_schema=_OPERATION_OUTPUT_SCHEMA,
        side_effects=[SideEffectKind.DELETE],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["workspace.write"],
        needs_local_confirmation=True,
        error_codes=[
            "WORKSPACE_NOT_BOUND",
            "PROTECTED_PATH",
            "NOT_EMPTY_DIRECTORY",
            "TOO_MANY_FILES",
            "TRASH_UNAVAILABLE",
            "TRASH_QUOTA_EXCEEDED",
            "TRASH_CONTENT_UNREADABLE",
            "NOT_SUPPORTED_BY_PROVIDER",
            "APPROVAL_REQUIRED",
            "DELETE_FAILED",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_CODE_EXECUTE,
        contract_version=1,
        summary="在隔离环境执行代码/命令（本地沙箱或用户设备）",
        input_schema=_CODE_EXECUTE_INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "exit_code": {"type": "integer"},
                "duration_ms": {"type": "integer"},
            },
        },
        side_effects=[SideEffectKind.EXECUTE],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["code.execute"],
        needs_local_confirmation=True,
        error_codes=[
            "SANDBOX_UNAVAILABLE",
            "SANDBOX_REJECTED",
            "EXECUTION_TIMEOUT",
            "PROJECT_NOT_AUTHORIZED",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_GIT_OPERATIONS,
        contract_version=1,
        summary="读取仓库状态或提交本地改动（只允许 status/diff/commit）",
        input_schema=_GIT_INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "changes": {"type": "array", "items": {"type": "object"}},
            },
        },
        # 高风险动作（reset --hard / clean / force push）**不在**本能力词表内。
        side_effects=[SideEffectKind.READ, SideEffectKind.EXECUTE],
        data_locality=DataLocality.LOCAL_ONLY,
        required_permissions=["git.operations"],
        needs_local_confirmation=True,
        error_codes=[
            "GIT_NOT_A_REPOSITORY",
            "PROJECT_NOT_AUTHORIZED",
            "GIT_COMMAND_FAILED",
        ],
    ),
    CapabilityDescriptor(
        name=CAPABILITY_ARTIFACT_CREATE,
        contract_version=1,
        summary="生成交付产物（办公文档等），返回产物引用而非正文",
        input_schema=_ARTIFACT_INPUT_SCHEMA,
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "object"}},
            },
        },
        side_effects=[SideEffectKind.WRITE],
        # 产物在服务端渲染并落到用户的产物目录，不需要客户端在线。
        data_locality=DataLocality.CLOUD,
        required_permissions=["artifact.create"],
        artifact_when_large=True,
        error_codes=["DOCUMENT_RENDER_FAILED", "OUTPUT_DIR_UNAVAILABLE"],
    ),
)

#: 服务端**可直接执行**的能力（客户端离线也能跑）：Provider 化后据此判断是否必须
#: 等客户端。``local_only`` 的能力一律不能落进这个集合。
# ── [纯决策] 服务端可执行能力集合
SERVER_EXECUTABLE_CAPABILITIES: frozenset[str] = frozenset(
    {CAPABILITY_ARTIFACT_CREATE}
)


# ── [纯决策] 不可变目录：查、校验、快照（Provider 注册必须与它一致）
class CapabilityCatalog:
    """内置能力目录（不可变；Provider 注册必须与它一致）。"""

    def __init__(self, descriptors: tuple[CapabilityDescriptor, ...] | None = None) -> None:
        items = descriptors if descriptors is not None else _BUILTIN_DESCRIPTORS
        self._by_name: dict[str, CapabilityDescriptor] = {}
        for item in items:
            if item.name in self._by_name:
                raise ValueError(f"能力重复声明：{item.name}")
            self._by_name[item.name] = item

    def names(self) -> tuple[str, ...]:
        return tuple(self._by_name)

    def all(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(self._by_name.values())

    def get(self, name: str, *, version: int | None = None) -> CapabilityDescriptor | None:
        base = str(name or "").split("@", 1)[0]
        descriptor = self._by_name.get(base)
        if descriptor is None:
            return None
        if version is not None and int(version) != descriptor.contract_version:
            return None
        return descriptor

    def require(self, name: str, *, version: int | None = None) -> CapabilityDescriptor:
        descriptor = self.get(name, version=version)
        if descriptor is None:
            raise KeyError(f"未声明的能力：{name}{'' if version is None else f'@{version}'}")
        return descriptor

    def descriptor_for_implementation(self, implementation: str) -> CapabilityDescriptor | None:
        """现有实现名 → 能力描述符（迁移期反查）。"""
        return self.get(IMPLEMENTATION_MAP.get(str(implementation or "").strip(), ""))

    def assert_catalog_consistent(self, descriptor: CapabilityDescriptor) -> None:
        """Provider 注册时校验：注册声明必须与目录声明一致。

        防止"注册时降级声明"——例如把 ``local_only`` 的读取说成 ``cloud`` 以便路由到
        服务端，或悄悄去掉审批要求。
        """
        declared = self.require(descriptor.name, version=descriptor.contract_version)
        problems: list[str] = []
        if declared.data_locality is not descriptor.data_locality:
            problems.append(
                f"data_locality 不一致：目录={declared.data_locality} 注册={descriptor.data_locality}"
            )
        declared_effects = {str(item) for item in declared.side_effects}
        registered_effects = {str(item) for item in descriptor.side_effects}
        if registered_effects != declared_effects:
            problems.append(
                f"side_effects 不一致：目录={sorted(declared_effects)} 注册={sorted(registered_effects)}"
            )
        if descriptor.needs_local_confirmation != declared.needs_local_confirmation:
            problems.append("needs_local_confirmation 不一致（本机确认要求不允许由注册方放宽）")
        if problems:
            raise ValueError(f"能力 {descriptor.qualified_name} 注册声明与目录不一致：" + "；".join(problems))

    def to_snapshot(self) -> list[dict[str, Any]]:
        return [item.to_snapshot() for item in self._by_name.values()]


#: 进程内共享的内置目录（只读）。
# ── [纯决策] 进程内单例（只读）
capability_catalog = CapabilityCatalog()


__all__ = [
    "CAPABILITY_ARTIFACT_CREATE",
    "CAPABILITY_CODE_EXECUTE",
    "CAPABILITY_CODE_SCAN",
    "CAPABILITY_GIT_OPERATIONS",
    "CAPABILITY_WORKSPACE_DELETE",
    "CAPABILITY_WORKSPACE_EDIT",
    "CAPABILITY_WORKSPACE_MOVE",
    "CAPABILITY_WORKSPACE_READ",
    "CAPABILITY_WORKSPACE_WRITE",
    "CapabilityCatalog",
    "IMPLEMENTATION_MAP",
    "SERVER_EXECUTABLE_CAPABILITIES",
    "WORKSPACE_OPERATION_CAPABILITIES",
    "capability_catalog",
]
