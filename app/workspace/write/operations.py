"""工作区操作网关：``workspace.write / edit / move / delete``（+ 回收站 list/restore/purge）。

链路（方案第 2 条的落点）：

    Skill / DAG Node / 模型工具
      → （能力路径经 Capability Broker；旧路径经 executor 分支）
      → WorkspaceOperationService（本模块：上下文注入 → 策略 → 版本 → 审批 → 幂等）
      → 客户端原子工具（workspace_read / workspace_list / workspace_write /
                        workspace_move / workspace_delete / workspace_stage_* / workspace_commit）
      → OperationResult
      → ToolOutput 兼容投影（``OperationResult.to_tool_output``）

边界与实现事实（以客户端 ``CAPABILITY_BRIDGE.md`` §11.5 为准）：

* **服务端不直接操作客户端工作区**：所有落盘动作都经客户端原子工具；服务端只做
  校验、编排、版本、审批、记账与审计；
* **写入/编辑**：客户端提交路径是"同目录临时文件 + fsync + rename"的**原子替换**
  （``atomic_replace: true``）；服务端在写入前做版本校验，写入后读回校验并给出新版本。
  注意原子替换会换掉 inode，因此**权限位不保留**（结果里如实标注）；
* **移动**：客户端 ``workspace_move`` 是同一文件系统内的**单次 rename**（文件与目录都原子），
  跨设备返回 ``CROSS_DEVICE_UNSUPPORTED``（绝不"先复制再删除"）；目标已存在时客户端拒绝
  （``TARGET_EXISTS`` → ``ALREADY_EXISTS``，rename 不覆盖）；
* **删除**：客户端 ``workspace_delete`` 默认用 rename 把目标原子移入
  ``.lumi_trash/<trash_id>``（**不读内容** ⇒ 目录与二进制都能进回收站），并维护
  ``.lumi_trash/trash.json``；``permanent=true`` 才真删（且客户端始终要求本机确认）；
  非空目录必须显式 ``recursive``（``RECURSIVE_REQUIRED``）；
* **回收站**：客户端是主人。后端只**读**它的索引（``trash.json``），
  恢复用 ``workspace_move`` rename 回原路径（天然支持目录/二进制），
  清理用"暂存删除 + 重写索引 + 一次提交"（客户端把 ``.lumi_trash`` 视为受保护路径，
  不允许 ``workspace_delete`` 直接删它）。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

from loguru import logger

from app.contracts.operations import (
    TRASH_DIRNAME,
    TRASH_INDEX_NAME,
    DeleteRequest,
    DeleteStats,
    EditRequest,
    MoveRequest,
    OperationContext,
    OperationErrorCode,
    OperationKind,
    RevisionRules,
    RollbackState,
    WriteRequest,
    already_absent_result,
    denied_result,
    elapsed_ms,
    failed_result,
    is_trash_path,
    no_change_result,
    trash_entry_path,
)
from app.contracts.operations.results import (
    OperationResult,
    pending_approval_result,
    preview_result,
    success_result,
)
from app.workspace.write import revision as rev
from app.workspace.write.trash import (
    TrashIndex,
    guard_normal_operation,
    restore_update,
    summary_of,
)

#: 幂等结果缓存条数（进程内；跨 worker 需要 Redis，届时只换实现）。
IDEMPOTENCY_CACHE_SIZE = 128
#: 回收站内部读写路径（普通操作被 guard 拦下，回收站服务自己用）。
#: 文件名与客户端一致（``.lumi_trash/trash.json``）：两边读写同一份台账。
TRASH_INDEX_PATH = f"{TRASH_DIRNAME}/{TRASH_INDEX_NAME}"

#: 客户端 MCP 返回的失败码 → 操作错误码（未列出的折叠为具体工具的 *_FAILED）。
_CLIENT_ERROR_MAP: dict[str, str] = {
    "READ_BEFORE_WRITE_REQUIRED": OperationErrorCode.REVISION_REQUIRED.value,
    "WORKSPACE_VERSION_CONFLICT": OperationErrorCode.REVISION_MISMATCH.value,
    "REJECTED": OperationErrorCode.DENIED_BY_USER.value,
    "WORKSPACE_NOT_BOUND": OperationErrorCode.WORKSPACE_NOT_BOUND.value,
    "WORKSPACE_NOT_REGISTERED": OperationErrorCode.WORKSPACE_NOT_REGISTERED.value,
    "WORKSPACE_DEVICE_OFFLINE": OperationErrorCode.WORKSPACE_DEVICE_OFFLINE.value,
    "WORKSPACE_PATH_NOT_FOUND": OperationErrorCode.WORKSPACE_PATH_NOT_FOUND.value,
    "WORKSPACE_PATH_NOT_DIRECTORY": OperationErrorCode.PATH_NOT_DIRECTORY.value,
    "WORKSPACE_UNSUPPORTED_FORMAT": OperationErrorCode.TRASH_CONTENT_UNREADABLE.value,
    "TIMEOUT": OperationErrorCode.TIMEOUT.value,
    # 客户端原子工具的结构化结论（与 CAPABILITY_BRIDGE.md §11.5 一致）
    "TARGET_EXISTS": OperationErrorCode.ALREADY_EXISTS.value,
    "TARGET_IS_SOURCE_SUBDIR": OperationErrorCode.TARGET_INSIDE_SOURCE.value,
    "CROSS_DEVICE_UNSUPPORTED": OperationErrorCode.CROSS_DEVICE_UNSUPPORTED.value,
    "RECURSIVE_REQUIRED": OperationErrorCode.NOT_EMPTY_DIRECTORY.value,
    "TRASH_MOVE_FAILED": OperationErrorCode.TRASH_UNAVAILABLE.value,
    "PROTECTED_PATH": OperationErrorCode.PROTECTED_PATH.value,
    "NO_MATCH": OperationErrorCode.OLD_TEXT_NOT_FOUND.value,
    "AMBIGUOUS_MATCH": OperationErrorCode.OLD_TEXT_NOT_UNIQUE.value,
}

#: 已经是操作错误码的客户端码（原样透传，不折叠成工具级失败）。
_OPERATION_ERROR_CODES: frozenset[str] = frozenset(
    item.value for item in OperationErrorCode
)


# ── [运行时适配] 两种执行端：客户端 MCP / 导航器合成
class WorkspaceClient(Protocol):
    """操作网关需要的客户端原子能力（生产实现走桌面 MCP，测试用内存实现）。"""

    async def tool_names(self) -> set[str]: ...

    async def stat(self, path: str) -> dict[str, Any]: ...

    async def read_text(self, path: str) -> dict[str, Any]: ...

    async def list_dir(self, path: str) -> dict[str, Any]: ...

    async def write_text(self, path: str, content: str, *, call_id: str = "") -> dict[str, Any]: ...

    async def move(self, source: str, target: str) -> dict[str, Any]: ...

    async def delete(
        self, path: str, *, recursive: bool = False, permanent: bool = False, call_id: str = ""
    ) -> dict[str, Any]: ...

    async def stage_write(self, path: str, content: str) -> dict[str, Any]: ...

    async def stage_delete(self, path: str) -> dict[str, Any]: ...

    async def commit(
        self, *, base_version: int, idempotency_key: str, call_id: str = ""
    ) -> dict[str, Any]: ...


# ── 网关 ──────────────────────────────────────────────────────────
# ── [写] 操作网关：暂存 → 校验 → 提交（所有写路径的唯一入口）
class WorkspaceOperationService:
    """四个工作区操作 + 回收站恢复/清理的统一实现。"""

    def __init__(
        self,
        client: WorkspaceClient,
        *,
        context: OperationContext | None = None,
        publish_events: bool = True,
    ) -> None:
        self._client = client
        self._ctx = context or OperationContext()
        self._publish = bool(publish_events)
        self._idempotent: dict[str, tuple[float, OperationResult]] = {}

    # ── 对外入口 ──────────────────────────────────────────

    async def execute(
        self, kind: OperationKind | str, args: dict[str, Any]
    ) -> OperationResult:
        """统一入口（executor 分支与能力 Provider 都走这里）。"""
        wanted = OperationKind(str(kind))
        started = time.perf_counter()
        cached = self._idempotent_lookup(wanted, args)
        if cached is not None:
            return cached
        handler = {
            OperationKind.WRITE: self.write,
            OperationKind.EDIT: self.edit,
            OperationKind.MOVE: self.move,
            OperationKind.DELETE: self.delete,
        }[wanted]
        result = await handler(args)
        result = result.model_copy(update={"duration_ms": elapsed_ms(started)})
        await self._publish_result(result)
        self._idempotent_store(result)
        return result

    async def write(self, args: dict[str, Any]) -> OperationResult:
        request = WriteRequest.from_arguments(args)
        blocked = self._guard_path(request.path)
        if blocked is not None:
            return blocked
        content = request.content
        raw = content.encode("utf-8")
        if not content.strip() and not request.allow_empty:
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=request.path,
                code=OperationErrorCode.EMPTY_CONTENT_REJECTED.value,
                message="内容为空：确实要清空文件时请显式传 allow_empty=true",
            )
        if len(raw) > self._ctx.protection.max_write_bytes:
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=request.path,
                code=OperationErrorCode.CONTENT_TOO_LARGE.value,
                message=(
                    f"内容 {len(raw)} 字节超过单次写入上限 "
                    f"{self._ctx.protection.max_write_bytes} 字节"
                ),
            )
        if request.newline not in {"preserve", "lf", "crlf"}:
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=request.path,
                code=OperationErrorCode.INVALID_ARGUMENTS.value,
                message=f"newline 只能是 preserve/lf/crlf，收到 {request.newline}",
            )
        state = await self._read_state(request.path)
        if state.get("is_dir"):
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=request.path,
                code=OperationErrorCode.PATH_IS_DIRECTORY.value,
                message="目标是目录，不能作为文件写入",
            )
        exists = bool(state.get("exists"))
        old_text = str(state.get("text") or "")
        if exists and not state.get("text_readable", True):
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=request.path,
                code=OperationErrorCode.REVISION_REQUIRED.value,
                message="已有文件不是可读文本，无法做版本校验（拒绝盲写）",
                details={"binary": True},
            )
        new_text, newline_used = rev.apply_newline_policy(
            content, request.newline, existing=old_text
        )
        if exists:
            problem = rev.revision_problem(
                request.expected_revision, str(state.get("revision") or "")
            )
            if not request.expected_revision:
                problem = (
                    "覆盖已有文件必须带 expected_revision（先读取拿到版本），"
                    "避免盲写覆盖别人的修改"
                )
            if problem:
                return failed_result(
                    self._ctx,
                    kind=OperationKind.WRITE,
                    logical_path=request.path,
                    code=OperationErrorCode.REVISION_MISMATCH.value
                    if request.expected_revision
                    else OperationErrorCode.REVISION_REQUIRED.value,
                    message=problem,
                    details={"current_revision": str(state.get("revision") or "")},
                )
            if old_text == new_text:
                return no_change_result(
                    self._ctx,
                    kind=OperationKind.WRITE,
                    logical_path=request.path,
                    revision=str(state.get("revision") or ""),
                )
        added, removed = rev.line_change_stats(old_text, new_text)
        plan = {
            "files": 1,
            "dirs": 0,
            "bytes": len(new_text.encode("utf-8")),
            "created": not exists,
            "added_lines": added,
            "removed_lines": removed,
            "newline": newline_used,
        }
        if request.dry_run:
            return preview_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=request.path,
                old_revision=str(state.get("revision") or ""),
                plan=plan,
            )
        return await self._commit_content(
            kind=OperationKind.WRITE,
            path=request.path,
            new_text=new_text,
            old_text=old_text,
            exists=exists,
            base_version=int(state.get("workspace_version") or 0),
            plan=plan,
            newline=newline_used,
        )

    async def edit(self, args: dict[str, Any]) -> OperationResult:
        request = EditRequest.from_arguments(args)
        blocked = self._guard_path(request.path)
        if blocked is not None:
            return blocked
        if not request.old_str:
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.INVALID_ARGUMENTS.value,
                message="edit 必须提供 old_str（要替换的原文）",
            )
        if request.is_noop:
            return no_change_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
            )
        state = await self._read_state(request.path)
        if not state.get("exists"):
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.WORKSPACE_PATH_NOT_FOUND.value,
                message="要编辑的文件不存在",
            )
        if state.get("is_dir"):
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.PATH_IS_DIRECTORY.value,
                message="目标是目录，不能编辑",
            )
        if not state.get("text_readable", True):
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.REVISION_REQUIRED.value,
                message="该文件不是可读文本，无法做严格匹配替换",
                details={"binary": True},
            )
        old_text = str(state.get("text") or "")
        current_revision = str(state.get("revision") or "")
        if not request.expected_revision:
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.REVISION_REQUIRED.value,
                message="编辑必须带 expected_revision（先读取拿到版本），否则无法发现并发修改",
                details={"current_revision": current_revision},
            )
        problem = rev.revision_problem(request.expected_revision, current_revision)
        if problem:
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.REVISION_MISMATCH.value,
                message=problem,
                details={"current_revision": current_revision},
            )
        occurrences = rev.match_count(
            old_text, request.old_str, normalize_newlines=request.normalize_newlines
        )
        if occurrences == 0:
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.OLD_TEXT_NOT_FOUND.value,
                message="old_str 在文件中未找到（可能已被修改或换行风格不同）",
                details={"revision": current_revision},
            )
        if occurrences > 1 and not request.replace_all and request.occurrence <= 0:
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.OLD_TEXT_NOT_UNIQUE.value,
                message=f"old_str 匹配到 {occurrences} 处，替换全部会造成误改",
                details={"occurrences": occurrences, "revision": current_revision},
            )
        new_text, replaced = rev.replace_once(
            old_text,
            request.old_str,
            request.new_str,
            occurrence=request.occurrence,
            replace_all=request.replace_all,
            normalize_newlines=request.normalize_newlines,
        )
        if replaced == 0:
            return failed_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                code=OperationErrorCode.OLD_TEXT_NOT_FOUND.value,
                message=f"第 {request.occurrence} 处匹配不存在",
                details={"occurrences": occurrences},
            )
        if new_text == old_text:
            return no_change_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                revision=current_revision,
            )
        added, removed = rev.line_change_stats(old_text, new_text)
        plan = {
            "files": 1,
            "dirs": 0,
            "bytes": len(new_text.encode("utf-8")),
            "occurrences": replaced,
            "added_lines": added,
            "removed_lines": removed,
        }
        if request.dry_run:
            return preview_result(
                self._ctx,
                kind=OperationKind.EDIT,
                logical_path=request.path,
                old_revision=current_revision,
                plan=plan,
            )
        return await self._commit_content(
            kind=OperationKind.EDIT,
            path=request.path,
            new_text=new_text,
            old_text=old_text,
            exists=True,
            base_version=int(state.get("workspace_version") or 0),
            plan=plan,
        )

    async def move(self, args: dict[str, Any]) -> OperationResult:
        request = MoveRequest.from_arguments(args)
        for path in (request.source_path, request.target_path):
            blocked = self._guard_path(path)
            if blocked is not None:
                return blocked
        if not request.source_path or not request.target_path:
            return failed_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=OperationErrorCode.INVALID_ARGUMENTS.value,
                message="move 需要 source_path 与 target_path",
            )
        if request.source_path == request.target_path:
            return no_change_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                target_path=request.target_path,
            )
        if request.target_path.startswith(f"{request.source_path}/"):
            return failed_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=OperationErrorCode.TARGET_INSIDE_SOURCE.value,
                message="目标位于源目录内部（会形成循环）",
                details={"target_path": request.target_path},
            )
        source = await self._read_state(request.source_path)
        if not source.get("exists"):
            return already_absent_result(
                self._ctx, kind=OperationKind.MOVE, logical_path=request.source_path
            )
        is_dir = bool(source.get("is_dir"))
        if is_dir:
            problem = rev.revision_problem(
                request.expected_revision, str(source.get("directory_revision") or "")
            )
        else:
            problem = rev.revision_problem(
                request.expected_revision, str(source.get("revision") or "")
            )
        if problem:
            return failed_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=OperationErrorCode.REVISION_MISMATCH.value,
                message=problem,
                details={
                    "current_revision": str(
                        source.get("directory_revision") if is_dir else source.get("revision") or ""
                    )
                },
            )
        target = await self._read_state(request.target_path)
        if target.get("exists") and not request.overwrite:
            return failed_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=OperationErrorCode.ALREADY_EXISTS.value,
                message="目标已存在：改名，或先删除目标再移动",
                details={"target_path": request.target_path},
            )
        plan = {
            "files": int(source.get("file_count") or (0 if is_dir else 1)),
            "dirs": int(source.get("dir_count") or (1 if is_dir else 0)),
            "bytes": int(source.get("size") or 0),
            "overwrite": bool(target.get("exists")),
            "kind": "dir" if is_dir else "file",
        }
        if request.dry_run:
            return preview_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                target_path=request.target_path,
                old_revision=str(source.get("revision") or ""),
                plan=plan,
            )
        # 客户端 rename **不覆盖**：即使 overwrite=true 也会返回 TARGET_EXISTS，
        # 这里不自己做"删除再移动"（两步操作不是原子的），如实把结论交给调用方。
        return await self._execute_move(
            request=request,
            is_dir=is_dir,
            source_revision=str(
                (source.get("directory_revision") if is_dir else source.get("revision")) or ""
            ),
            overwrote=bool(target.get("exists")),
            plan=plan,
        )

    async def delete(self, args: dict[str, Any]) -> OperationResult:
        request = DeleteRequest.from_arguments(args)
        blocked = self._guard_path(request.path)
        if blocked is not None:
            return blocked
        state = await self._read_state(request.path)
        if not state.get("exists"):
            return already_absent_result(
                self._ctx, kind=OperationKind.DELETE, logical_path=request.path
            )
        is_dir = bool(state.get("is_dir"))
        if is_dir:
            problem = rev.revision_problem(
                request.expected_revision, str(state.get("directory_revision") or "")
            )
        else:
            problem = rev.revision_problem(
                request.expected_revision, str(state.get("revision") or "")
            )
        if problem:
            return failed_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                code=OperationErrorCode.REVISION_MISMATCH.value,
                message=problem,
                details={
                    "current_revision": str(
                        (state.get("directory_revision") if is_dir else state.get("revision")) or ""
                    )
                },
            )
        stats = {
            "files": int(state.get("file_count") or (0 if is_dir else 1)),
            "dirs": int(state.get("dir_count") or (1 if is_dir else 0)),
            "bytes": int(state.get("size") or 0),
            "recursive": bool(request.recursive),
            "permanent": bool(request.permanent),
            "to_trash": not request.permanent,
            "kind": "dir" if is_dir else "file",
        }
        if request.dry_run:
            return preview_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                old_revision=str(
                    (state.get("directory_revision") if is_dir else state.get("revision")) or ""
                ),
                plan=stats,
            )
        # 永久删除不可恢复：服务端先要一次审批（客户端还会再确认一次）。
        if request.permanent and not self._ctx.has_approval:
            return pending_approval_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                old_revision=str(state.get("revision") or ""),
                plan=stats,
                warnings=["永久删除不可恢复，必须显式审批"],
            )
        return await self._execute_delete(
            request=request,
            is_dir=is_dir,
            stats=stats,
            old_revision=str(
                (state.get("directory_revision") if is_dir else state.get("revision")) or ""
            ),
        )

    # ── 回收站接口 ────────────────────────────────────────

    async def list_trash(self, args: dict[str, Any] | None = None) -> OperationResult:
        policy = self._ctx.trash
        index, error = await self._load_index()
        if error is not None:
            return error
        assert index is not None
        plan = summary_of(index.records(), policy=policy)
        return success_result(
            self._ctx,
            kind=OperationKind.LIST_TRASH,
            logical_path=TRASH_INDEX_PATH,
            rollback_state=RollbackState.NOT_APPLICABLE,
            stats=plan,
        )

    async def restore(self, args: dict[str, Any]) -> OperationResult:
        """恢复：用客户端 ``workspace_move`` 把 ``.lumi_trash/<trash_id>`` rename 回原路径。

        用 rename 而不是"读内容再写回"：回收站里的条目可能是**目录或二进制**
        （客户端 rename 进回收站时不读内容），读文本再写回会损坏它们。
        """
        entry_id = str((args or {}).get("entry_id") or "").strip()
        index, error = await self._load_index()
        if error is not None:
            return error
        assert index is not None
        record = index.get(entry_id)
        if record is None:
            return failed_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=entry_id,
                code=OperationErrorCode.TRASH_ENTRY_NOT_FOUND.value,
                message=f"回收站里没有条目 {entry_id}",
            )
        if self._ctx.trash.restore_requires_approval and not self._ctx.has_approval:
            return pending_approval_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=record.logical_path,
                target_path=record.trash_path,
                old_revision=record.revision,
                plan={
                    "files": int(record.files or 0),
                    "dirs": int(record.dirs or 0),
                    "bytes": int(record.bytes or 0),
                },
                warnings=["恢复会在原路径重建内容（目标已存在时会被拒绝），需审批"],
            )
        source_state = await self._read_state(record.trash_path, allow_trash=True)
        if not source_state.get("exists"):
            return failed_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=record.logical_path,
                code=OperationErrorCode.TRASH_ENTRY_NOT_FOUND.value,
                message="回收站条目内容已不存在（可能已被清理或已恢复）",
                details={"trash_path": record.trash_path},
            )
        # 目标已存在 → 客户端 move 会以 TARGET_EXISTS 拒绝（不覆盖），这里如实映射。
        response = await self._client.move(record.trash_path, record.logical_path)
        verdict = self._verdict(response)
        if verdict == "pending":
            return pending_approval_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=record.logical_path,
                target_path=record.trash_path,
                old_revision=record.revision,
                plan={"files": int(record.files or 0), "dirs": int(record.dirs or 0)},
                warnings=["恢复已准备就绪，等待本机审批"],
            )
        if verdict == "rejected":
            return denied_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=record.logical_path,
                code=OperationErrorCode.DENIED_BY_USER.value,
                message="用户拒绝了本次恢复",
            )
        if verdict != "ok":
            return failed_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=record.logical_path,
                code=self._map_error(response, OperationErrorCode.MOVE_FAILED.value),
                message=str(response.get("error") or response.get("output") or "恢复未完成"),
                details={"trash_path": record.trash_path},
            )
        restored = await self._read_state(record.logical_path)
        if not restored.get("exists"):
            return failed_result(
                self._ctx,
                kind=OperationKind.RESTORE,
                logical_path=record.logical_path,
                code=OperationErrorCode.MOVE_FAILED.value,
                message="恢复后读回校验不一致（原路径仍不存在），不报告成功",
            )
        # 台账：客户端索引是权威，这里把条目标记为已恢复（失败只警告，内容已回来）。
        index.update(record.entry_id, **restore_update(record))
        index_written = await self._write_index(index)
        warnings = [] if index_written else ["回收站索引更新失败：工作区面板可能仍显示该条目"]
        return success_result(
            self._ctx,
            kind=OperationKind.RESTORE,
            logical_path=record.logical_path,
            old_revision=record.revision,
            new_revision=str(
                (restored.get("directory_revision") if record.is_dir else restored.get("revision"))
                or ""
            ),
            rollback_state=RollbackState.AVAILABLE,
            stats={
                "files": int(record.files or (0 if record.is_dir else 1)),
                "dirs": int(record.dirs or (1 if record.is_dir else 0)),
                "bytes": int(record.bytes or 0),
                "entry_id": record.entry_id,
            },
            warnings=warnings,
        )

    async def purge(self, args: dict[str, Any]) -> OperationResult:
        """清理回收站：指定 entry_ids 或清理已过期条目（永久删除，需审批）。"""
        payload = dict(args or {})
        policy = self._ctx.trash
        index, error = await self._load_index()
        if error is not None:
            return error
        assert index is not None
        wanted = [str(item) for item in (payload.get("entry_ids") or []) if str(item).strip()]
        targets = (
            [index.get(item) for item in wanted]
            if wanted
            else [
                item
                for item in index.records()
                if item.entry_id in set(summary_of(index.records(), policy=policy)["expired"])
            ]
        )
        records = [item for item in targets if item is not None]
        if not records:
            return no_change_result(
                self._ctx,
                kind=OperationKind.PURGE,
                logical_path=TRASH_INDEX_PATH,
            )
        plan = {
            "files": len(records),
            "dirs": 0,
            "bytes": sum(int(item.bytes or 0) for item in records),
            "entry_ids": [item.entry_id for item in records],
        }
        if not self._ctx.has_approval:
            return pending_approval_result(
                self._ctx,
                kind=OperationKind.PURGE,
                logical_path=TRASH_INDEX_PATH,
                plan=plan,
                warnings=["清理回收站不可恢复，必须审批"],
            )
        if payload.get("dry_run"):
            return preview_result(
                self._ctx,
                kind=OperationKind.PURGE,
                logical_path=TRASH_INDEX_PATH,
                plan=plan,
            )
        remaining = TrashIndex(
            [item for item in index.records() if item.entry_id not in {
                record.entry_id for record in records
            }],
            retention_days=index.retention_days,
        )
        # 回收站内容不能用 workspace_delete 删（客户端把 .lumi_trash 视为受保护路径），
        # 所以走"暂存删除 + 重写索引 + 一次提交"——同一提交里完成，不会留下半截状态。
        stages: list[tuple[str, str, str | None]] = [
            ("delete", record.trash_path, None) for record in records
        ]
        stages.append(("write", TRASH_INDEX_PATH, remaining.dumps()))
        state = await self._read_state(TRASH_INDEX_PATH, allow_trash=True)
        steps = await self._stage_and_commit(
            kind=OperationKind.PURGE,
            stages=stages,
            base_version=int(state.get("workspace_version") or 0),
            call_id=self._call_id(),
        )
        if steps is not None:
            return steps
        for record in records:
            left = await self._read_state(record.trash_path, allow_trash=True)
            if left.get("exists"):
                return failed_result(
                    self._ctx,
                    kind=OperationKind.PURGE,
                    logical_path=TRASH_INDEX_PATH,
                    code=OperationErrorCode.DELETE_FAILED.value,
                    message="清理提交后读回发现回收站条目仍存在，不报告成功",
                    details={"trash_id": record.entry_id},
                )
        return success_result(
            self._ctx,
            kind=OperationKind.PURGE,
            logical_path=TRASH_INDEX_PATH,
            rollback_state=RollbackState.UNAVAILABLE,
            stats=plan,
            warnings=["回收站内容已永久删除，不可恢复"],
        )

    # ── 内部：执行原语 ────────────────────────────────────

    async def _commit_content(
        self,
        *,
        kind: OperationKind,
        path: str,
        new_text: str,
        old_text: str,
        exists: bool,
        base_version: int,
        plan: dict[str, Any],
        newline: str = "",
    ) -> OperationResult:
        """写入/编辑共用的落盘路径：客户端写入 → 读回校验 → 生成新 revision。"""
        call_id = self._call_id()
        response = await self._client.write_text(path, new_text, call_id=call_id)
        verdict = self._verdict(response)
        if verdict == "pending":
            return pending_approval_result(
                self._ctx,
                kind=kind,
                logical_path=path,
                old_revision=rev.revision_for_text(old_text) if exists else "",
                plan=plan,
                warnings=["客户端已暂存变更，等待本机审批后提交"],
            )
        if verdict == "rejected":
            return denied_result(
                self._ctx,
                kind=kind,
                logical_path=path,
                code=OperationErrorCode.DENIED_BY_USER.value,
                message="用户拒绝了本次操作",
            )
        if verdict != "ok":
            return failed_result(
                self._ctx,
                kind=kind,
                logical_path=path,
                code=self._map_error(response, OperationErrorCode.WRITE_FAILED.value),
                message=str(response.get("error") or response.get("output") or "写入未完成"),
            )
        verified = await self._read_state(path)
        if not verified.get("exists"):
            return failed_result(
                self._ctx,
                kind=kind,
                logical_path=path,
                code=OperationErrorCode.WRITE_FAILED.value,
                message="写入已返回成功，但读回校验发现文件不存在（不报告成功）",
            )
        if verified.get("text_readable", True) and str(verified.get("text") or "") != new_text:
            return failed_result(
                self._ctx,
                kind=kind,
                logical_path=path,
                code=OperationErrorCode.WRITE_FAILED.value,
                message="写入已返回成功，但读回内容与预期不一致（不报告成功）",
            )
        added, removed = rev.line_change_stats(old_text, new_text)
        fields: dict[str, Any] = {}
        if kind is OperationKind.EDIT:
            fields["stats"] = {
                **plan,
                "occurrences": int(plan.get("occurrences") or 1),
                "added_lines": added,
                "removed_lines": removed,
                "newline_normalized": False,
            }
        else:
            fields["stats"] = {
                **plan,
                "created": not exists,
                "added_lines": added,
                "removed_lines": removed,
                "newline": newline,
                # 客户端提交路径是"同目录临时文件 + fsync + rename"：原子替换（§11.5）。
                "atomic_replace": True,
                # 但原子替换会换掉 inode，因此权限位不保留（临时文件用默认权限）。
                "permissions_preserved": False,
            }
        changes = self._result_changes(
            created=[] if exists else [path],
            modified=[path] if exists else [],
            added=added,
            removed=removed,
            bytes_written=len(new_text.encode("utf-8")),
        )
        return success_result(
            self._ctx,
            kind=kind,
            logical_path=path,
            old_revision=str(rev.revision_for_text(old_text)) if exists else "",
            new_revision=str(verified.get("revision") or ""),
            changes=changes,
            workspace_version=int(verified.get("workspace_version") or base_version),
            rollback_state=RollbackState.AVAILABLE,
            warnings=(
                ["原子替换会重置文件权限位（临时文件默认权限）"] if exists else []
            ),
            **fields,
        )

    async def _execute_move(
        self,
        *,
        request: MoveRequest,
        is_dir: bool,
        source_revision: str,
        overwrote: bool,
        plan: dict[str, Any],
    ) -> OperationResult:
        """移动：客户端 ``workspace_move`` = 同一文件系统内**单次 rename**（文件与目录都原子）。

        客户端会在 rename 之前给出结构化结论（``CROSS_DEVICE_UNSUPPORTED`` /
        ``TARGET_EXISTS`` / ``TARGET_IS_SOURCE_SUBDIR``），因此这里不自己实现"复制+删除"
        —— 跨设备移动一律拒绝，绝不先复制再删源。
        """
        response = await self._client.move(request.source_path, request.target_path)
        verdict = self._verdict(response)
        if verdict == "pending":
            return pending_approval_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                target_path=request.target_path,
                old_revision=source_revision,
                plan=plan,
                warnings=["移动已准备就绪，等待本机审批"],
            )
        if verdict == "rejected":
            return denied_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=OperationErrorCode.DENIED_BY_USER.value,
                message="用户拒绝了本次移动",
            )
        if verdict != "ok":
            return failed_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=self._map_error(response, OperationErrorCode.MOVE_FAILED.value),
                message=str(response.get("error") or response.get("output") or "移动未完成"),
                details={"target_path": request.target_path, "kind": "dir" if is_dir else "file"},
            )
        source_after = await self._read_state(request.source_path)
        target_after = await self._read_state(request.target_path)
        if source_after.get("exists") or not target_after.get("exists"):
            return failed_result(
                self._ctx,
                kind=OperationKind.MOVE,
                logical_path=request.source_path,
                code=OperationErrorCode.MOVE_FAILED.value,
                message="移动后读回校验不一致（源仍存在或目标不存在），不报告成功",
                details={
                    "source_exists": bool(source_after.get("exists")),
                    "target_exists": bool(target_after.get("exists")),
                },
            )
        changes = self._result_changes(
            moved=[{"from": request.source_path, "to": request.target_path}],
            bytes_written=int(target_after.get("size") or 0) if not is_dir else 0,
        )
        new_revision = str(
            (target_after.get("directory_revision") if is_dir else target_after.get("revision")) or ""
        )
        return success_result(
            self._ctx,
            kind=OperationKind.MOVE,
            logical_path=request.source_path,
            target_path=request.target_path,
            old_revision=source_revision,
            new_revision=new_revision,
            changes=changes,
            workspace_version=int(target_after.get("workspace_version") or 0),
            rollback_state=RollbackState.AVAILABLE,
            stats={
                **plan,
                "overwrote": bool(overwrote),
                "kind": "dir" if is_dir else "file",
                # 客户端实现是单次 rename：同文件系统内对文件与目录都原子。
                "atomic": True,
                "same_device": True,
            },
        )

    async def _execute_delete(
        self,
        *,
        request: DeleteRequest,
        is_dir: bool,
        stats: dict[str, Any],
        old_revision: str,
    ) -> OperationResult:
        """删除：客户端 ``workspace_delete``（默认 rename 进回收站，``permanent`` 才真删）。

        回收站由**客户端**维护（``.lumi_trash/<trash_id>`` + ``trash.json``）：
        rename 不读取内容，所以二进制文件同样可以进回收站；移动失败时客户端抛
        ``TRASH_MOVE_FAILED``，这里原样映射为 ``TRASH_UNAVAILABLE``——绝不报告删除成功。
        """
        response = await self._client.delete(
            request.path,
            recursive=bool(request.recursive),
            permanent=bool(request.permanent),
            call_id=self._call_id(),
        )
        verdict = self._verdict(response)
        if verdict == "pending":
            return pending_approval_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                old_revision=old_revision,
                plan=stats,
                warnings=["删除已准备就绪，等待本机审批"],
            )
        if verdict == "rejected":
            return denied_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                code=OperationErrorCode.DENIED_BY_USER.value,
                message="用户拒绝了本次删除",
            )
        if verdict != "ok":
            return failed_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                code=self._map_error(response, OperationErrorCode.DELETE_FAILED.value),
                message=str(response.get("error") or response.get("output") or "删除未完成"),
                details={"kind": "dir" if is_dir else "file", "permanent": bool(request.permanent)},
            )
        payload = response.get("data") if isinstance(response.get("data"), dict) else {}
        if bool(payload.get("already_absent")):
            return already_absent_result(
                self._ctx, kind=OperationKind.DELETE, logical_path=request.path
            )
        after = await self._read_state(request.path)
        if after.get("exists"):
            return failed_result(
                self._ctx,
                kind=OperationKind.DELETE,
                logical_path=request.path,
                code=OperationErrorCode.DELETE_FAILED.value,
                message="删除后读回发现目标仍存在，不报告成功",
            )
        permanent = bool(payload.get("permanent", request.permanent))
        reversible = bool(payload.get("reversible", not permanent))
        entry_id = str(payload.get("trash_id") or "")
        if not permanent:
            # 进回收站：内容必须在 .lumi_trash/<trash_id> 里（缺失说明没真删成功）。
            trash_after = await self._read_state(
                trash_entry_path(entry_id), allow_trash=True
            ) if entry_id else {"exists": False}
            if not entry_id or not trash_after.get("exists"):
                return failed_result(
                    self._ctx,
                    kind=OperationKind.DELETE,
                    logical_path=request.path,
                    code=OperationErrorCode.TRASH_UNAVAILABLE.value,
                    message="删除返回成功但回收站内容缺失，不报告成功",
                    details={"trash_id": entry_id},
                )
        return success_result(
            self._ctx,
            kind=OperationKind.DELETE,
            logical_path=request.path,
            changes=self._result_changes(deleted=[request.path]),
            rollback_state=(
                RollbackState.AVAILABLE if reversible else RollbackState.UNAVAILABLE
            ),
            workspace_version=int(after.get("workspace_version") or 0),
            stats=DeleteStats(
                **{
                    **stats,
                    "permanent": permanent,
                    "to_trash": not permanent,
                    "files": int(payload.get("files") or stats.get("files") or 0),
                    "dirs": int(payload.get("dirs") or stats.get("dirs") or 0),
                    "bytes": int(payload.get("bytes") or stats.get("bytes") or 0),
                    "recursive": bool(payload.get("recursive", request.recursive)),
                    "entry_id": entry_id,
                    "restorable": reversible,
                    "retention_days": int(self._ctx.trash.retention_days),
                    "is_symlink": False,
                }
            ).to_dict(),
            warnings=(
                ["已永久删除，不可恢复"]
                if permanent
                else [f"已移入回收站（trash_id={entry_id}，可用 restore 恢复）"]
            ),
        )

    async def _stage_and_commit(
        self,
        *,
        kind: OperationKind,
        stages: list[tuple[str, str, str | None]],
        base_version: int,
        call_id: str,
    ) -> OperationResult | None:
        """暂存写入/删除 + 一次提交；返回 ``None`` 表示提交成功。"""
        for action, path, content in stages:
            if action == "write":
                response = await self._client.stage_write(path, str(content or ""))
            else:
                response = await self._client.stage_delete(path)
            verdict = self._verdict(response)
            if verdict == "ok":
                continue
            if verdict == "pending":
                return pending_approval_result(
                    self._ctx,
                    kind=kind,
                    logical_path=path,
                    plan={"staged": len(stages)},
                    warnings=["变更已暂存，等待本机审批后提交"],
                )
            if verdict == "rejected":
                return denied_result(
                    self._ctx,
                    kind=kind,
                    logical_path=path,
                    code=OperationErrorCode.DENIED_BY_USER.value,
                    message="用户拒绝了本次操作",
                )
            return failed_result(
                self._ctx,
                kind=kind,
                logical_path=path,
                code=self._map_error(
                    response,
                    OperationErrorCode.MOVE_FAILED.value
                    if kind is OperationKind.MOVE
                    else OperationErrorCode.WRITE_FAILED.value,
                ),
                message=str(response.get("error") or response.get("output") or "暂存失败"),
            )
        commit = await self._client.commit(
            base_version=int(base_version or 0),
            idempotency_key=self._ctx.idempotency_key,
            call_id=call_id,
        )
        verdict = self._verdict(commit)
        if verdict == "ok":
            return None
        if verdict == "pending":
            return pending_approval_result(
                self._ctx,
                kind=kind,
                logical_path="",
                plan={"staged": len(stages)},
                warnings=["变更已暂存，等待本机审批后提交"],
            )
        if verdict == "rejected":
            return denied_result(
                self._ctx,
                kind=kind,
                logical_path="",
                code=OperationErrorCode.DENIED_BY_USER.value,
                message="用户拒绝了本次提交",
            )
        return failed_result(
            self._ctx,
            kind=kind,
            logical_path="",
            code=self._map_error(commit, OperationErrorCode.WRITE_FAILED.value),
            message=str(commit.get("error") or commit.get("output") or "提交失败"),
        )

    # ── 内部：读取与状态 ──────────────────────────────────

    async def _read_state(self, path: str, *, allow_trash: bool = False) -> dict[str, Any]:
        """读取路径状态：存在性/类型/内容/revision/workspace_version。"""
        if not allow_trash:
            reason = guard_normal_operation(path)
            if reason:
                return {
                    "exists": False,
                    "blocked": True,
                    "error_code": OperationErrorCode.TRASH_PATH_FORBIDDEN.value,
                    "message": reason,
                }
        listing = await self._client.list_dir(_parent(path))
        if not listing.get("ok", True):
            return {
                "exists": False,
                "error_code": str(listing.get("error_code") or "WORKSPACE_READ_FAILED"),
            }
        entry = _find_entry(listing.get("entries") or [], path)
        workspace_version = int(listing.get("workspace_version") or 0)
        if entry is None:
            return {"exists": False, "workspace_version": workspace_version}
        if str(entry.get("kind") or "") == "directory":
            # 目录要列自己的条目（父目录列表里只有它这一项），并给出**目录快照版本**
            # （移动/删除目录时校验它：不递归哈希整棵目录内容）。
            inner = await self._client.list_dir(path)
            entries = (inner.get("entries") if inner.get("ok") else []) or []
            snapshot = [
                {
                    "name": str(item.get("name") or ""),
                    "kind": str(item.get("kind") or "file"),
                    "size": int(item.get("size") or 0),
                    "revision": str(item.get("revision") or ""),
                }
                for item in entries
                if isinstance(item, dict)
            ]
            return {
                "exists": True,
                "is_dir": True,
                "entries": entries,
                "size": sum(int(item.get("size") or 0) for item in snapshot),
                "file_count": sum(1 for item in snapshot if item["kind"] != "directory"),
                "dir_count": sum(1 for item in snapshot if item["kind"] == "directory"),
                "directory_revision": RevisionRules.for_directory(snapshot),
                "workspace_version": int(
                    inner.get("workspace_version") or workspace_version
                ),
            }
        read = await self._client.read_text(path)
        if not read.get("ok"):
            return {
                "exists": True,
                "is_dir": False,
                "text_readable": False,
                "size": int(entry.get("size") or 0),
                "error_code": str(read.get("error_code") or "WORKSPACE_READ_FAILED"),
                "workspace_version": int(read.get("workspace_version") or workspace_version),
            }
        text = str(read.get("text") or "")
        return {
            "exists": True,
            "is_dir": False,
            "text_readable": True,
            "text": text,
            "size": int(entry.get("size") or len(text.encode("utf-8"))),
            "revision": str(read.get("revision") or rev.revision_for_text(text)),
            "workspace_version": int(read.get("workspace_version") or workspace_version),
            "truncated": bool(read.get("truncated")),
        }

    async def _load_index(self) -> tuple[TrashIndex | None, OperationResult | None]:
        state = await self._read_state(TRASH_INDEX_PATH, allow_trash=True)
        if state.get("exists") and state.get("text_readable", True):
            return TrashIndex.loads(str(state.get("text") or "")), None
        return TrashIndex(), None

    async def _write_index(self, index: TrashIndex) -> bool:
        """索引单独落盘（恢复流程用）；返回是否成功。

        失败只记警告（内容已经恢复成功，不能因为台账而谎报失败），但调用方会把
        "面板可能仍显示该条目"写进 warnings。
        """
        try:
            response = await self._client.write_text(
                TRASH_INDEX_PATH, index.dumps(), call_id=self._call_id()
            )
            return self._verdict(response) == "ok"
        except Exception as exc:  # noqa: BLE001 - 索引失败不影响"内容已恢复"的事实
            logger.warning("[operation] 回收站索引更新失败: {}", str(exc)[:160])
            return False

    # ── 内部：工具函数 ────────────────────────────────────

    def _guard_path(self, path: str) -> OperationResult | None:
        """路径前置校验：非空、越界、回收站、保护路径。"""
        if not path:
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path="",
                code=OperationErrorCode.INVALID_ARGUMENTS.value,
                message="必须提供工作区内的相对路径",
            )
        if is_trash_path(path):
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=path,
                code=OperationErrorCode.TRASH_PATH_FORBIDDEN.value,
                message=guard_normal_operation(path),
            )
        protected, reason = self._ctx.protection.is_protected(path)
        if protected:
            return failed_result(
                self._ctx,
                kind=OperationKind.WRITE,
                logical_path=path,
                code=OperationErrorCode.PROTECTED_PATH.value,
                message=reason,
            )
        return None

    def _call_id(self) -> str:
        """稳定的调用指纹：有幂等键时用它，保证重试命中客户端的同一次审批/提交。"""
        if self._ctx.idempotency_key:
            return f"op-{self._ctx.idempotency_key}"[:120]
        return f"op-{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _verdict(response: dict[str, Any]) -> str:
        """客户端 MCP 返回 → ``ok`` / ``pending`` / ``rejected`` / ``failed``。"""
        payload = response if isinstance(response, dict) else {}
        status = str(payload.get("status") or "").casefold()
        if status == "pending_approval":
            return "pending"
        code = str(payload.get("error_code") or "").upper()
        if code == "REJECTED":
            return "rejected"
        if payload.get("success") is True:
            return "ok"
        if payload.get("ok") is True:
            return "ok"
        return "failed"

    @staticmethod
    def _map_error(response: dict[str, Any], fallback: str) -> str:
        """客户端错误码 → 操作错误码。

        已经是操作错误码的（例如 ``NOT_SUPPORTED_BY_PROVIDER`` / ``TRASH_*``）原样透传：
        它们比"某个工具失败了"更准确，折叠掉就等于丢信息。
        """
        code = str((response or {}).get("error_code") or "").upper()
        if not code:
            return fallback
        if code in _OPERATION_ERROR_CODES:
            return code
        return _CLIENT_ERROR_MAP.get(code, fallback)

    @staticmethod
    def _result_changes(
        *,
        created: list[str] | None = None,
        modified: list[str] | None = None,
        deleted: list[str] | None = None,
        moved: list[dict[str, str]] | None = None,
        created_dirs: list[str] | None = None,
        added: int = 0,
        removed: int = 0,
        bytes_written: int = 0,
    ):
        from app.contracts.operations.common import ChangeSummary

        return ChangeSummary(
            created=list(created or []),
            modified=list(modified or []),
            deleted=list(deleted or []),
            moved=list(moved or []),
            created_dirs=list(created_dirs or []),
            added_lines=int(added),
            removed_lines=int(removed),
            bytes_written=int(bytes_written),
        )

    # ── 内部：幂等 ────────────────────────────────────────

    def _cache_key(self, kind: OperationKind, args: dict[str, Any]) -> str:
        key = str(self._ctx.idempotency_key or "")
        if not key:
            return ""
        return f"{kind}|{key}|{self._ctx.workspace_id}"

    def _idempotent_lookup(
        self, kind: OperationKind, args: dict[str, Any]
    ) -> OperationResult | None:
        key = self._cache_key(kind, args)
        if not key:
            return None
        entry = self._idempotent.get(key)
        if entry is None:
            return None
        if time.time() - entry[0] > 900:
            self._idempotent.pop(key, None)
            return None
        cached = entry[1]
        return cached.model_copy(
            update={"meta": {**cached.meta, "idempotent_replay": True}}
        )

    def _idempotent_store(self, result: OperationResult) -> None:
        key = self._cache_key(result.kind, {})
        if not key or not result.status.is_terminal:
            return
        if len(self._idempotent) >= IDEMPOTENCY_CACHE_SIZE:
            oldest = min(self._idempotent, key=lambda item: self._idempotent[item][0])
            self._idempotent.pop(oldest, None)
        self._idempotent[key] = (time.time(), result)

    # ── 内部：事件 ────────────────────────────────────────

    async def _publish_result(self, result: OperationResult) -> None:
        if not self._publish:
            return
        # Job 快照：记录"最近一次操作状态"，供刷新后恢复操作面板（与事件流职责不同）。
        try:
            from app.services.operation_snapshots import record_operation_summary

            await record_operation_summary(result)
        except Exception as exc:  # noqa: BLE001 - 快照失败不能影响操作结果
            logger.debug("[operation] 操作快照失败（降级）: {}", str(exc)[:120])
        try:
            from app.services.operation_events import (
                OPERATION_EVENT_PREVIEW,
                publish_operation_event,
                publish_operation_result,
            )

            if result.dry_run:
                fields = _preview_fields(result)
                await publish_operation_event(
                    OPERATION_EVENT_PREVIEW, job_id=result.job_id, **fields
                )
                return
            await publish_operation_result(result)
        except Exception as exc:  # noqa: BLE001 - 事件失败不能影响操作结果
            logger.debug("[operation] 事件发布失败（降级）: {}", str(exc)[:120])


# ── 生产实现：走桌面 MCP ──────────────────────────────────────────
# ── [运行时适配] 导航器合成端（无客户端时的服务端实现）
class NavigatorWorkspaceClient:
    """用 ``WorkspaceNavigatorService`` 把客户端原子工具包成网关需要的接口。

    只调用**已存在的**客户端工具：``workspace_list`` / ``workspace_read`` /
    ``workspace_write`` / ``workspace_stage_write`` / ``workspace_stage_delete`` /
    ``workspace_commit``。缺工具时返回结构化失败（而不是假装成功）。
    """

    def __init__(self, service: Any, *, read_max_bytes: int = 4 * 1024 * 1024) -> None:
        self._service = service
        self._read_max_bytes = int(read_max_bytes)

    @property
    def workspace_id(self) -> str:
        return str(getattr(self._service, "workspace_id", "") or "")

    async def tool_names(self) -> set[str]:
        try:
            return {str(item.get("name") or "") for item in await self._service.advertised_tools()}
        except Exception:  # noqa: BLE001 - 工具发现失败按"没有工具"处理
            return set()

    async def stat(self, path: str) -> dict[str, Any]:
        parent = _parent(path)
        listing = await self.list_dir(parent)
        if not listing.get("ok"):
            return listing
        entry = _find_entry(listing.get("entries") or [], path)
        if entry is None:
            return {"ok": True, "exists": False}
        return {"ok": True, "exists": True, **entry}

    async def list_dir(self, path: str) -> dict[str, Any]:
        try:
            payload = await self._service.call(
                "workspace_list",
                {
                    "workspace_id": self.workspace_id,
                    "path": path,
                    "depth": 1,
                    "max_results": 500,
                    # 操作网关必须看见隐藏/被忽略条目：否则 .lumi_trash（回收站索引与内容）
                    # 以及点开头的目标文件会被当成"不存在"，导致删除记录丢失或重复创建。
                    # 面向模型的过滤在 navigator 的 list 里做，这里是内部事实查询。
                    "include_ignored": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 - 客户端不可用要变成结构化失败
            code, message = getattr(exc, "code", ""), str(exc)
            return {"ok": False, "error_code": str(code or "WORKSPACE_READ_FAILED"), "message": message}
        failure = self._service.resolve_error(payload)
        if failure is not None:
            # 目录不存在是正常状态（很多调用就是来问"在不在"），其余才是失败。
            if failure[0] == "WORKSPACE_PATH_NOT_FOUND":
                return {"ok": True, "entries": [], "workspace_version": _version_of(payload)}
            return {"ok": False, "error_code": failure[0], "message": failure[1]}
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        rows = data.get("entries")
        if not isinstance(rows, list) or not rows:
            rows = data.get("items") if isinstance(data.get("items"), list) else rows
        entries = []
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            raw_kind = str(item.get("kind") or item.get("type") or "file").casefold()
            entries.append(
                {
                    "name": str(item.get("name") or _basename(item.get("path"))),
                    "path": str(item.get("path") or ""),
                    "kind": "directory" if raw_kind in {"directory", "dir", "folder"} else "file",
                    "size": int(item.get("size") or 0),
                    "modified_at": item.get("modified_at") or item.get("mtime"),
                    "ignored": bool(item.get("ignored")),
                }
            )
        return {"ok": True, "entries": entries, "workspace_version": _version_of(payload)}

    async def read_text(self, path: str) -> dict[str, Any]:
        from app.workspace.read.reader import WorkspaceReader

        service = self._service
        reader = WorkspaceReader(
            user_id=getattr(service, "user_id", ""),
            user_role=getattr(service, "user_role", "user"),
            workspace_id=self.workspace_id,
            conversation_id=getattr(service, "conversation_id", ""),
        )
        chunks: list[str] = []
        cursor = ""
        workspace_version = 0
        total = 0
        for _ in range(64):
            try:
                payload = await reader.read("", path=path, cursor=cursor, max_chars=200_000)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error_code": "WORKSPACE_READ_FAILED", "message": str(exc)[:200]}
            status = str(payload.get("status") or "")
            if status in {"failed", "empty"} and not payload.get("content"):
                meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
                return {
                    "ok": False,
                    "error_code": str(meta.get("error_code") or "WORKSPACE_READ_FAILED"),
                    "message": str(payload.get("summary") or "读取失败"),
                }
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            try:
                workspace_version = int(meta.get("workspace_version") or workspace_version)
            except (TypeError, ValueError):
                pass
            for item in payload.get("content") or []:
                if isinstance(item, dict):
                    text = str(item.get("text") or "")
                    total += len(text.encode("utf-8"))
                    if total > self._read_max_bytes:
                        return {
                            "ok": False,
                            "error_code": "CONTENT_TOO_LARGE",
                            "message": f"文件超过 {self._read_max_bytes} 字节，超出操作网关的读取上限",
                        }
                    chunks.append(text)
            if not payload.get("has_more"):
                break
            cursor = str(payload.get("cursor") or "")
            if not cursor:
                break
        text = "\n".join(chunks)
        return {
            "ok": True,
            "text": text,
            "revision": rev.revision_for_text(text),
            "workspace_version": workspace_version,
        }

    async def write_text(self, path: str, content: str, *, call_id: str = "") -> dict[str, Any]:
        return await self._call(
            "workspace_write", {"path": path, "content": content, "_lumi_call_id": call_id}
        )

    async def move(self, source: str, target: str) -> dict[str, Any]:
        """客户端 ``workspace_move``：同文件系统内单次 rename（文件与目录都原子）。"""
        return await self._call("workspace_move", {"from": source, "to": target})

    async def delete(
        self, path: str, *, recursive: bool = False, permanent: bool = False, call_id: str = ""
    ) -> dict[str, Any]:
        """客户端 ``workspace_delete``：默认 rename 进回收站，``permanent`` 才真删。"""
        return await self._call(
            "workspace_delete",
            {
                "path": path,
                "recursive": bool(recursive),
                "permanent": bool(permanent),
                "_lumi_call_id": call_id,
            },
        )

    async def stage_write(self, path: str, content: str) -> dict[str, Any]:
        return await self._call("workspace_stage_write", {"path": path, "content": content})

    async def stage_delete(self, path: str) -> dict[str, Any]:
        return await self._call("workspace_stage_delete", {"path": path})

    async def commit(
        self, *, base_version: int, idempotency_key: str, call_id: str = ""
    ) -> dict[str, Any]:
        return await self._call(
            "workspace_commit",
            {
                "base_version": int(base_version or 0),
                "idempotency_key": str(idempotency_key or call_id or uuid.uuid4().hex),
                "_lumi_call_id": call_id,
            },
        )

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        names = await self.tool_names()
        if names and tool not in names:
            return {
                "success": False,
                "error_code": "NOT_SUPPORTED_BY_PROVIDER",
                "error": f"当前客户端未提供 {tool} 原子能力",
            }
        payload_args = {"workspace_id": self.workspace_id, **args}
        try:
            payload = await self._service.call(tool, payload_args)
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error_code": str(getattr(exc, "code", "") or "WORKSPACE_READ_FAILED"),
                "error": str(exc)[:200],
            }
        if isinstance(payload, dict):
            return payload
        return {"success": False, "error_code": "WORKSPACE_READ_FAILED", "error": "客户端返回非结构化结果"}


# ── 小工具 ────────────────────────────────────────────────────────
def _parent(path: str) -> str:
    text = str(path or "")
    if "/" not in text:
        return ""
    return text.rsplit("/", 1)[0]


def _basename(path: Any) -> str:
    return str(path or "").rsplit("/", 1)[-1]


def _version_of(payload: Any) -> int:
    """客户端信封里的 ``workspace_version``（``_workspaceMeta`` 会把它铺在 data 里）。"""
    if not isinstance(payload, dict):
        return 0
    candidates = [payload.get("data"), payload.get("meta"), payload]
    for source in candidates:
        if not isinstance(source, dict):
            continue
        for key in ("workspace_version", "version"):
            if source.get(key) is None:
                continue
            try:
                return int(source[key])
            except (TypeError, ValueError):
                continue
    return 0


def _find_entry(entries: list[Any], path: str) -> dict[str, Any] | None:
    wanted = str(path or "").casefold()
    for item in entries:
        if not isinstance(item, dict):
            continue
        if str(item.get("path") or "").casefold() == wanted:
            return item
        if str(item.get("name") or "").casefold() == _basename(wanted):
            return item
    return None


def _preview_fields(result: OperationResult) -> dict[str, Any]:
    from app.services.operation_events import event_fields_for_result

    return event_fields_for_result(result)


#: 旧名称兼容（历史调用点/文档里出现过）。
# ── [纯决策] 工具 → 操作类别 / 预览字段（纯映射）
def operation_kind_for_tool(tool_name: str) -> OperationKind | None:
    """工具名 → 操作类型（含 ``code.edit`` 历史别名）。"""
    text = str(tool_name or "").strip().casefold()
    if not text:
        return None
    bare = text.split("__")[-1].rsplit(".", 1)[-1]
    return {
        "workspace_write": OperationKind.WRITE,
        "workspace_edit": OperationKind.EDIT,
        "code_edit": OperationKind.EDIT,
        "edit": OperationKind.EDIT,
        "workspace_move": OperationKind.MOVE,
        "workspace_delete": OperationKind.DELETE,
        "delete_file": OperationKind.DELETE,
    }.get(bare)


__all__ = [
    "IDEMPOTENCY_CACHE_SIZE",
    "NavigatorWorkspaceClient",
    "TRASH_INDEX_PATH",
    "WorkspaceClient",
    "WorkspaceOperationService",
    "operation_kind_for_tool",
]
