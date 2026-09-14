"""ReAct 的**工具发现与候选选择**：名字判定、调用去重键、L2 检索、领域申请。

这一层回答两类问题：

* **"这个名字到底指哪个工具、能不能改"**：``_impl_name`` / ``_is_read_tool`` /
  ``_requires_prior_read`` / 去重键。**所有基于名字的判断都必须走实现名**——
  模型可见面收敛后模型会说 ``Write``，而护栏、失败排除、去重键都建立在
  ``workspace_write`` 上（见 ``_impl_name`` 的说明）。
* **"这一轮有哪些工具可发现"**：L2 会话级检索（``_search_tools``）与
  模型主动申请领域（``_request_domain``）。领域申请只改"边界"，不执行任何操作。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.agents.skills.discovery import search_tools


class ToolSelectionMixin:
    """工具名判定 + 发现/领域申请（混入 ``OfficeReactRunner``；不定义 ``__init__``）。"""

    @staticmethod
    def _call_key(name: str, args: dict) -> str:
        """(工具名, 参数) 的去重键：同一组合连续失败两次就不再重试。"""
        raw = json.dumps(
            {"name": name, "args": args if isinstance(args, dict) else {}},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _impl_name(self, name: str) -> str:
        """模型可见名 → 实现名（收敛关闭、或不是收敛名时**恒等**）。

        所有基于名字的判断都必须用它的返回值：前置读取护栏、失败方法排除、
        调用去重键、工具构造。否则模型叫 ``Write`` 就会绕过针对
        ``workspace_write``/``workspace_edit`` 的安全护栏。
        """
        text = str(name or "")
        return self._surface_alias.get(text, text)

    @staticmethod
    def _is_read_tool(name: str) -> bool:
        return str(name or "").casefold() in {
            "read", "glob", "grep", "filestat", "openfile", "read_document", "office_doc_read",
            "get_project_context", "inspect_document_set",
            # workspace 通用读取域（内部原子能力 + 模型可见的聚合入口）
            "workspace_navigator",
            "workspace_read", "workspace_search", "workspace_list", "workspace_catalog",
        }

    @staticmethod
    def _requires_prior_read(name: str) -> bool:
        # Write may legitimately create a new file; in-place mutation and
        # destructive operations must be preceded by a read of the same
        # target. The client/server tool still performs its own path check.
        #
        # ``workspace_edit`` 必须前置读取：它的契约要求 expected_revision，
        # 而 revision 只能从 workspace_navigator(action=read) 拿到。
        return str(name or "").casefold() in {
            "edit", "delete", "rename", "office_doc_edit",
            "workspace_stage_write", "workspace_stage_delete",
            "workspace_edit",
        }

    @staticmethod
    def _target_key(name: str, args: dict) -> str:
        value = (args or {}).get("file_path") or (args or {}).get("path") or (args or {}).get("doc_id") or (args or {}).get("target_file")
        return f"{name.casefold()}:{str(value or '').replace(chr(92), '/')}"

    @staticmethod
    def _read_target_key(args: dict) -> str:
        value = (args or {}).get("file_path") or (args or {}).get("path") or (args or {}).get("doc_id") or (args or {}).get("target_file")
        return str(value or "").replace("\\", "/").strip().casefold()

    async def _search_tools(self, query: str) -> str:
        """L2 工具发现：只在当前办公场景的合法能力中检索。"""
        from app.agents.skills.executor import get_capabilities_for_scene

        legal = await get_capabilities_for_scene("office", self.user_role, self.user_id)
        found = search_tools(query, legal, limit=5, allowed_tools={item.name for item in legal})
        self.discovery_session.discovered_domains.update(item.domain for item in found if item.domain)
        self.discovery_session.add(found)
        return "已发现工具：" + ", ".join(item.name for item in found) if found else "未发现匹配工具"

    async def _request_domain(self, domain: str, reason: str = "", mode: str = "read_only") -> str:
        """Authorize a model-requested domain without executing a tool.

        Domain names are normalized here, while authorization is performed by
        loading the already-filtered scene capabilities.  The model can ask
        for a domain, but cannot grant itself a tool or a write permission.
        """
        aliases = {
            "network": "research", "web": "research", "联网": "research", "网络": "research",
            "knowledge": "research", "知识": "research", "research": "research",
            "file": "document", "files": "document", "文档": "document", "文件": "document",
            "document": "document", "data": "data", "数据": "data",
            "code": "development", "development": "development", "代码": "development",
            "system": "system", "命令": "system", "desktop": "desktop", "桌面": "desktop",
            "schedule": "schedule", "日程": "schedule", "communication": "communication",
            "writing": "writing", "输出": "writing",
        }
        normalized = aliases.get(str(domain or "").strip().casefold(), str(domain or "").strip().casefold())
        if normalized not in {"research", "document", "data", "development", "system", "desktop", "schedule", "communication", "writing"}:
            return f"无法识别领域：{normalized or '（空）'}。请从 network/document/data/development/system 等领域中选择。"
        from app.agents.skills.executor import get_capabilities_for_scene

        legal = await get_capabilities_for_scene("office", self.user_role, self.user_id)
        authorized = [item for item in legal if str(item.domain or item.category or "").casefold() == normalized]
        # A read-only domain request must not widen into write capabilities.
        # Write tools are injected only when the stage explicitly asks for
        # write mode; normal approval/effect-journal gates still apply then.
        if str(mode or "read_only").casefold() != "write":
            authorized = [item for item in authorized if not item.write_op and not item.requires_confirmation]
        if normalized not in self._requested_domains and len(self._domain_history) >= self.max_domain_transitions:
            return "本任务已达到领域切换上限。请基于当前已授权领域完成任务，或先向用户说明需要继续扩展范围。"
        self._requested_domains.add(normalized)
        if normalized != self._active_domain:
            self._domain_history.append(normalized)
        self._active_domain = normalized
        self._domain_mode = str(mode or self._domain_mode or "read_only").casefold()
        self._emit({"type": "domain", "domain": normalized, "mode": self._domain_mode, "reason": str(reason or "")[:240]})
        self.discovery_session.discovered_domains.add(normalized)
        self.discovery_session.add(authorized)
        await self.discovery_session.save(self.user_id, self.job_id)
        names = ", ".join(item.name for item in authorized[:12])
        if not authorized:
            return f"未授权或不存在该领域：{normalized}。请改申请其他领域或向用户澄清。"
        return f"已授权进入 {normalized} 域（模式：{mode or 'read_only'}），下一轮可用工具：{names}。原因已记录。"

    #: 由 runner 的 ``__init__`` 赋值（这里只做类型提示）。
    _surface_alias: dict[str, str]
    discovery_session: Any
    action_intents: tuple[str, ...]


__all__ = ["ToolSelectionMixin"]
