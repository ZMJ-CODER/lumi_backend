"""统一工作区操作契约：状态、上下文、版本规则与保护策略（**纯契约**）。

为什么要有这一层：写入/编辑/移动/删除原先各自用布尔 ``success`` + 自由文本错误表达结果，
于是"等待审批""被拒绝""内容没变""本来就不存在"全都被压成"失败"或"成功"，调用方只能
靠字符串猜。这里把四件事固定下来：

1. :class:`OperationStatus`：统一状态词表（success / no_change / pending_approval /
   denied / failed / already_absent）——``error`` 只表达错误，不承担状态表达；
2. :class:`OperationContext`：由**服务端**注入的调用上下文（身份、工作区、设备、租约、
   幂等键、审批令牌），Skill/DAG/模型一律不能自己传；
3. :class:`RevisionRules`：**唯一**的版本算法（内容哈希 + 元信息），write/edit/move/delete
   四个工具必须用同一套，否则 write 返回的版本没法给 edit/delete/move 用；
4. :class:`ProtectionPolicy` / :class:`TrashPolicy`：动态保护路径、回收站布局与配额。

本模块不 import ``app.*``（只依赖标准库 + pydantic），因此可以被 contracts/services/agents
任意一侧引用而不产生环。
"""

from __future__ import annotations

import hashlib
import posixpath
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ── 状态词表 ──────────────────────────────────────────────────────


class OperationKind(StrEnum):
    """一次工作区操作的类型（审计与事件都以它为准）。"""

    WRITE = "write"
    EDIT = "edit"
    MOVE = "move"
    DELETE = "delete"
    RESTORE = "restore"
    PURGE = "purge"
    LIST_TRASH = "list_trash"


class OperationStatus(StrEnum):
    """操作结果状态（前端只渲染，不自己归类）。

    * ``SUCCESS``：真的改动了工作区；
    * ``NO_CHANGE``：内容与目标状态已经一致（写同样内容、edit 的 old==new），**不算失败**，
      也不触发审批确认流程；
    * ``PENDING_APPROVAL``：已准备就绪，等本机/服务端审批；
    * ``DENIED``：被策略或用户拒绝（不是"执行失败"）；
    * ``FAILED``：真的失败了，看 ``error.code``；
    * ``ALREADY_ABSENT``：删除目标本来就不存在（幂等成功态，不是错误）。
    """

    SUCCESS = "success"
    NO_CHANGE = "no_change"
    PENDING_APPROVAL = "pending_approval"
    DENIED = "denied"
    FAILED = "failed"
    ALREADY_ABSENT = "already_absent"

    @property
    def is_ok(self) -> bool:
        """是否属于"调用方目标已达成"的终态（no_change / already_absent 也算）。"""
        return self in {
            OperationStatus.SUCCESS,
            OperationStatus.NO_CHANGE,
            OperationStatus.ALREADY_ABSENT,
        }

    @property
    def is_terminal(self) -> bool:
        return self is not OperationStatus.PENDING_APPROVAL

    @classmethod
    def coerce(
        cls, value: object, *, default: "OperationStatus | None" = None
    ) -> "OperationStatus":
        text = str(value or "").strip().casefold()
        for item in cls:
            if item.value == text:
                return item
        return default or cls.FAILED


class OperationApprovalState(StrEnum):
    """审批状态（与 ``error`` 分离：拒绝不是错误，是决定）。"""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    INVALID = "invalid"


class RollbackState(StrEnum):
    """可回滚性（前端据此决定是否给"撤销"入口）。"""

    NOT_APPLICABLE = "not_applicable"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_NEEDED = "not_needed"


