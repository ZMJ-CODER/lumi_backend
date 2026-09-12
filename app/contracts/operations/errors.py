"""操作错误码：稳定码 + 重试性 + 是否需审批 + 安全的下一步建议（**纯契约**）。

规则（与方案"统一错误和审计"一致）：

* 错误码是跨模块契约的一部分，**新增必须在本模块登记**，不允许随手拼字符串；
* ``error`` 只表达错误：``no_change`` / ``already_absent`` / ``pending_approval`` /
  ``denied`` 都是**状态**（见 :class:`OperationStatus`），不要伪装成错误码；
* 每个码都自带"能不能重试""要不要审批""下一步干什么"，这样模型与前端都不必猜；
* 错误详情（``details``）不得放文件正文、密钥或绝对路径。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OperationErrorCode(StrEnum):
    """工作区操作错误码（服务端与客户端 Provider 共用）。"""

    # ── 参数与路径 ──
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    PATH_OUTSIDE_WORKSPACE = "PATH_OUTSIDE_WORKSPACE"
    PROTECTED_PATH = "PROTECTED_PATH"
    TRASH_PATH_FORBIDDEN = "TRASH_PATH_FORBIDDEN"
    REVISION_REQUIRED = "REVISION_REQUIRED"
    REVISION_MISMATCH = "REVISION_MISMATCH"
    PATH_IS_DIRECTORY = "PATH_IS_DIRECTORY"
    PATH_NOT_DIRECTORY = "PATH_NOT_DIRECTORY"
    NOT_EMPTY_DIRECTORY = "NOT_EMPTY_DIRECTORY"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    TARGET_INSIDE_SOURCE = "TARGET_INSIDE_SOURCE"
    EMPTY_CONTENT_REJECTED = "EMPTY_CONTENT_REJECTED"
    CONTENT_TOO_LARGE = "CONTENT_TOO_LARGE"
    TOO_MANY_FILES = "TOO_MANY_FILES"
    OLD_TEXT_NOT_FOUND = "OLD_TEXT_NOT_FOUND"
    OLD_TEXT_NOT_UNIQUE = "OLD_TEXT_NOT_UNIQUE"

    # ── 工作区/设备可用性 ──
    WORKSPACE_NOT_BOUND = "WORKSPACE_NOT_BOUND"
    WORKSPACE_NOT_REGISTERED = "WORKSPACE_NOT_REGISTERED"
    WORKSPACE_DEVICE_OFFLINE = "WORKSPACE_DEVICE_OFFLINE"
    WORKSPACE_PATH_NOT_FOUND = "WORKSPACE_PATH_NOT_FOUND"
    WORKSPACE_READ_FAILED = "WORKSPACE_READ_FAILED"

    # ── 审批与授权 ──
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_INVALID = "APPROVAL_INVALID"
    DENIED_BY_USER = "DENIED_BY_USER"
    DENIED_BY_POLICY = "DENIED_BY_POLICY"
    PERMISSION_DENIED = "PERMISSION_DENIED"

    # ── 执行失败 ──
    WRITE_FAILED = "WRITE_FAILED"
    EDIT_FAILED = "EDIT_FAILED"
    MOVE_FAILED = "MOVE_FAILED"
    DELETE_FAILED = "DELETE_FAILED"
    CROSS_DEVICE_UNSUPPORTED = "CROSS_DEVICE_UNSUPPORTED"
    NOT_SUPPORTED_BY_PROVIDER = "NOT_SUPPORTED_BY_PROVIDER"
    PROVIDER_OFFLINE = "PROVIDER_OFFLINE"
    TIMEOUT = "TIMEOUT"

    # ── 回收站 ──
    TRASH_UNAVAILABLE = "TRASH_UNAVAILABLE"
    TRASH_QUOTA_EXCEEDED = "TRASH_QUOTA_EXCEEDED"
    TRASH_ENTRY_NOT_FOUND = "TRASH_ENTRY_NOT_FOUND"
    TRASH_CONTENT_UNREADABLE = "TRASH_CONTENT_UNREADABLE"


@dataclass(frozen=True, slots=True)
class OperationErrorSpec:
    """错误码的元信息：重试性 / 是否需审批 / 安全的下一步建议。"""

    retryable: bool = False
    requires_approval: bool = False
    safe_next_action: str = ""


#: 错误码 → 元信息（未登记的码按"不可重试、无需审批、无建议"处理，但会记日志提醒）。
_SPECS: dict[str, OperationErrorSpec] = {
    OperationErrorCode.INVALID_ARGUMENTS.value: OperationErrorSpec(
        safe_next_action="按工具 schema 修正参数后重试一次。"
    ),
    OperationErrorCode.PATH_OUTSIDE_WORKSPACE.value: OperationErrorSpec(
        safe_next_action="只使用工作区内的相对路径（不接受绝对路径、盘符、.. 与符号链接逃逸）。"
    ),
    OperationErrorCode.PROTECTED_PATH.value: OperationErrorSpec(
        safe_next_action="该路径受保护（版本库元数据/凭据/回收站）；如确需修改请让用户手工处理。"
    ),
    OperationErrorCode.TRASH_PATH_FORBIDDEN.value: OperationErrorSpec(
        safe_next_action="回收站只能通过 restore/purge 接口访问，不要用 list/read/write 触碰 .lumi_trash。"
    ),
    OperationErrorCode.REVISION_REQUIRED.value: OperationErrorSpec(
        safe_next_action="先读取文件拿到 revision，再把 expected_revision 传进来。"
    ),
    OperationErrorCode.REVISION_MISMATCH.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="文件已被改动：重新读取拿到最新 revision 后重试（不要用过期上下文反复提交）。",
    ),
    OperationErrorCode.PATH_IS_DIRECTORY.value: OperationErrorSpec(
        safe_next_action="目标是目录：读取请用 list，删除/移动请显式声明目录语义。"
    ),
    OperationErrorCode.PATH_NOT_DIRECTORY.value: OperationErrorSpec(
        safe_next_action="目标不是目录，请改用文件语义。"
    ),
    OperationErrorCode.NOT_EMPTY_DIRECTORY.value: OperationErrorSpec(
        requires_approval=True,
        safe_next_action="目录非空：确认要递归处理时显式传 recursive=true（会走审批）。"
    ),
    OperationErrorCode.ALREADY_EXISTS.value: OperationErrorSpec(
        safe_next_action="目标已存在：改名、显式 overwrite=true，或先删除目标（需审批）。"
    ),
    OperationErrorCode.TARGET_INSIDE_SOURCE.value: OperationErrorSpec(
        safe_next_action="目标位于源目录内部：会造成循环，请换一个目标路径。"
    ),
    OperationErrorCode.EMPTY_CONTENT_REJECTED.value: OperationErrorSpec(
        safe_next_action="空内容被拒绝：确实要清空文件时显式传 allow_empty=true。"
    ),
    OperationErrorCode.CONTENT_TOO_LARGE.value: OperationErrorSpec(
        safe_next_action="内容超过单次写入上限：分片写入或改用 edit 精确替换。"
    ),
    OperationErrorCode.TOO_MANY_FILES.value: OperationErrorSpec(
        requires_approval=True,
        safe_next_action="影响文件数超过阈值：缩小范围，或确认后走审批执行。"
    ),
    OperationErrorCode.OLD_TEXT_NOT_FOUND.value: OperationErrorSpec(
        safe_next_action="old_str 未匹配：重新读取该文件确认当前内容（可能已被修改或换行风格不同）。"
    ),
    OperationErrorCode.OLD_TEXT_NOT_UNIQUE.value: OperationErrorSpec(
        safe_next_action="old_str 匹配多处：补充上下文让匹配唯一，或显式传 occurrence=第几处。"
    ),
    OperationErrorCode.WORKSPACE_NOT_BOUND.value: OperationErrorSpec(
        safe_next_action="当前会话没有绑定工作区：先在客户端选择/打开一个工作区。"
    ),
    OperationErrorCode.WORKSPACE_NOT_REGISTERED.value: OperationErrorSpec(
        safe_next_action="工作区未注册或没有可路由的桌面连接：请确认客户端已登录并连接。"
    ),
    OperationErrorCode.WORKSPACE_DEVICE_OFFLINE.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="托管该工作区的客户端当前离线：恢复连接后重试，不要改用服务端副本。"
    ),
    OperationErrorCode.WORKSPACE_PATH_NOT_FOUND.value: OperationErrorSpec(
        safe_next_action="路径不存在：先用 list/search 重新定位文件。"
    ),
    OperationErrorCode.WORKSPACE_READ_FAILED.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="读取失败：确认客户端在线后重试。"
    ),
    OperationErrorCode.APPROVAL_REQUIRED.value: OperationErrorSpec(
        retryable=True,
        requires_approval=True,
        safe_next_action="等待用户在本机确认后，用**同一个**幂等键重试本次调用。"
    ),
    OperationErrorCode.APPROVAL_INVALID.value: OperationErrorSpec(
        retryable=True,
        requires_approval=True,
        safe_next_action="审批令牌与本次调用指纹不一致（参数变了？）：重新发起审批。"
    ),
    OperationErrorCode.DENIED_BY_USER.value: OperationErrorSpec(
        safe_next_action="用户拒绝了本次操作：不要重试同一操作，先与用户确认新方案。"
    ),
    OperationErrorCode.DENIED_BY_POLICY.value: OperationErrorSpec(
        safe_next_action="被工作区策略拒绝：不要绕过策略，向用户说明原因。"
    ),
    OperationErrorCode.PERMISSION_DENIED.value: OperationErrorSpec(
        safe_next_action="当前身份无权执行该操作。"
    ),
    OperationErrorCode.WRITE_FAILED.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="写入未落盘：确认客户端在线后重试；已暂存内容不会重复提交。"
    ),
    OperationErrorCode.EDIT_FAILED.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="编辑未落盘：重新读取文件后重试。"
    ),
    OperationErrorCode.MOVE_FAILED.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="移动未完成：确认源/目标状态后重试（移动是幂等的，重复执行不会重复移动）。"
    ),
    OperationErrorCode.DELETE_FAILED.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="删除未完成：确认回收站可用后重试。"
    ),
    OperationErrorCode.CROSS_DEVICE_UNSUPPORTED.value: OperationErrorSpec(
        safe_next_action="跨设备移动不支持：请改为同一次工作区内的移动，或手动复制后校验再删除（不要自动复制+删除）。"
    ),
    OperationErrorCode.NOT_SUPPORTED_BY_PROVIDER.value: OperationErrorSpec(
        safe_next_action="当前客户端未提供该原子能力：升级客户端后再执行，不要改用近似操作蒙混。"
    ),
    OperationErrorCode.PROVIDER_OFFLINE.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="客户端 Provider 不在线：等待重连后用同一幂等键重试。"
    ),
    OperationErrorCode.TIMEOUT.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="客户端未在超时时间内返回：先用 read/list 确认实际状态，再用同一幂等键重试。"
    ),
    OperationErrorCode.TRASH_UNAVAILABLE.value: OperationErrorSpec(
        retryable=True,
        safe_next_action="回收站不可用（写入被拒绝或工作区只读）：不要报告删除成功。"
    ),
    OperationErrorCode.TRASH_QUOTA_EXCEEDED.value: OperationErrorSpec(
        requires_approval=True,
        safe_next_action="回收站超出保留期/配额：先清理回收站（purge）或改用手工删除并显式审批。"
    ),
    OperationErrorCode.TRASH_ENTRY_NOT_FOUND.value: OperationErrorSpec(
        safe_next_action="回收站里没有该条目：可能已被清理或恢复，先 list_trash 确认。"
    ),
    OperationErrorCode.TRASH_CONTENT_UNREADABLE.value: OperationErrorSpec(
        safe_next_action="该文件不是可读文本（二进制/解析失败）：回收站只接受可暂存内容，请改用客户端原子删除并显式审批。"
    ),
}

#: 操作错误码 → 能力错误码（跨边界时使用；能力词表是前端/插件共享的稳定集合）。
CAPABILITY_CODE_BY_OPERATION_ERROR: dict[str, str] = {
    OperationErrorCode.APPROVAL_REQUIRED.value: "APPROVAL_REQUIRED",
    OperationErrorCode.APPROVAL_INVALID.value: "APPROVAL_INVALID",
    OperationErrorCode.DENIED_BY_USER.value: "POLICY_DENIED",
    OperationErrorCode.DENIED_BY_POLICY.value: "POLICY_DENIED",
    OperationErrorCode.PROTECTED_PATH.value: "POLICY_DENIED",
    OperationErrorCode.PATH_OUTSIDE_WORKSPACE.value: "POLICY_DENIED",
    OperationErrorCode.TRASH_PATH_FORBIDDEN.value: "POLICY_DENIED",
    OperationErrorCode.PERMISSION_DENIED.value: "PERMISSION_DENIED",
    OperationErrorCode.WORKSPACE_NOT_BOUND.value: "WORKSPACE_NOT_BOUND",
    OperationErrorCode.WORKSPACE_NOT_REGISTERED.value: "WORKSPACE_NOT_REGISTERED",
    OperationErrorCode.WORKSPACE_DEVICE_OFFLINE.value: "WORKSPACE_DEVICE_OFFLINE",
    OperationErrorCode.WORKSPACE_PATH_NOT_FOUND.value: "WORKSPACE_PATH_NOT_FOUND",
    OperationErrorCode.PATH_IS_DIRECTORY.value: "WORKSPACE_PATH_NOT_DIRECTORY",
    OperationErrorCode.PATH_NOT_DIRECTORY.value: "WORKSPACE_PATH_NOT_DIRECTORY",
    OperationErrorCode.INVALID_ARGUMENTS.value: "INVALID_ARGUMENTS",
    OperationErrorCode.REVISION_REQUIRED.value: "INVALID_ARGUMENTS",
    OperationErrorCode.EMPTY_CONTENT_REJECTED.value: "INVALID_ARGUMENTS",
    OperationErrorCode.CONTENT_TOO_LARGE.value: "INVALID_ARGUMENTS",
    OperationErrorCode.ALREADY_EXISTS.value: "INVALID_ARGUMENTS",
    OperationErrorCode.TARGET_INSIDE_SOURCE.value: "INVALID_ARGUMENTS",
    OperationErrorCode.OLD_TEXT_NOT_FOUND.value: "INVALID_ARGUMENTS",
    OperationErrorCode.OLD_TEXT_NOT_UNIQUE.value: "INVALID_ARGUMENTS",
    OperationErrorCode.TOO_MANY_FILES.value: "APPROVAL_REQUIRED",
    OperationErrorCode.REVISION_MISMATCH.value: "FAILED",
    OperationErrorCode.CROSS_DEVICE_UNSUPPORTED.value: "CAPABILITY_UNAVAILABLE",
    OperationErrorCode.NOT_SUPPORTED_BY_PROVIDER.value: "CAPABILITY_UNAVAILABLE",
    OperationErrorCode.TRASH_UNAVAILABLE.value: "CAPABILITY_UNAVAILABLE",
    OperationErrorCode.TRASH_CONTENT_UNREADABLE.value: "CAPABILITY_UNAVAILABLE",
    OperationErrorCode.TRASH_QUOTA_EXCEEDED.value: "APPROVAL_REQUIRED",
    OperationErrorCode.PROVIDER_OFFLINE.value: "PROVIDER_OFFLINE",
    OperationErrorCode.TIMEOUT.value: "TIMEOUT",
}


class OperationError(BaseModel):
    """操作错误信封：稳定码 + 人类可读信息 + 重试/审批/下一步。"""

    model_config = ConfigDict(frozen=True)

    code: str
    message: str = ""
    retryable: bool = False
    requires_approval: bool = False
    safe_next_action: str = ""
    #: 结构化细节（字段名/路径/版本号）；**不得**放正文与凭据。
    details: dict[str, Any] = Field(default_factory=dict)

    @property
    def capability_code(self) -> str:
        return CAPABILITY_CODE_BY_OPERATION_ERROR.get(str(self.code), "FAILED")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


def error_spec(code: str) -> OperationErrorSpec:
    """取错误码元信息；未登记的码会记一条提示（防止"随手拼码"悄悄扩散）。"""
    text = str(code or "")
    spec = _SPECS.get(text)
    if spec is not None:
        return spec
    try:
        from loguru import logger

        logger.warning("[operation] 未登记的操作错误码（缺少重试/审批/建议元信息）: {}", text)
    except Exception:  # noqa: BLE001 - 未登记码不应影响主流程
        pass
    return OperationErrorSpec()


def operation_error(
    code: OperationErrorCode | str,
    message: str = "",
    *,
    details: dict[str, Any] | None = None,
    retryable: bool | None = None,
    requires_approval: bool | None = None,
    safe_next_action: str = "",
) -> OperationError:
    """按登记表构造错误信封（调用方只覆盖需要的字段）。"""
    spec = error_spec(str(code))
    return OperationError(
        code=str(code),
        message=str(message or str(code)),
        retryable=spec.retryable if retryable is None else bool(retryable),
        requires_approval=(
            spec.requires_approval if requires_approval is None else bool(requires_approval)
        ),
        safe_next_action=str(safe_next_action or spec.safe_next_action),
        details=dict(details or {}),
    )


def registered_codes() -> frozenset[str]:
    """已登记元信息的错误码（测试用：新增码必须登记）。"""
    return frozenset(_SPECS)


__all__ = [
    "CAPABILITY_CODE_BY_OPERATION_ERROR",
    "OperationError",
    "OperationErrorCode",
    "OperationErrorSpec",
    "error_spec",
    "operation_error",
    "registered_codes",
]
