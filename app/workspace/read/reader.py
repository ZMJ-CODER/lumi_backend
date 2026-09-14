"""统一工作区读取服务（后端侧 `workspace_read` 的实现）。

模型/编排只需要一个读取能力：``workspace_read``。目录枚举、文件类型识别、
解析器选择（PPTX/DOCX/PDF/XLSX/CSV/图片 OCR/压缩包/文本）、内容筛选与分页
全部在本服务内部完成；原始二进制/压缩包/XML/乱码永不进入模型上下文。

输入（模型可见参数尽量少）::

    {"request": "读取这份 PPT 并回答用户的问题", "path": "可选",
     "cursor": "可选", "max_chars": 12000}

输出统一结构（与前端/模型契约一致）::

    {"status": "success|partial|failed|empty", "summary": "...",
     "content": [{"source": "...", "location": "slide-3", "title": "...", "text": "..."}],
     "has_more": false, "cursor": null,
     "meta": {"format": "pptx", "parser": "pptx", "encoding": null, "workspace_version": 3}}
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

DEFAULT_MAX_CHARS = 12000
_CURSOR_TTL_SECONDS = 900
_CURSOR_STORE: dict[str, dict] = {}

# 解析器优先级：统一内容提取 > 文本读取 > 结构化读取。
_EXTRACT_TOOLS = ("workspace_content_extract", "workspace_read_structured", "workspace_read")
_CATALOG_TOOLS = ("workspace_catalog", "workspace_list")
_SEARCH_TOOLS = ("workspace_search",)

_EXT_TO_FORMAT = {
    ".pptx": "pptx", ".ppt": "ppt", ".docx": "docx", ".doc": "doc", ".pdf": "pdf",
    ".xlsx": "xlsx", ".xls": "xls", ".csv": "csv", ".md": "markdown", ".txt": "text",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".py": "text", ".ts": "text",
    ".tsx": "text", ".js": "text", ".html": "html", ".zip": "archive",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
}
_SECTION_KEYS = (
    ("slides", "slide"), ("pages", "page"), ("paragraphs", "paragraph"),
    ("sheets", "sheet"), ("rows", "row"), ("blocks", "block"), ("ocr_blocks", "ocr-block"),
    ("lines", "line"), ("sections", "section"), ("items", "item"),
)
_GARBAGE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ZIP_MAGIC = ("pk\x03\x04", "PK\x03\x04")


@dataclass(slots=True)
class UnifiedSection:
    source: str
    location: str
    title: str
    text: str

    def to_dict(self) -> dict:
        return {
            "source": self.source[:300],
            "location": self.location[:120],
            "title": self.title[:200],
            "text": self.text,
        }


@dataclass(slots=True)
class ReadOutcome:
    status: str = "success"
    summary: str = ""
    content: list[dict] = field(default_factory=list)
    has_more: bool = False
    cursor: str | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "summary": self.summary,
            "content": list(self.content),
            "has_more": bool(self.has_more),
            "cursor": self.cursor,
            "meta": dict(self.meta),
        }


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z0-9_.-]{2,}", str(text or "")))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", str(text or "")):
        tokens.update(chunk[i:i + 2] for i in range(len(chunk) - 1))
    return {token.casefold() for token in tokens}


def _payload_remote_cursor(payload: Any) -> str:
    """取远端解析器自带的 cursor（信封或 meta 里）。"""
    if not isinstance(payload, dict):
        return ""
    for source in (payload, payload.get("meta")):
        if isinstance(source, dict) and source.get("cursor"):
            return str(source["cursor"])
    return ""


def _is_garbage(text: str) -> bool:
    raw = str(text or "")
    if not raw.strip():
        return True
    if any(raw.startswith(magic) for magic in _ZIP_MAGIC):
        return True
    control = len(_GARBAGE_RE.findall(raw))
    if control / max(1, len(raw)) > 0.05:
        return True
    return False


def _strip_markup(text: str) -> str:
    """去掉 XML/HTML 标签与压缩/二进制残留，只保留可读文本。"""
    cleaned = str(text or "")
    for _ in range(4):
        stripped = re.sub(r"<[^<>]{1,400}>", " ", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = (
        cleaned.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&nbsp;", " ")
    )
    return cleaned


def _clean_text(text: str) -> str:
    return _GARBAGE_RE.sub("", str(text or "")).replace("\r\n", "\n").strip()


def _format_for(path: str) -> str:
    lowered = str(path or "").casefold()
    for ext, fmt in _EXT_TO_FORMAT.items():
        if lowered.endswith(ext):
            return fmt
    return "text"


class WorkspaceReader:
    """一个工作区范围内的统一读取器（无状态实例，游标存进程内 TTL 缓存）。"""

    def __init__(
        self,
        *,
        user_id: str,
        user_role: str = "user",
        workspace_id: str,
        conversation_id: str = "",
    ) -> None:
        self.user_id = user_id
        self.user_role = user_role
        self.workspace_id = str(workspace_id or "").strip()
        self.conversation_id = conversation_id

    # ── 对外入口 ──────────────────────────────────────────────

    async def read(
        self,
        request: str,
        *,
        path: str = "",
        cursor: str = "",
        max_chars: int = DEFAULT_MAX_CHARS,
    ) -> dict:
        """读取并按统一结构返回；任何异常都收敛为 failed/empty，不抛给上层。"""
        if not self.workspace_id:
            return ReadOutcome(
                status="failed",
                summary="当前对话没有绑定工作区，无法读取本地文件。",
                meta={"error_code": "WORKSPACE_NOT_BOUND"},
            ).to_dict()

        route = self._route()
        if route["status_code"] != "WORKSPACE_READY" or not route.get("server_name"):
            code = str(route.get("status_code") or "WORKSPACE_NOT_REGISTERED")
            return ReadOutcome(
                status="failed",
                summary=self._status_message(code),
                meta={"error_code": code},
            ).to_dict()

        if cursor:
            return await self._continue(cursor, max_chars=max_chars)

        try:
            self._advertised = await self._list_tools(route["server_name"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace_read 列出桌面工具失败: {}", str(exc)[:160])
            self._advertised = []

        targets = await self._select_targets(request, path=path)
        if not targets:
            return ReadOutcome(
                status="empty",
                summary="工作区里没有找到与问题相关的可读文件；请说明具体文件名或路径。",
                meta={"format": None, "parser": None, "encoding": None, "workspace_version": None},
            ).to_dict()

        sections: list[UnifiedSection] = []
        parser_used = ""
        workspace_version: Any = None
        failed_reason = ""
        remote_cursor = ""
        remote_has_more = False
        for target in targets:
            extracted = await self._extract(target, route)
            if extracted is None:
                failed_reason = failed_reason or "读取失败"
                continue
            payload, parser_name = extracted
            parser_used = parser_used or parser_name
            workspace_version = workspace_version or self._workspace_version(payload)
            # 远端解析器自己的分页状态：必须原样保留并随游标回传，否则续读会
            # 重新请求首批（重复）或永远拿不到后续（丢尾部）。
            if not remote_cursor:
                remote_cursor = _payload_remote_cursor(payload)
                remote_has_more = bool(
                    payload.get("has_more") or (payload.get("meta") or {}).get("has_more")
                )
            sections.extend(self._normalize(target, payload))
        if not sections and not remote_has_more:
            return ReadOutcome(
                status="failed",
                summary=failed_reason or "未能从目标文件中提取到可读正文。",
                meta={"format": None, "parser": parser_used or None, "encoding": None,
                      "workspace_version": workspace_version},
            ).to_dict()
        # 远端解析器返回了 cursor（即使 JSON 里没有 sections）→ 分页事实由它决定：
        # 本地已经发完的段照发，剩余部分用远端 cursor 继续拉取。
        if remote_cursor:
            page = self._paginate(
                sections,
                request=request,
                targets=targets,
                parser_used=parser_used,
                workspace_version=workspace_version,
                max_chars=max_chars,
                remote_cursor=remote_cursor,
            )
            if not page.get("has_more") or not page.get("cursor"):
                return page
            # 还没读完：在这个后端游标里记住远端 cursor，续读时回传。
            state = _CURSOR_STORE.get(page["cursor"])
            if state is not None:
                state["sections"] = [item.to_dict() for item in sections]
                state["emitted_sections"] = len(sections)
                state["emitted_offset"] = 0
                state["remote_cursor"] = remote_cursor
                state["targets"] = targets
            return page

        if not sections and remote_has_more:
            # 远端说还有内容但没有正文：给一个可继续的游标，不要谎报"读完"。
            cursor_id = uuid.uuid4().hex
            _CURSOR_STORE[cursor_id] = {
                "created_at": time.time(),
                "sections": [],
                "emitted_sections": 0,
                "emitted_offset": 0,
                "remote_cursor": remote_cursor,
                "request": request,
                "targets": targets,
                "parser": parser_used,
                "workspace_version": workspace_version,
            }
            return ReadOutcome(
                status="partial",
                summary="远端解析器还有后续内容，可继续读取。",
                content=[],
                has_more=True,
                cursor=cursor_id,
                meta={"format": None, "parser": parser_used or None, "encoding": None,
                      "workspace_version": workspace_version},
            ).to_dict()

        return self._paginate(
            sections,
            request=request,
            targets=targets,
            parser_used=parser_used,
            workspace_version=workspace_version,
            max_chars=max_chars,
            remote_cursor=remote_cursor,
        )

    # ── 内部：路由 / 工具发现 / 目标选择 ───────────────────────

    def _route(self) -> dict:
        from app.workspace.context import resolve_workspace_desktop

        try:
            return resolve_workspace_desktop(self.user_id, self.workspace_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace_read 解析桌面路由失败: {}", str(exc)[:160])
            return {"status_code": "WORKSPACE_NOT_REGISTERED", "server_name": ""}

    @staticmethod
    async def _list_tools(server_name: str) -> list[dict]:
        from app.agents.mcp.manager import list_tools

        return [item for item in await list_tools(server_name) if isinstance(item, dict)]

    async def _select_targets(self, request: str, *, path: str) -> list[dict]:
        """返回待读取文件 [{path, name}]；支持显式路径与自然语言定位。"""
        explicit = str(path or "").strip()
        entries = await self._catalog_entries()
        by_path = {str(item.get("path") or ""): item for item in entries}

        if explicit:
            if explicit in by_path:
                return [by_path[explicit]]
            return [{"path": explicit, "name": explicit}]

        # 请求里提到的文件名优先（“读取答辩 PPT” → 答辩.pptx）
        tokens = _tokenize(request)
        scored: list[tuple[float, dict]] = []
        for item in entries:
            name = str(item.get("path") or item.get("name") or "")
            kind = str(item.get("type") or "")
            if kind in {"directory", "dir", "folder"}:
                continue
            name_tokens = _tokenize(name)
            overlap = len(tokens & name_tokens)
            score = float(overlap)
            if overlap == 0 and any(token and token in name.casefold() for token in tokens):
                score = 0.5
            if score > 0:
                scored.append((score, {"path": name, "name": name}))
        if scored:
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [item for _score, item in scored[:2]]

        # 没有文件名线索：先在工作区内检索，再读取命中的文件。
        hits = await self._search(request)
        if hits:
            return hits[:2]
        # 最后才退化为“读取工作区内少量文件”，但绝不批量倾倒全部文件。
        return [
            {"path": str(item.get("path") or ""), "name": str(item.get("path") or "")}
            for item in entries
            if str(item.get("type") or "") not in {"directory", "dir", "folder"}
        ][:1]

    async def _catalog_entries(self) -> list[dict]:
        tool = self._pick_tool(_CATALOG_TOOLS)
        if not tool:
            return []
        args = {"workspace_id": self.workspace_id}
        if tool != "workspace_catalog":
            args["path"] = ""
        payload = await self._call(tool, args)
        data = self._payload(payload)
        if isinstance(data, dict):
            for key in ("entries", "top_level", "items", "files", "children"):
                value = data.get(key)
                if isinstance(value, list):
                    data = value
                    break
        if not isinstance(data, list):
            return []
        entries: list[dict] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("path") or item.get("name") or "").strip().strip("/")
            if not name:
                continue
            entries.append({
                "path": name,
                "name": name,
                "type": str(item.get("type") or item.get("kind") or "file"),
            })
        return entries

    async def _search(self, request: str) -> list[dict]:
        tool = self._pick_tool(_SEARCH_TOOLS)
        if not tool:
            return []
        payload = await self._call(tool, {"workspace_id": self.workspace_id, "query": request})
        data = self._payload(payload)
        hits: list[dict] = []
        if isinstance(data, dict):
            for key in ("matches", "results", "items", "files"):
                value = data.get(key)
                if isinstance(value, list):
                    data = value
                    break
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("path") or item.get("file") or item.get("name") or "").strip()
                if name:
                    hits.append({"path": name, "name": name})
        return hits

    # ── 内部：解析与归一化 ────────────────────────────────────

    def _pick_tool(self, candidates: tuple[str, ...]) -> str:
        names = {str(item.get("name") or "") for item in getattr(self, "_advertised", [])}
        for candidate in candidates:
            if candidate in names:
                return candidate
        return ""

    async def _extract(
        self, target: dict, route: dict, *, remote_cursor: str = ""
    ) -> tuple[Any, str] | None:
        tool = self._pick_tool(_EXTRACT_TOOLS)
        if not tool:
            return None
        path = str(target.get("path") or "")
        call_args: dict = {"workspace_id": self.workspace_id, "path": path}
        if remote_cursor:
            # 远端解析器自带的分页 cursor：原样回传，让远端从上次位置继续。
            call_args["cursor"] = remote_cursor
        payload = await self._call(tool, call_args)
        if payload is None or str(payload.get("status") or "").lower() in {"failed", "cancelled"}:
            # 统一内容提取不可用时，降级为文本读取（仅对文本类文件有效）。
            if tool != "workspace_read" and "workspace_read" in {
                str(item.get("name") or "") for item in getattr(self, "_advertised", [])
            }:
                payload = await self._call(
                    "workspace_read", dict(call_args)
                )
                tool = "workspace_read"
        if payload is None:
            return None
        return payload, tool

    async def _call(self, tool: str, args: dict) -> dict | None:
        from app.agents.mcp.manager import call_tool

        route = self._route()
        server_name = str(route.get("server_name") or "")
        if not server_name:
            return None
        try:
            result = await call_tool(
                server_name,
                tool,
                dict(args),
                task_id=self.conversation_id or None,
                user_id=self.user_id,
                device_id=str(route.get("device_id") or ""),
                workspace_id=self.workspace_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("workspace_read 调用 {} 失败: {}", tool, str(exc)[:160])
            return None
        return result if isinstance(result, dict) else None

    @staticmethod
    def _payload(payload: dict | None) -> Any:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data")
        return data if data is not None else payload

    def _normalize(self, target: dict, payload: Any) -> list[UnifiedSection]:
        source = str(target.get("name") or target.get("path") or "workspace")
        data = self._payload(payload)
        sections: list[UnifiedSection] = []

        def add(location: str, title: str, text: str) -> None:
            cleaned = _clean_text(_strip_markup(text))
            if not cleaned or _is_garbage(cleaned):
                return
            sections.append(UnifiedSection(
                source=source, location=location, title=title or location, text=cleaned,
            ))

        if isinstance(data, list):
            for index, item in enumerate(data, 1):
                if isinstance(item, dict):
                    add(str(item.get("location") or f"block-{index}"),
                        str(item.get("title") or ""),
                        str(item.get("text") or item.get("content") or ""))
                else:
                    add(f"block-{index}", "", str(item))
            return sections

        if not isinstance(data, dict):
            add("block-1", "", str(data or ""))
            return sections

        for key, label in _SECTION_KEYS:
            value = data.get(key)
            if not isinstance(value, list):
                continue
            for index, item in enumerate(value, 1):
                if isinstance(item, dict):
                    location = str(item.get("location") or f"{label}-{index}")
                    title = str(item.get("title") or item.get("name") or item.get("sheet") or "")
                    text = str(item.get("text") or item.get("content") or item.get("value") or "")
                    add(location, title, text)
                else:
                    add(f"{label}-{index}", "", str(item))
            if sections:
                return sections

        for key in ("text", "content", "markdown", "output"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                lines = [line for line in value.splitlines()]
                chunk: list[str] = []
                for line in lines:
                    chunk.append(line)
                    if len("\n".join(chunk)) >= 1200:
                        add(f"line-{len(sections) * 40 + 1}", "", "\n".join(chunk))
                        chunk = []
                if chunk:
                    add(f"line-{len(sections) * 40 + 1}", "", "\n".join(chunk))
                return sections
        return sections

    @staticmethod
    def _workspace_version(payload: Any) -> Any:
        if isinstance(payload, dict):
            meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            for key in ("workspace_version", "version", "base_version"):
                if meta.get(key) is not None:
                    return meta[key]
                if payload.get(key) is not None:
                    return payload[key]
        return None

    # ── 内部：预算与游标分页 ─────────────────────────────────

    def _paginate(
        self,
        sections: list[UnifiedSection],
        *,
        request: str,
        targets: list[dict],
        parser_used: str,
        workspace_version: Any,
        max_chars: int,
        remote_cursor: str = "",
        start_section: int = 0,
        start_offset: int = 0,
    ) -> dict:
        # 续读起点：只对起点那一段应用段内偏移。
        default_start = max(0, int(start_section or 0))
        budget = max(500, int(max_chars or DEFAULT_MAX_CHARS))
        kept: list[UnifiedSection] = []
        used = 0
        # 起点：第 start_section 段的第 start_offset 个字符（续读时指向上次截断处）。
        start_section = max(0, int(start_section or 0))
        start_offset = max(0, int(start_offset or 0))
        emitted_sections = start_section
        emitted_offset = 0
        for index in range(start_section, len(sections)):
            if used >= budget:
                break
            section = sections[index]
            text = section.text
            if index == default_start and start_offset > 0:
                text = text[start_offset:]
                # 起点段必须先切片：否则截断时会用原始全量文本，
                # 续读页会把上次已经发过的部分再发一遍（页面重叠）。
                section = UnifiedSection(section.source, section.location, section.title, text)
            remaining = budget - used
            if len(text) > remaining:
                kept.append(UnifiedSection(
                    section.source, section.location, section.title,
                    text[:remaining] + "\n…（本段已截断，可用 cursor 继续）",
                ))
                # 这一页在这里结束：该段只发出了 remaining 个字符。
                emitted_sections = index
                emitted_offset = (start_offset if index == default_start else 0) + remaining
                break
            kept.append(UnifiedSection(section.source, section.location, section.title, text))
            used += len(section.text)
            emitted_sections = index + 1
            emitted_offset = 0

        # 还有后续 section、某段只发了一部分、或远端解析器自己还有 cursor
        # → 都需要给调用方一个可继续的后端游标。
        has_more = emitted_sections < len(sections) or emitted_offset > 0 or bool(remote_cursor)
        cursor: str | None = None
        if has_more:
            cursor = uuid.uuid4().hex
            _CURSOR_STORE[cursor] = {
                "created_at": time.time(),
                # 存**原文**副本：续读时按 (段号, 段内偏移) 切片，
                # 用打过"已截断"标记的文本切片会算错偏移、重复内容。
                "sections": [item.to_dict() for item in sections],
                "emitted_sections": emitted_sections,
                "emitted_offset": emitted_offset,
                # 远端解析器自己的 cursor 必须原样保留，续读时回传，
                # 否则远端分页状态在后端重包装后丢失（重复读或读不到）。
                "remote_cursor": remote_cursor,
                "request": request,
                "targets": targets,
                "parser": parser_used,
                "workspace_version": workspace_version,
            }
        first = kept[0] if kept else None
        summary = (
            f"已读取 {first.source}（{len(kept)}/{len(sections)} 段，约 {used} 字符）"
            if first else "未提取到可读正文"
        )
        if has_more:
            summary += "；内容较长，可继续读取后续部分。"
        return ReadOutcome(
            status="partial" if has_more else "success",
            summary=summary,
            content=[item.to_dict() for item in kept],
            has_more=has_more,
            cursor=cursor,
            meta={
                "format": _format_for(first.source) if first else None,
                "parser": parser_used or None,
                "encoding": None,
                "workspace_version": workspace_version,
            },
        ).to_dict()

    async def _continue(self, cursor: str, *, max_chars: int) -> dict:
        state = _CURSOR_STORE.get(cursor)
        if not state:
            return ReadOutcome(
                status="failed",
                summary="继续读取的游标已过期，请重新发起读取。",
                meta={"error_code": "CURSOR_EXPIRED"},
            ).to_dict()
        sections = [UnifiedSection(**item) for item in state.get("sections") or []]
        emitted = int(state.get("emitted_sections") or 0)
        emitted_offset = int(state.get("emitted_offset") or 0)
        remote_cursor = str(state.get("remote_cursor") or "")
        targets = list(state.get("targets") or [])
        parser_used = str(state.get("parser") or "")
        workspace_version = state.get("workspace_version")

        if emitted >= len(sections) and emitted_offset <= 0 and not remote_cursor:
            return ReadOutcome(
                status="success",
                summary="已读取完全部内容。",
                content=[],
                has_more=False,
                meta={"parser": parser_used, "workspace_version": workspace_version},
            ).to_dict()

        # 远端还有后续：用远端 cursor 再拉一批，追加到已有段后面。
        if remote_cursor and emitted >= len(sections):
            try:
                self._advertised = await self._list_tools(self._route()["server_name"])
            except Exception:  # noqa: BLE001
                self._advertised = []
            target = targets[0] if targets else {"path": str(state.get("path") or "")}
            extracted = await self._extract(target, self._route(), remote_cursor=remote_cursor)
            if extracted is not None:
                payload, parser_name = extracted
                parser_used = parser_used or parser_name
                workspace_version = workspace_version or self._workspace_version(payload)
                extra = self._normalize(target, payload)
                if extra:
                    sections = [*sections, *extra]
                new_remote = _payload_remote_cursor(payload) or remote_cursor
                remote_cursor = new_remote if payload.get("has_more") else ""

        result = self._paginate(
            sections,
            request=str(state.get("request") or "继续读取"),
            targets=targets,
            parser_used=parser_used,
            workspace_version=workspace_version,
            max_chars=max_chars,
            remote_cursor=remote_cursor,
            start_section=emitted,
            start_offset=emitted_offset,
        )
        # 续读不再重写游标：内层 _paginate 生成的新游标已经指向"这次剩下部分"的
        # 正确位置（offset/partial_offset/远端 cursor 都在内层算好了）。之前这里
        # 用旧 state 覆盖 inner 的 sections，会把远端 cursor 与段内偏移一起丢掉。
        _prune_cursors()
        return result

    @staticmethod
    def _status_message(code: str) -> str:
        from app.workspace.context import describe_status

        return describe_status(code, workspace_id="")


def _prune_cursors() -> None:
    now = time.time()
    for key, state in list(_CURSOR_STORE.items()):
        if now - float(state.get("created_at") or 0) > _CURSOR_TTL_SECONDS:
            _CURSOR_STORE.pop(key, None)


def unified_payload_to_text(payload: dict, *, limit: int = 20000) -> str:
    """把统一结构渲染为可注入模型的上下文文本；无正文时返回空串。

    无正文（failed/empty）必须返回空串，让调用方走“无法读取”的降级路径，
    而不是把摘要当成资料注入模型。
    """
    if not isinstance(payload, dict):
        return ""
    content = [item for item in (payload.get("content") or []) if isinstance(item, dict)]
    if not content:
        return ""
    lines: list[str] = []
    summary = str(payload.get("summary") or "")
    if summary:
        lines.append(f"[读取摘要] {summary}")
    for item in content:
        header = f"[{item.get('source')} · {item.get('location')}]"
        title = str(item.get("title") or "")
        if title:
            header += f" {title}"
        lines.append(header)
        lines.append(str(item.get("text") or ""))
    if payload.get("has_more"):
        lines.append(f"（内容未读完，可用 cursor 继续：{payload.get('cursor')}）")
    return "\n".join(lines)[:limit]


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "DEFAULT_MAX_CHARS",
    "ReadOutcome",
    "UnifiedSection",
    "WorkspaceReader",
    "json_dumps",
    "unified_payload_to_text",
]