# ── 版本规则（唯一算法）──────────────────────────────────────────
class RevisionRules:
    """文件/目录版本标识的**唯一**生成与匹配规则。

    文件：``sha256:<16 位十六进制内容哈希>:<字节数>``。

    * 内容哈希保证"内容变了版本必变"（并发修改一定被检出）；
    * 字节数是**文件元信息**的一部分（截断/追加但哈希恰好碰撞时也能区分，且便于排查）；
    * 不引入 mtime：服务端拿不到可信 mtime，写进版本号只会制造"看起来变了其实没变"的噪声。

    目录：``dir1:<16 位摘要>:<条目数>``，摘要是排序后的
    ``名字/类型/大小/子版本`` 列表的哈希——**不递归计算整份目录内容**（移动目录时
    重新哈希整棵树既慢又没必要，见 move 契约）。
    """

    FILE_PREFIX = "sha256"
    DIR_PREFIX = "dir1"
    HASH_CHARS = 16

    @classmethod
    def content_hash(cls, content: str | bytes) -> str:
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return hashlib.sha256(raw).hexdigest()[: cls.HASH_CHARS]

    @classmethod
    def for_file(cls, content: str | bytes, *, size: int | None = None) -> str:
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        length = len(raw) if size is None else int(size)
        return f"{cls.FILE_PREFIX}:{cls.content_hash(raw)}:{length}"

    @classmethod
    def for_directory(cls, entries: list[dict[str, Any]]) -> str:
        """目录项摘要（条目形如 ``{name, kind, size, revision}``）。"""
        rows = sorted(
            "/".join(
                [
                    str(item.get("name") or ""),
                    str(item.get("kind") or ""),
                    str(int(item.get("size") or 0)),
                    str(item.get("revision") or ""),
                ]
            )
            for item in entries
            if isinstance(item, dict)
        )
        digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[: cls.HASH_CHARS]
        return f"{cls.DIR_PREFIX}:{digest}:{len(rows)}"

    @classmethod
    def matches(cls, expected: str, actual: str) -> bool:
        """``expected`` 是否是 ``actual`` 的同一次版本。

        接受三种写法（避免调用方因为多粘贴/少粘贴一段就误判冲突）：

        * 完整版本 ``sha256:<hash>:<size>``；
        * 裸哈希 ``<hash>``；
        * 裸哈希前缀（>= 8 位）。
        """
        want = str(expected or "").strip()
        have = str(actual or "").strip()
        if not want:
            return True
        if want == have:
            return True
        wanted_hash = cls.hash_of(want)
        have_hash = cls.hash_of(have)
        if not wanted_hash or not have_hash:
            return False
        if len(wanted_hash) >= 8 and have_hash.startswith(wanted_hash):
            return True
        return len(wanted_hash) >= 8 and wanted_hash.startswith(have_hash)

    @classmethod
    def hash_of(cls, revision: str) -> str:
        text = str(revision or "").strip()
        if not text:
            return ""
        parts = text.split(":")
        if len(parts) >= 2 and parts[0] in {cls.FILE_PREFIX, cls.DIR_PREFIX}:
            return parts[1]
        return text if all(ch in "0123456789abcdef" for ch in text.casefold()) else ""


# ── 保护策略与配额 ────────────────────────────────────────────────
class ProtectionPolicy(BaseModel):
    """动态保护路径与阈值（可由策略包覆盖，缺省保守）。"""

    model_config = ConfigDict(frozen=True)

    #: 精确匹配的保护目录/文件名（工作区根相对路径，任意层级）。
    protected_names: tuple[str, ...] = (
        ".git", ".gitignore", ".gitattributes", ".lumi", ".lumi_trash",
    )
    #: 后缀保护（凭据/密钥类）。
    protected_suffixes: tuple[str, ...] = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
    #: 名字里包含这些片段即保护（大小写不敏感）。
    protected_fragments: tuple[str, ...] = ("id_rsa", "id_ed25519", "credentials", "secret", "token")
    #: 目录移动/删除递归时的条目阈值：超过即要求显式审批。
    approval_file_threshold: int = 20
    approval_bytes_threshold: int = 2 * 1024 * 1024
    #: 单次写入内容上限（与客户端 2MB 限额一致）。
    max_write_bytes: int = 2 * 1024 * 1024

    def is_protected(self, rel_path: str) -> tuple[bool, str]:
        """返回 ``(是否受保护, 原因)``。"""
        rel = normalize_rel_path(rel_path)
        if not rel:
            return False, ""
        protected = {name.casefold() for name in self.protected_names}
        for part in rel.split("/"):
            lowered = part.casefold()
            if lowered in protected:
                return True, f"受保护路径：{part}"
            for suffix in self.protected_suffixes:
                if lowered.endswith(suffix):
                    return True, f"凭据/密钥类文件受保护：{part}"
            for fragment in self.protected_fragments:
                if fragment in lowered:
                    return True, f"名称包含敏感片段（{fragment}）：{part}"
        return False, ""


