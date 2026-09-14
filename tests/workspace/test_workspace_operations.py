"""工作区操作契约与网关回归：``workspace.write / edit / move / delete`` + 回收站。

覆盖用户方案里的验收场景（后端部分）：

* **write**：新建 / 覆盖（缺版本 → REVISION_REQUIRED）/ 内容相同 → ``no_change`` /
  空内容拒绝 / 显式清空 / 版本过期 / 保护路径 / 回收站路径 / dry_run 不落盘；
* **edit**：唯一匹配 / 找不到 / 多处匹配 / 相同内容 → ``no_change`` /
  缺版本 / 版本过期 / 换行归一；
* **move**：文件重命名 / 目标已存在 / 源不存在 → ``already_absent`` /
  目标在源内部 / 目录移动 → ``NOT_SUPPORTED_BY_PROVIDER``；
* **delete**：移入回收站（默认）/ 永久删除需审批 / 路径不存在 → ``already_absent`` /
  目录删除 → ``NOT_SUPPORTED_BY_PROVIDER`` / 二进制内容不可入回收站；
* **回收站**：概览 / 恢复 / 配额；
* **幂等**：同一幂等键不重复落盘；
* **契约**：状态词表、revision 算法唯一、``OperationResult → ToolOutput`` 兼容投影。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.contracts.operations import (
    OperationApprovalState,
    OperationContext,
    OperationErrorCode,
    OperationKind,
    OperationStatus,
    RevisionRules,
    already_absent_result,
    no_change_result,
    pending_approval_result,
    success_result,
)
from app.workspace.write.operations import (
    TRASH_INDEX_PATH,
    WorkspaceOperationService,
    operation_kind_for_tool,
)
from app.workspace.write.revision import (
    apply_newline_policy,
    line_change_stats,
    match_count,
    replace_once,
    revision_for_text,
)
from app.workspace.write.trash import TrashIndex, build_record

# ── 内存客户端（替身桌面 MCP）────────────────────────────────────


class FakeClient:
    """内存工作区：语义与客户端原子工具一致（暂存 + 一次提交、base_version 校验）。"""

    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        dirs: set[str] | None = None,
        binary: set[str] | None = None,
        tools: set[str] | None = None,
        pending: set[str] | None = None,
        rejected: set[str] | None = None,
        broken: set[str] | None = None,
        client_errors: dict[str, str] | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.dirs = set(dirs or ())
        self.binary = set(binary or ())
        self.tools = set(
            tools
            or {
                "workspace_list",
                "workspace_read",
                "workspace_write",
                "workspace_edit",
                "workspace_move",
                "workspace_delete",
                "workspace_stage_write",
                "workspace_stage_delete",
                "workspace_commit",
            }
        )
        self.pending = set(pending or ())
        self.rejected = set(rejected or ())
        self.broken = set(broken or ())
        self.version = 1
        self.staged_writes: dict[str, str] = {}
        self.staged_deletes: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        #: 客户端错误码注入（按工具名），用于模拟结构化失败码
        self.client_errors: dict[str, str] = dict(client_errors or {})
        #: 回收站索引由客户端维护：这里以 ``.lumi_trash/trash.json`` 为准
        self.files.setdefault(TRASH_INDEX_PATH, "")

    # ── 回收站索引（客户端视角）──
    def _set_trash_entries(self, entries: list[dict]) -> None:
        self.files[TRASH_INDEX_PATH] = TrashIndex(
            [
                build_record(
                    logical_path=str(entry.get("logical_path") or ""),
                    entry_id=str(entry.get("trash_id") or ""),
                    bytes_size=int(entry.get("bytes") or 0),
                    files=int(entry.get("files") or 0),
                    dirs=int(entry.get("dirs") or 0),
                    is_dir=str(entry.get("kind") or "file") == "dir",
                    deleted_at=str(entry.get("deleted_at") or ""),
                    expires_at=str(entry.get("expires_at") or ""),
                )
                for entry in entries
            ]
        ).dumps()

    def _trash_entries(self) -> list[dict]:
        raw = self.files.get(TRASH_INDEX_PATH) or ""
        if not raw.strip():
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        rows = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        # 索引里存的是客户端字段名（trash_id），测试里统一按 trash_id 读
        return [dict(item) for item in rows if isinstance(item, dict)]

    # ── 内部 ──
    def _all_dirs(self) -> set[str]:
        dirs = set(self.dirs)
        for path in list(self.files) + list(self.staged_writes):
            parts = path.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:index]))
        for path in self.staged_deletes:
            parts = path.split("/")[:-1]
            for index in range(1, len(parts) + 1):
                dirs.add("/".join(parts[:index]))
        return dirs

    def _remove_tree(self, path: str) -> None:
        """删掉路径及其子树（rename/rm 语义：目录本身也要消失）。"""
        for item in [key for key in self.files if key == path or key.startswith(f"{path}/")]:
            self.files.pop(item, None)
        self.dirs = {
            item for item in self.dirs if item != path and not item.startswith(f"{path}/")
        }

    def _children(self, parent: str) -> list[dict]:
        rows: list[dict] = []
        prefix = f"{parent}/" if parent else ""
        names = set(self.files) | self._all_dirs()
        for path in sorted(names):
            if not path.startswith(prefix) or path == parent:
                continue
            rest = path[len(prefix):]
            if "/" in rest:
                continue
            kind = "directory" if path in self._all_dirs() else "file"
            size = 0
            if kind == "file":
                raw = self.files.get(path, "")
                size = len(raw.encode("utf-8")) if isinstance(raw, str) else 0
            rows.append({"name": rest, "path": path, "kind": kind, "size": size})
        return rows

    def _verdict(self, tool: str) -> dict | None:
        if tool in self.client_errors:
            code = str(self.client_errors[tool])
            return {"success": False, "error_code": code, "error": f"客户端返回 {code}"}
        if tool in self.broken:
            return {
                "success": False,
                "error_code": {
                    "workspace_move": "WORKSPACE_MOVE_FAILED",
                    "workspace_delete": "WORKSPACE_DELETE_FAILED",
                }.get(tool, "WORKSPACE_WRITE_FAILED"),
                "error": "客户端执行失败",
            }
        if tool in self.rejected:
            return {"success": False, "error_code": "REJECTED", "error": "用户拒绝"}
        if tool in self.pending:
            return {"status": "pending_approval", "success": False, "call_id": "c1"}
        return None

    # ── WorkspaceClient 协议 ──
    async def tool_names(self) -> set[str]:
        return set(self.tools)

    async def stat(self, path: str) -> dict:
        if path in self.files:
            return {"ok": True, "exists": True, "kind": "file", "size": len(self.files[path])}
        if path in self._all_dirs():
            return {"ok": True, "exists": True, "kind": "directory", "size": 0}
        return {"ok": True, "exists": False}

    async def list_dir(self, path: str) -> dict:
        if path and path not in self._all_dirs() and not any(
            item.startswith(f"{path}/") for item in self.files
        ):
            return {"ok": False, "error_code": "WORKSPACE_PATH_NOT_FOUND", "message": "目录不存在"}
        return {"ok": True, "entries": self._children(path), "workspace_version": self.version}

    async def read_text(self, path: str) -> dict:
        if path not in self.files:
            return {"ok": False, "error_code": "WORKSPACE_PATH_NOT_FOUND", "message": "不存在"}
        if path in self.binary:
            return {
                "ok": False,
                "error_code": "WORKSPACE_UNSUPPORTED_FORMAT",
                "message": "不是可读文本",
            }
        text = self.files[path]
        return {
            "ok": True,
            "text": text,
            "revision": revision_for_text(text),
            "workspace_version": self.version,
        }

    async def write_text(self, path: str, content: str, *, call_id: str = "") -> dict:
        self.calls.append(("write_text", {"path": path, "content": content, "call_id": call_id}))
        if "workspace_write" not in self.tools:
            return {"success": False, "error_code": "NOT_SUPPORTED_BY_PROVIDER", "error": "无工具"}
        verdict = self._verdict("workspace_write")
        if verdict is not None:
            return verdict
        if path in self.binary:
            return {"success": False, "error_code": "READ_BEFORE_WRITE_REQUIRED", "error": "需先读取"}
        self.files[path] = content
        self.version += 1
        return {
            "success": True,
            "data": {"path": path, "bytes": len(content.encode("utf-8")), "status": "committed"},
        }

    async def move(self, source: str, target: str) -> dict:
        """客户端 ``workspace_move``：同文件系统内单次 rename（文件与目录都原子）。"""
        self.calls.append(("move", {"from": source, "to": target}))
        if "workspace_move" not in self.tools:
            return {"success": False, "error_code": "NOT_SUPPORTED_BY_PROVIDER", "error": "无工具"}
        verdict = self._verdict("workspace_move")
        if verdict is not None:
            return verdict
        if source not in self.files and source not in self._all_dirs():
            return {
                "success": False,
                "error_code": "WORKSPACE_PATH_NOT_FOUND",
                "error": f"源路径不存在：{source}",
            }
        if source == target:
            return {"success": True, "data": {"from": source, "to": target, "no_change": True}}
        if target in self.files or target in self._all_dirs():
            return {
                "success": False,
                "error_code": "TARGET_EXISTS",
                "error": f"目标已存在：{target}",
            }
        if target.startswith(f"{source}/"):
            return {
                "success": False,
                "error_code": "TARGET_IS_SOURCE_SUBDIR",
                "error": "目标位于源目录之内",
            }
        is_dir = source not in self.files
        if is_dir:
            # 目录：整棵子树一起搬（rename 语义），源目录本身也要消失
            moved_files = {
                item: self.files[item]
                for item in list(self.files)
                if item.startswith(f"{source}/")
            }
            moved_dirs = {
                item for item in self._all_dirs() if item.startswith(f"{source}/")
            }
            self._remove_tree(source)
            for path, content in moved_files.items():
                self.files[f"{target}{path[len(source):]}"] = content
            self.dirs.add(target)
            self.dirs |= {f"{target}{item[len(source):]}" for item in moved_dirs}
        else:
            self.files[target] = self.files.pop(source)
        self.version += 1
        return {
            "success": True,
            "data": {
                "rel": target,
                "from": source,
                "to": target,
                "atomic": True,
                "revision": None,
                "is_directory": is_dir,
            },
        }

    async def delete(
        self, path: str, *, recursive: bool = False, permanent: bool = False, call_id: str = ""
    ) -> dict:
        """客户端 ``workspace_delete``：默认 rename 进回收站，``permanent`` 才真删。"""
        self.calls.append(
            ("delete", {"path": path, "recursive": recursive, "permanent": permanent})
        )
        if "workspace_delete" not in self.tools:
            return {"success": False, "error_code": "NOT_SUPPORTED_BY_PROVIDER", "error": "无工具"}
        verdict = self._verdict("workspace_delete")
        if verdict is not None:
            return verdict
        exists = path in self.files or path in self._all_dirs()
        if not exists:
            return {
                "success": True,
                "data": {"rel": path, "already_absent": True, "files": 0, "dirs": 0, "bytes": 0},
            }
        is_dir = path in self._all_dirs()
        members = [item for item in self.files if item == path or item.startswith(f"{path}/")]
        if is_dir and members and not recursive:
            return {
                "success": False,
                "error_code": "RECURSIVE_REQUIRED",
                "error": "目录非空，需要显式 recursive",
            }
        size = sum(len(self.files[item]) for item in members)
        dirs = sum(
            1 for item in self._all_dirs() if item == path or item.startswith(f"{path}/")
        )
        if permanent:
            self._remove_tree(path)
            self.version += 1
            return {
                "success": True,
                "data": {
                    "rel": path,
                    "permanent": True,
                    "reversible": False,
                    "files": len(members),
                    "dirs": max(0, dirs - 1) if is_dir else 0,
                    "bytes": size,
                    "recursive": recursive,
                },
            }
        entries = self._trash_entries()
        trash_id = f"t_{len(entries) + 1}"
        base = f".lumi_trash/{trash_id}"
        if is_dir:
            moved = {
                item: self.files[item] for item in members if item != path
            }
            self._remove_tree(path)
            for item, content in moved.items():
                self.files[f"{base}{item[len(path):]}"] = content
        else:
            self.files[base] = self.files.pop(path)
            self.dirs.discard(path)
        entries.append(
            {
                "trash_id": trash_id,
                "logical_path": path,
                "kind": "dir" if is_dir else "file",
                "bytes": size,
                "files": len(members),
                "dirs": max(0, dirs - 1) if is_dir else 0,
                "deleted_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-01-08T00:00:00+00:00",
                "restorable": True,
            }
        )
        self._set_trash_entries(entries)
        self.version += 1
        return {
            "success": True,
            "data": {
                "rel": path,
                "trash_id": trash_id,
                "permanent": False,
                "reversible": True,
                "expires_at": "2026-01-08T00:00:00+00:00",
                "is_directory": is_dir,
                "recursive": recursive,
                "files": len(members),
                "dirs": max(0, dirs - 1) if is_dir else 0,
                "bytes": size,
            },
        }

    async def stage_write(self, path: str, content: str) -> dict:
        self.calls.append(("stage_write", {"path": path, "content": content}))
        if "workspace_stage_write" not in self.tools:
            return {"success": False, "error_code": "NOT_SUPPORTED_BY_PROVIDER", "error": "无工具"}
        verdict = self._verdict("workspace_stage_write")
        if verdict is not None:
            return verdict
        self.staged_writes[path] = content
        return {"success": True, "data": {"path": path, "staged": True}}

    async def stage_delete(self, path: str) -> dict:
        self.calls.append(("stage_delete", {"path": path}))
        if "workspace_stage_delete" not in self.tools:
            return {"success": False, "error_code": "NOT_SUPPORTED_BY_PROVIDER", "error": "无工具"}
        verdict = self._verdict("workspace_stage_delete")
        if verdict is not None:
            return verdict
        self.staged_deletes.append(path)
        return {"success": True, "data": {"path": path, "staged": True}}

    async def commit(self, *, base_version: int, idempotency_key: str, call_id: str = "") -> dict:
        self.calls.append(
            ("commit", {"base_version": base_version, "idempotency_key": idempotency_key})
        )
        verdict = self._verdict("workspace_commit")
        if verdict is not None:
            return verdict
        if base_version and int(base_version) != self.version:
            return {
                "success": False,
                "error_code": "WORKSPACE_VERSION_CONFLICT",
                "error": "工作区已被修改",
            }
        for path, content in self.staged_writes.items():
            self.files[path] = content
        for path in self.staged_deletes:
            self._remove_tree(path)
        self.staged_writes.clear()
        self.staged_deletes.clear()
        self.version += 1
        return {"success": True, "data": {"committed": 1, "workspace_version": self.version}}


def _service(client: FakeClient, **kwargs) -> WorkspaceOperationService:
    ctx = OperationContext(
        workspace_id="ws-1",
        device_id="dev-1",
        job_id="job-1",
        conversation_id="conv-1",
        idempotency_key=str(kwargs.pop("idempotency_key", "")),
        approval_state=kwargs.pop("approval_state", OperationApprovalState.NOT_REQUIRED),
        **kwargs,
    )
    return WorkspaceOperationService(client, context=ctx, publish_events=False)


def _run(coro):
    return asyncio.run(coro)


# ── 1. 契约与纯函数 ──────────────────────────────────────────────


def test_status_vocabulary_separates_state_from_error():
    assert OperationStatus.SUCCESS.is_ok and OperationStatus.NO_CHANGE.is_ok
    assert OperationStatus.ALREADY_ABSENT.is_ok, "幂等删除成功态不是错误"
    assert not OperationStatus.DENIED.is_ok and not OperationStatus.FAILED.is_ok
    assert not OperationStatus.PENDING_APPROVAL.is_terminal
    assert OperationStatus.coerce("no_change") is OperationStatus.NO_CHANGE


def test_revision_algorithm_is_single_and_matchable():
    first = RevisionRules.for_file("hello")
    assert first.startswith("sha256:") and first.endswith(":5")
    assert RevisionRules.for_file("hello") == first
    assert RevisionRules.for_file("hello!") != first, "内容变了版本必变"
    directory = RevisionRules.for_directory(
        [{"name": "a.py", "kind": "file", "size": 5, "revision": first}]
    )
    assert directory.startswith("dir1:") and directory.endswith(":1")
    # 完整串 / 裸哈希 / 前缀都能匹配同一次版本（调用方不必精确复制）
    digest = first.split(":")[1]
    assert RevisionRules.matches(first, first)
    assert RevisionRules.matches(digest, first)
    assert RevisionRules.matches(digest[:8], first)
    assert not RevisionRules.matches("deadbeef", first)
    assert RevisionRules.matches("", first), "没给版本表示不校验"


def test_edit_helpers_and_line_stats():
    assert match_count("a\nb\na\n", "a") == 2
    assert match_count("a\r\nb\r\n", "a\nb", normalize_newlines=True) == 1
    replaced, count = replace_once("a\nb\na\n", "a", "x", occurrence=2)
    assert count == 1 and replaced == "a\nb\nx\n", "只替换指定的第 N 处"
    assert replace_once("a\nb\na\n", "a", "x")[1] == 1, "默认最多替换一处"
    added, removed = line_change_stats("a\nb\nc\n", "a\nB\nc\nd\n")
    assert added == 2 and removed == 1
    text, style = apply_newline_policy("a\nb", "preserve", existing="x\r\ny\r\n")
    assert style == "crlf" and text == "a\r\nb", "沿用已有文件换行风格"


def test_operation_result_projects_into_existing_tool_output():
    """不新增平行信封：OperationResult 必须能折进既有 ToolOutput。"""
    ok = success_result(None, kind=OperationKind.WRITE, logical_path="a.py", new_revision="sha256:x:1")
    output = ok.to_tool_output(tool_name="workspace_write")
    assert output.status == "success"
    assert output.content_type == "structured"
    assert output.metadata["operation"] == "write"
    assert output.metadata["new_revision"] == "sha256:x:1"
    assert output.data["operation"] == "write"

    unchanged = no_change_result(None, kind=OperationKind.WRITE, logical_path="a.py", revision="sha256:x:1")
    assert unchanged.to_tool_output().status == "success"
    assert unchanged.to_tool_output().metadata["decision_signals"]["no_change"] is True

    pending = pending_approval_result(None, kind=OperationKind.DELETE, logical_path="a.py")
    assert pending.to_tool_output().status == "pending_approval"
    assert pending.error is None, "待审批不是错误"

    absent = already_absent_result(None, kind=OperationKind.DELETE, logical_path="a.py")
    assert absent.to_tool_output().status == "success"
    assert absent.status is OperationStatus.ALREADY_ABSENT

    capability = ok.to_capability_result(capability="workspace.write@1")
    assert capability.ok and capability.payload["operation"] == "write"


def test_tool_name_mapping_covers_aliases():
    assert operation_kind_for_tool("workspace_write") is OperationKind.WRITE
    assert operation_kind_for_tool("mcp__lumi_pc__workspace_edit") is OperationKind.EDIT
    assert operation_kind_for_tool("code.edit") is OperationKind.EDIT
    assert operation_kind_for_tool("workspace_move") is OperationKind.MOVE
    assert operation_kind_for_tool("workspace_delete") is OperationKind.DELETE
    assert operation_kind_for_tool("unknown_tool") is None


def test_every_operation_error_code_has_metadata():
    from app.contracts.operations import registered_codes
    from app.contracts.operations.errors import OperationErrorCode

    missing = {item.value for item in OperationErrorCode} - set(registered_codes())
    assert not missing, f"这些错误码没有登记重试/审批/下一步建议：{sorted(missing)}"


# ── 2. write ─────────────────────────────────────────────────────


def test_write_creates_file_and_reports_revision():
    client = FakeClient()
    result = _run(_service(client).write({"path": "src/new.py", "content": "print(1)\n"}))
    assert result.status is OperationStatus.SUCCESS
    assert result.new_revision == revision_for_text("print(1)\n")
    assert client.files["src/new.py"] == "print(1)\n"
    assert result.changes.created == ["src/new.py"]
    assert result.rollback_state.value == "available"


def test_write_overwrite_requires_expected_revision():
    client = FakeClient({"a.py": "old\n"})
    service = _service(client)
    blocked = _run(service.write({"path": "a.py", "content": "new\n"}))
    assert blocked.status is OperationStatus.FAILED
    assert blocked.error.code == OperationErrorCode.REVISION_REQUIRED.value
    assert client.files["a.py"] == "old\n", "缺版本时不得盲写"

    ok = _run(
        service.write(
            {"path": "a.py", "content": "new\n", "expected_revision": revision_for_text("old\n")}
        )
    )
    assert ok.status is OperationStatus.SUCCESS and client.files["a.py"] == "new\n"
    assert ok.old_revision == revision_for_text("old\n")


def test_write_same_content_is_no_change_and_does_not_touch_client():
    client = FakeClient({"a.py": "same\n"})
    result = _run(
        _service(client).write(
            {"path": "a.py", "content": "same\n", "expected_revision": revision_for_text("same\n")}
        )
    )
    assert result.status is OperationStatus.NO_CHANGE
    assert result.rollback_state.value == "not_needed"
    assert not [call for call in client.calls if call[0] == "write_text"], "no_change 不应写入"


def test_write_rejects_empty_and_revision_mismatch():
    client = FakeClient({"a.py": "old\n"})
    service = _service(client)
    empty = _run(service.write({"path": "new.py", "content": "   "}))
    assert empty.error.code == OperationErrorCode.EMPTY_CONTENT_REJECTED.value
    allowed = _run(service.write({"path": "new.py", "content": "   ", "allow_empty": True}))
    assert allowed.status is OperationStatus.SUCCESS

    stale = _run(
        service.write({"path": "a.py", "content": "x\n", "expected_revision": "sha256:deadbeefdeadbeef:3"})
    )
    assert stale.error.code == OperationErrorCode.REVISION_MISMATCH.value
    assert stale.error.retryable is True and stale.error.safe_next_action


def test_write_blocks_protected_and_trash_paths():
    client = FakeClient()
    service = _service(client)
    protected = _run(service.write({"path": ".git/config", "content": "x"}))
    assert protected.error.code == OperationErrorCode.PROTECTED_PATH.value
    secret = _run(service.write({"path": "config/id_rsa", "content": "x"}))
    assert secret.error.code == OperationErrorCode.PROTECTED_PATH.value
    trash = _run(service.write({"path": ".lumi_trash/files/x/a.py", "content": "x"}))
    assert trash.error.code == OperationErrorCode.TRASH_PATH_FORBIDDEN.value


def test_write_dry_run_previews_without_writing():
    client = FakeClient({"a.py": "old\n"})
    result = _run(
        _service(client).write(
            {
                "path": "a.py",
                "content": "new\n",
                "expected_revision": revision_for_text("old\n"),
                "dry_run": True,
            }
        )
    )
    assert result.dry_run is True and result.status is OperationStatus.NO_CHANGE
    assert client.files["a.py"] == "old\n"
    assert any("dry_run" in item for item in result.warnings)


def test_write_reports_failure_when_client_breaks():
    client = FakeClient({"a.py": "old\n"}, broken={"workspace_write"})
    result = _run(
        _service(client).write(
            {"path": "a.py", "content": "new\n", "expected_revision": revision_for_text("old\n")}
        )
    )
    assert result.status is OperationStatus.FAILED
    assert result.error.code == OperationErrorCode.WRITE_FAILED.value


def test_write_pending_approval_from_client_is_not_error():
    client = FakeClient({"a.py": "old\n"}, pending={"workspace_write"})
    result = _run(
        _service(client).write(
            {"path": "a.py", "content": "new\n", "expected_revision": revision_for_text("old\n")}
        )
    )
    assert result.status is OperationStatus.PENDING_APPROVAL
    assert result.error is None
    assert result.to_tool_output().status == "pending_approval"


def test_write_refuses_binary_file_without_revision():
    client = FakeClient({"logo.png": "binary"}, binary={"logo.png"})
    result = _run(_service(client).write({"path": "logo.png", "content": "x"}))
    assert result.error.code == OperationErrorCode.REVISION_REQUIRED.value


# ── 3. edit ──────────────────────────────────────────────────────


def test_edit_replaces_unique_match():
    client = FakeClient({"a.py": "def f():\n    return 1\n"})
    result = _run(
        _service(client).edit(
            {
                "path": "a.py",
                "old_str": "return 1",
                "new_str": "return 2",
                "expected_revision": revision_for_text("def f():\n    return 1\n"),
            }
        )
    )
    assert result.status is OperationStatus.SUCCESS
    assert client.files["a.py"] == "def f():\n    return 2\n"
    assert result.new_revision == revision_for_text(client.files["a.py"])
    assert result.stats["added_lines"] == 1 and result.stats["removed_lines"] == 1


def test_edit_rejects_missing_multiple_and_noop():
    original = "a\nb\na\n"
    client = FakeClient({"a.py": original})
    service = _service(client)
    revision = revision_for_text(original)

    missing = _run(
        service.edit({"path": "a.py", "old_str": "zzz", "new_str": "x", "expected_revision": revision})
    )
    assert missing.error.code == OperationErrorCode.OLD_TEXT_NOT_FOUND.value

    multiple = _run(
        service.edit({"path": "a.py", "old_str": "a", "new_str": "x", "expected_revision": revision})
    )
    assert multiple.error.code == OperationErrorCode.OLD_TEXT_NOT_UNIQUE.value
    assert client.files["a.py"] == original, "多处匹配不得整体替换"

    second = _run(
        service.edit(
            {
                "path": "a.py",
                "old_str": "a",
                "new_str": "x",
                "occurrence": 2,
                "expected_revision": revision,
            }
        )
    )
    assert second.status is OperationStatus.SUCCESS and client.files["a.py"] == "a\nb\nx\n"

    noop = _run(
        _service(FakeClient({"a.py": original})).edit(
            {"path": "a.py", "old_str": "a", "new_str": "a", "expected_revision": revision}
        )
    )
    assert noop.status is OperationStatus.NO_CHANGE


def test_edit_requires_revision_and_detects_concurrent_change():
    client = FakeClient({"a.py": "one\n"})
    service = _service(client)
    missing = _run(service.edit({"path": "a.py", "old_str": "one", "new_str": "two"}))
    assert missing.error.code == OperationErrorCode.REVISION_REQUIRED.value

    stale = _run(
        service.edit(
            {
                "path": "a.py",
                "old_str": "one",
                "new_str": "two",
                "expected_revision": revision_for_text("zero\n"),
            }
        )
    )
    assert stale.error.code == OperationErrorCode.REVISION_MISMATCH.value
    assert client.files["a.py"] == "one\n"


def test_edit_normalizes_crlf_when_matching():
    client = FakeClient({"a.py": "line1\r\nline2\r\n"})
    result = _run(
        _service(client).edit(
            {
                "path": "a.py",
                "old_str": "line1\nline2\n",
                "new_str": "line1\nchanged\n",
                "expected_revision": revision_for_text("line1\r\nline2\r\n"),
            }
        )
    )
    assert result.status is OperationStatus.SUCCESS
    assert client.files["a.py"] == "line1\nchanged\n"


# ── 4. move ──────────────────────────────────────────────────────


def test_move_renames_file_and_verifies_both_sides():
    client = FakeClient({"src/a.py": "content\n"})
    result = _run(
        _service(client).move(
            {
                "source_path": "src/a.py",
                "target_path": "src/b.py",
                "expected_revision": revision_for_text("content\n"),
            }
        )
    )
    assert result.status is OperationStatus.SUCCESS
    assert "src/a.py" not in client.files and client.files["src/b.py"] == "content\n"
    assert result.target_path == "src/b.py" and result.new_revision
    # 客户端实现是单次 rename：同文件系统内文件与目录都原子（§11.5）
    assert result.stats["atomic"] is True
    assert result.stats["kind"] == "file"
    assert result.changes.moved == [{"from": "src/a.py", "to": "src/b.py"}]
    assert any(call[0] == "move" for call in client.calls), "必须走客户端 workspace_move"


def test_move_never_overwrites_target():
    """客户端 rename 不覆盖：目标存在一律拒绝（不做"删除再移动"的两步操作）。"""
    client = FakeClient({"a.py": "A\n", "b.py": "B\n"})
    blocked = _run(_service(client).move({"source_path": "a.py", "target_path": "b.py"}))
    assert blocked.error.code == OperationErrorCode.ALREADY_EXISTS.value

    with_overwrite = _run(
        _service(client, approval_state=OperationApprovalState.APPROVED).move(
            {"source_path": "a.py", "target_path": "b.py", "overwrite": True}
        )
    )
    assert with_overwrite.status is OperationStatus.FAILED
    assert with_overwrite.error.code == OperationErrorCode.ALREADY_EXISTS.value
    assert client.files["a.py"] == "A\n" and client.files["b.py"] == "B\n", "拒绝时两边都不能动"


def test_move_missing_source_and_forbidden_targets():
    client = FakeClient({"a.py": "A\n"})
    service = _service(client)
    absent = _run(service.move({"source_path": "gone.py", "target_path": "x.py"}))
    assert absent.status is OperationStatus.ALREADY_ABSENT

    inside = _run(service.move({"source_path": "dir", "target_path": "dir/sub/x.py"}))
    assert inside.error.code == OperationErrorCode.TARGET_INSIDE_SOURCE.value


def test_move_directory_is_atomic_rename():
    """目录移动已支持：同一文件系统内单次 rename 本身原子（不再是 NOT_SUPPORTED）。"""
    client = FakeClient({"dir/a.py": "x\n", "dir/sub/b.py": "y\n"}, dirs={"dir", "dir/sub"})
    result = _run(
        _service(client, approval_state=OperationApprovalState.APPROVED).move(
            {"source_path": "dir", "target_path": "dir2"}
        )
    )
    assert result.status is OperationStatus.SUCCESS
    assert result.stats["atomic"] is True and result.stats["kind"] == "dir"
    assert "dir/a.py" not in client.files
    assert client.files["dir2/a.py"] == "x\n" and client.files["dir2/sub/b.py"] == "y\n"
    assert result.new_revision.startswith("dir1:"), "目录移动返回目录快照版本"


def test_move_cross_device_is_rejected_not_copied():
    client = FakeClient({"a.py": "A\n"}, broken={"workspace_move"})
    result = _run(_service(client).move({"source_path": "a.py", "target_path": "b.py"}))
    assert result.status is OperationStatus.FAILED
    assert result.error.code == OperationErrorCode.MOVE_FAILED.value
    assert client.files["a.py"] == "A\n"


def test_move_stale_revision_is_rejected():
    client = FakeClient({"a.py": "A\n"})
    result = _run(
        _service(client).move(
            {"source_path": "a.py", "target_path": "b.py", "expected_revision": "sha256:ffffffffffffffff:0"}
        )
    )
    assert result.error.code == OperationErrorCode.REVISION_MISMATCH.value
    assert client.files["a.py"] == "A\n"


# ── 5. delete 与回收站 ────────────────────────────────────────────


def _trash_paths(client: FakeClient) -> list[str]:
    """回收站里的**内容**路径（``trash.json`` 索引本身不算内容）。"""
    return sorted(
        path
        for path in client.files
        if path.startswith(".lumi_trash/") and not path.endswith("trash.json")
    )


def test_delete_moves_file_into_trash_and_records_it():
    client = FakeClient({"docs/a.md": "hello\n"})
    result = _run(_service(client).delete({"path": "docs/a.md"}))
    assert result.status is OperationStatus.SUCCESS
    assert "docs/a.md" not in client.files
    assert _trash_paths(client) == [".lumi_trash/t_1"], "内容应 rename 到 .lumi_trash/<trash_id>"
    entries = client._trash_entries()  # noqa: SLF001 - 客户端索引即台账
    assert entries and entries[0]["logical_path"] == "docs/a.md"
    assert result.stats["to_trash"] is True and result.stats["restorable"] is True
    assert result.stats["entry_id"] == "t_1"
    assert result.stats["files"] == 1 and result.stats["bytes"] == len("hello\n")
    assert result.rollback_state.value == "available"


def test_delete_permanent_requires_approval():
    client = FakeClient({"a.py": "x\n"})
    pending = _run(_service(client).delete({"path": "a.py", "permanent": True}))
    assert pending.status is OperationStatus.PENDING_APPROVAL
    assert client.files["a.py"] == "x\n", "未审批不得永久删除"

    approved = _run(
        _service(client, approval_state=OperationApprovalState.APPROVED).delete(
            {"path": "a.py", "permanent": True}
        )
    )
    assert approved.status is OperationStatus.SUCCESS
    assert "a.py" not in client.files and not _trash_paths(client)
    assert approved.rollback_state.value == "unavailable"
    assert approved.stats["permanent"] is True and approved.stats["to_trash"] is False


def test_delete_missing_path_is_already_absent_and_dir_needs_recursive():
    client = FakeClient({"dir/a.py": "x\n"}, dirs={"dir"})
    service = _service(client, approval_state=OperationApprovalState.APPROVED)
    absent = _run(service.delete({"path": "nope.py"}))
    assert absent.status is OperationStatus.ALREADY_ABSENT and absent.error is None

    not_recursive = _run(service.delete({"path": "dir"}))
    assert not_recursive.error.code == OperationErrorCode.NOT_EMPTY_DIRECTORY.value
    assert client.files["dir/a.py"] == "x\n", "未递归时不得部分删除"

    recursive = _run(service.delete({"path": "dir", "recursive": True}))
    assert recursive.status is OperationStatus.SUCCESS
    assert not any(path.startswith("dir/") for path in client.files)
    assert recursive.stats["to_trash"] is True and recursive.stats["kind"] == "dir"


def test_delete_binary_file_goes_to_trash_without_reading_content():
    """rename 不读内容 ⇒ 二进制也能进回收站（§11.5 第三处对齐）。"""
    client = FakeClient({"img.png": "binary"}, binary={"img.png"})
    result = _run(_service(client).delete({"path": "img.png"}))
    assert result.status is OperationStatus.SUCCESS
    assert "img.png" not in client.files
    assert _trash_paths(client) == [".lumi_trash/t_1"]
    assert result.stats["to_trash"] is True


def test_trash_write_failure_never_reports_success():
    """回收站 rename 失败（TRASH_MOVE_FAILED）时绝不能报告删除成功。"""
    client = FakeClient({"a.py": "x\n"}, broken={"workspace_delete"})
    result = _run(_service(client).delete({"path": "a.py"}))
    assert result.status is OperationStatus.FAILED
    assert result.error.code == OperationErrorCode.DELETE_FAILED.value
    assert client.files["a.py"] == "x\n", "回收站失败时源文件必须还在"


def test_trash_list_and_restore_round_trip():
    client = FakeClient({"docs/a.md": "hello\n"})
    service = _service(client)
    deleted = _run(service.delete({"path": "docs/a.md"}))
    entry_id = deleted.stats["entry_id"]
    assert entry_id

    listing = _run(service.list_trash())
    assert listing.stats["count"] == 1
    assert listing.stats["entries"][0]["logical_path"] == "docs/a.md"
    assert listing.stats["retention_days"] == 7, "与客户端保留期一致"
    assert listing.stats["index"] == ".lumi_trash/trash.json"

    pending = _run(service.restore({"entry_id": entry_id}))
    assert pending.status is OperationStatus.PENDING_APPROVAL, "恢复默认需要审批"

    restored = _run(
        _service(client, approval_state=OperationApprovalState.APPROVED).restore(
            {"entry_id": entry_id}
        )
    )
    assert restored.status is OperationStatus.SUCCESS
    assert client.files["docs/a.md"] == "hello\n"
    assert not _trash_paths(client), "恢复后回收站内容应被 rename 回原路径"
    # 恢复走的是 rename（不是读内容再写回）：二进制/目录同样安全
    assert any(call[0] == "move" for call in client.calls)


def test_trash_quota_is_enforced_by_the_client_not_the_gateway():
    """配额由客户端维护（上限 500 条）——后端只如实透传客户端的结构化结论。

    这里验证网关不再自己记账：连续删除都成功，索引条目数等于删除次数。
    """
    client = FakeClient({"a.py": "x", "b.py": "y"})
    service = _service(client)
    assert _run(service.delete({"path": "a.py"})).status is OperationStatus.SUCCESS
    assert _run(service.delete({"path": "b.py"})).status is OperationStatus.SUCCESS
    listing = _run(service.list_trash())
    assert listing.stats["count"] == 2
    assert listing.stats["max_items"] == 500, "与客户端上限一致"


def test_purge_requires_approval_and_removes_content():
    client = FakeClient({"a.py": "x\n"})
    service = _service(client)
    deleted = _run(service.delete({"path": "a.py"}))
    assert deleted.status is OperationStatus.SUCCESS

    pending = _run(service.purge({"entry_ids": [deleted.stats["entry_id"]]}))
    assert pending.status is OperationStatus.PENDING_APPROVAL
    assert _trash_paths(client), "未审批不得清理"

    purged = _run(
        _service(client, approval_state=OperationApprovalState.APPROVED).purge(
            {"entry_ids": [deleted.stats["entry_id"]]}
        )
    )
    assert purged.status is OperationStatus.SUCCESS
    assert not _trash_paths(client)
    assert client._trash_entries() == [], "索引也要同步清掉"  # noqa: SLF001


# ── 6. 幂等与断线 ────────────────────────────────────────────────


def test_same_idempotency_key_does_not_write_twice():
    client = FakeClient()
    service = _service(client, idempotency_key="key-1")
    first = _run(service.execute(OperationKind.WRITE, {"path": "a.py", "content": "1\n"}))
    second = _run(service.execute(OperationKind.WRITE, {"path": "a.py", "content": "1\n"}))
    assert first.status is OperationStatus.SUCCESS
    assert second.meta.get("idempotent_replay") is True
    assert len([call for call in client.calls if call[0] == "write_text"]) == 1


def test_provider_missing_tool_surfaces_structured_error():
    client = FakeClient(tools={"workspace_list", "workspace_read"})
    result = _run(_service(client).write({"path": "a.py", "content": "x\n"}))
    assert result.status is OperationStatus.FAILED
    assert result.error.code == OperationErrorCode.NOT_SUPPORTED_BY_PROVIDER.value


def test_tool_output_metadata_carries_audit_fields():
    client = FakeClient()
    result = _run(
        _service(client, idempotency_key="k1").write({"path": "a.py", "content": "x\n"})
    )
    metadata = result.to_tool_output(tool_name="workspace_write").metadata
    for field in (
        "operation",
        "logical_path",
        "new_revision",
        "approval_state",
        "rollback_state",
        "changed_files",
        "idempotency_key",
    ):
        assert field in metadata, field
    assert metadata["idempotency_key"] == "k1"
    assert metadata["workspace_id"] == "ws-1"


@pytest.mark.parametrize("kind", ["write", "edit", "move", "delete"])
def test_execute_dispatches_every_operation_kind(kind):
    client = FakeClient({"a.py": "A\n"})
    service = _service(client, approval_state=OperationApprovalState.APPROVED)
    args = {
        "write": {"path": "new.py", "content": "N\n"},
        "edit": {
            "path": "a.py",
            "old_str": "A",
            "new_str": "B",
            "expected_revision": revision_for_text("A\n"),
        },
        "move": {"source_path": "a.py", "target_path": "b.py"},
        "delete": {"path": "a.py"},
    }[kind]
    result = _run(service.execute(kind, args))
    assert result.kind is OperationKind(kind)
    assert result.status is OperationStatus.SUCCESS


class _FakeNavigator:
    """替身 WorkspaceNavigatorService：只记录客户端工具调用。"""

    workspace_id = "ws-1"
    user_id = "u1"
    user_role = "user"
    conversation_id = "conv-1"

    def __init__(self, payloads: dict[str, dict]) -> None:
        self.payloads = payloads
        self.calls: list[tuple[str, dict]] = []

    async def advertised_tools(self):
        return [{"name": name} for name in self.payloads]

    async def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, dict(args)))
        return self.payloads.get(tool, {"success": True, "data": {}})

    def resolve_error(self, payload, **_kwargs):  # noqa: ANN001
        return None


def test_navigator_client_uses_the_client_atomic_tool_names():
    """生产适配层必须调用客户端**真实存在**的工具名与参数（§11.5）。"""
    from app.workspace.write.operations import NavigatorWorkspaceClient

    payloads = {
        "workspace_move": {"success": True, "data": {"from": "a.py", "to": "b.py", "atomic": True}},
        "workspace_delete": {
            "success": True,
            "data": {"rel": "a.py", "trash_id": "t_1", "reversible": True, "permanent": False},
        },
    }
    navigator = _FakeNavigator(payloads)
    client = NavigatorWorkspaceClient(navigator)

    assert asyncio.run(client.move("a.py", "b.py"))["success"] is True
    assert navigator.calls[-1] == (
        "workspace_move",
        {"workspace_id": "ws-1", "from": "a.py", "to": "b.py"},
    )

    assert asyncio.run(client.delete("a.py", recursive=True, permanent=True))["success"] is True
    tool, args = navigator.calls[-1]
    assert tool == "workspace_delete"
    assert args["path"] == "a.py" and args["recursive"] is True and args["permanent"] is True