class TrashPolicy(BaseModel):
    """回收站保留期与空间配额（默认值与客户端 ``CAPABILITY_BRIDGE.md`` §11.4 一致：7 天 / 500 条）。"""

    model_config = ConfigDict(frozen=True)

    retention_days: int = 7
    max_items: int = 500
    max_bytes: int = 200 * 1024 * 1024
    #: 恢复/清理是"始终确认"档操作，需要显式审批（客户端 IPC 也如此）。
    restore_requires_approval: bool = True


class OperationLimits(BaseModel):
    """操作网关的大小/时间边界（防止一次操作把内容灌爆）。"""

    model_config = ConfigDict(frozen=True)

    #: 送进版本计算/编辑匹配的最大字节数（超过要求改用 edit 的区间模式或分片）。
    max_read_bytes: int = 4 * 1024 * 1024
    #: 一次操作内允许受影响的文件数（目录递归删除/移动的上界）。
    max_affected_files: int = 2000
    #: 单次客户端调用超时（秒）。
    call_timeout_seconds: float = 60.0


# ── 路径工具（不允许出现绝对路径/越界）────────────────────────────
def normalize_rel_path(value: Any) -> str:
    """把用户/模型给的路径收敛成工作区内的相对 POSIX 路径（不合法时返回空串）。"""
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("//") or (len(text) >= 2 and text[1] == ":"):
        return ""
    text = text.lstrip("/")
    text = posixpath.normpath(text)
    if text in {".", ".."} or text.startswith("../") or "/../" in text:
        return ""
    return "" if text.startswith("/") else text


def parent_of(rel_path: str) -> str:
    rel = normalize_rel_path(rel_path)
    if not rel or "/" not in rel:
        return ""
    return posixpath.dirname(rel)


def is_within(path: str, parent: str) -> bool:
    """``path`` 是否就是 ``parent`` 或在其内部（都按工作区相对路径比较）。"""
    child = normalize_rel_path(path)
    root = normalize_rel_path(parent)
    if not child:
        return False
    if not root:
        return True
    return child == root or child.startswith(f"{root}/")


TRASH_DIRNAME = ".lumi_trash"
#: 索引文件名（客户端写的；后端读取并按需重写，保证两边看到同一份记录）。
TRASH_INDEX_NAME = "trash.json"


def is_trash_path(rel_path: str) -> bool:
    """是否是回收站内部路径（普通工作区操作与读取一律不得触碰）。"""
    rel = normalize_rel_path(rel_path)
    if not rel:
        return False
    return rel == TRASH_DIRNAME or rel.startswith(f"{TRASH_DIRNAME}/")


def trash_entry_id(kind: str, rel_path: str, *, stamp: str = "") -> str:
    """回收站条目标识：类型 + 路径 + 时间戳的短哈希（稳定、可读、无路径泄漏风险）。"""
    seed = f"{kind}:{normalize_rel_path(rel_path)}:{stamp}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def trash_entry_path(entry_id: str) -> str:
    """回收站条目的内容落点：``.lumi_trash/<trash_id>``（客户端 rename 的目标）。"""
    return f"{TRASH_DIRNAME}/{str(entry_id or '').strip()}"


# ── 调用上下文（服务端注入）──────────────────────────────────────
class OperationContext(BaseModel):
    """一次操作调用的上下文（**只能由服务端构造**）。

    身份字段一律来自服务端授权事实；不提供任何 ``from_arguments()`` 之类的入口，
    避免 Skill/模型自报 workspace/device 越权操作别的目录。
    """

    model_config = ConfigDict(frozen=True)

    operation_id: str = ""
    request_id: str = ""
    trace_id: str = ""
    conversation_id: str = ""
    job_id: str = ""
    node_id: str = ""
    step_id: str = ""
    user_id: str = ""
    user_role: str = "user"
    workspace_id: str = ""
    device_id: str = ""
    lease_id: str = ""
    provider_id: str = ""
    idempotency_key: str = ""
    #: 审批令牌的**指纹**（服务端校验过的事实，不是模型自报的 approved=true）。
    approval_token: str = ""
    approval_state: OperationApprovalState = OperationApprovalState.NOT_REQUIRED
    #: 已通过审批的调用指纹集合（服务端审批服务写入）。
    confirmed_tool_calls: frozenset[str] = frozenset()
    #: 客户端上报的工作区版本（单调），随写操作回传作为 base_version。
    workspace_version: int = 0
    dry_run: bool = False
    limits: OperationLimits = Field(default_factory=OperationLimits)
    protection: ProtectionPolicy = Field(default_factory=ProtectionPolicy)
    trash: TrashPolicy = Field(default_factory=TrashPolicy)

    @property
    def has_approval(self) -> bool:
        return self.approval_state is OperationApprovalState.APPROVED or bool(self.approval_token)

    def child(self, **overrides: Any) -> "OperationContext":
        return self.model_copy(update=dict(overrides))

    @classmethod
    def from_source(
        cls,
        source: Any = None,
        *,
        operation_id: str = "",
        step_id: str = "",
        idempotency_key: str = "",
        approval_token: str = "",
        approval_state: OperationApprovalState | str = OperationApprovalState.NOT_REQUIRED,
        dry_run: bool = False,
        workspace_version: int = 0,
        lease_id: str = "",
        provider_id: str = "",
        **extra: Any,
    ) -> "OperationContext":
        """从服务端上下文（``AgentExecutionContext`` / 执行节点上下文 / None）构造。

        用鸭子类型读取字段，**不** import ``app.agents.*``，避免契约层依赖执行层。
        """
        src = source
        binding = getattr(src, "binding", None)
        metadata = getattr(src, "metadata", None)
        metadata = metadata if isinstance(metadata, dict) else {}
        data = {
            "operation_id": str(operation_id or metadata.get("operation_id") or ""),
            "request_id": str(getattr(src, "request_id", "") or metadata.get("request_id") or ""),
            "trace_id": str(getattr(src, "trace_id", "") or metadata.get("trace_id") or ""),
            "conversation_id": str(getattr(src, "conversation_id", "") or ""),
            "job_id": str(getattr(src, "job_id", "") or metadata.get("job_id") or ""),
            "node_id": str(getattr(src, "node_id", "") or metadata.get("node_id") or ""),
            "step_id": str(step_id or metadata.get("step_id") or ""),
            "user_id": str(getattr(src, "user_id", "") or ""),
            "user_role": str(getattr(src, "user_role", "") or "user"),
            "workspace_id": str(getattr(src, "workspace_id", "") or ""),
            "device_id": str(getattr(src, "device_id", "") or ""),
            "lease_id": str(lease_id or metadata.get("lease_id") or ""),
            "provider_id": str(provider_id or metadata.get("provider_id") or ""),
            "idempotency_key": str(idempotency_key or metadata.get("idempotency_key") or ""),
            "approval_token": str(approval_token or ""),
            "approval_state": approval_state or OperationApprovalState.NOT_REQUIRED,
            "workspace_version": int(workspace_version or 0),
            "dry_run": bool(dry_run),
        }
        if binding is not None:
            data["workspace_id"] = data["workspace_id"] or str(getattr(binding, "workspace_id", "") or "")
            data["device_id"] = data["device_id"] or str(getattr(binding, "device_id", "") or "")
            data["conversation_id"] = data["conversation_id"] or str(
                getattr(binding, "conversation_id", "") or ""
            )
        confirmed = getattr(src, "confirmed_tool_calls", None)
        if confirmed:
            data["confirmed_tool_calls"] = frozenset(str(item) for item in confirmed)
        data.update({key: value for key, value in extra.items() if value is not None})
        return cls(**data)


@dataclass(slots=True)
class ChangeSummary:
    """一次操作对工作区造成的变化摘要（不含正文）。"""

    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    moved: list[dict[str, str]] = field(default_factory=list)
    created_dirs: list[str] = field(default_factory=list)
    added_lines: int = 0
    removed_lines: int = 0
    bytes_written: int = 0

    @property
    def affected_files(self) -> list[str]:
        rows = [*self.created, *self.modified, *self.deleted]
        rows.extend(str(item.get("to") or "") for item in self.moved)
        return [item for item in dict.fromkeys(rows) if item]

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "moved": [dict(item) for item in self.moved],
            "created_dirs": list(self.created_dirs),
            "added_lines": int(self.added_lines),
            "removed_lines": int(self.removed_lines),
            "bytes_written": int(self.bytes_written),
        }


__all__ = [
    "ChangeSummary",
    "OperationApprovalState",
    "OperationContext",
    "OperationKind",
    "OperationLimits",
    "OperationStatus",
    "ProtectionPolicy",
    "RevisionRules",
    "RollbackState",
    "TRASH_DIRNAME",
    "TRASH_INDEX_NAME",
    "TrashPolicy",
    "is_trash_path",
    "is_within",
    "normalize_rel_path",
    "parent_of",
    "trash_entry_id",
    "trash_entry_path",
]
