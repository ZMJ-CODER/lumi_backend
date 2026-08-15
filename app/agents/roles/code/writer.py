"""编码 agent：基于指令与代码上下文生成/修改本地文件并写回项目."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress as _report_progress
from app.agents.core.patch import parse_search_replace
from app.agents.core.tools import (
    CODE_SYSTEM_PROMPT,
    FIX_SYSTEM_PROMPT,
    _looks_like_new_file,
    _read_with_retry,
    _step_title,
    effort_start_for_task,
    generate_code_content,
    list_project_files,
    locate_from_memory,
    locate_project_file,
)

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode


def _format_patch_context(blocks: list, imports: list, total: int) -> str:
    """把客户端提取的代码块 + 引用导入格式化为补丁上下文（行号 1 起始）."""
    parts = [f"文件共 {total} 行，下面是相关代码块（行号基于原文件，1 起始）："]
    for b in blocks:
        start = int(b.get("start_line", 0)) + 1
        end = int(b.get("end_line", 0)) + 1
        parts.append(
            f"\n### {b.get('name') or '<匿名>'}(第 {start}-{end} 行)\n{b.get('text') or ''}"
        )
    if imports:
        parts.append("\n### 文件头部引用/导入\n" + "\n".join(imports[:30]))
    return "\n".join(parts)


class CodeWriterAgent(WorkerAgent):
    """编码 agent：基于指令与代码上下文生成/修改本地文件并写回项目."""

    name = "code_writer"
    description = "根据指令生成或修改本地代码文件内容并写回项目"
    params_help = (
        'params 用 {"project_id": "项目ID", "instruction": "编码指令", '
        '"target_file": "可选文件路径", "original_content": "可选，来自 reader"}'
    )
    skills = [
        "read_project_file",
        "write_project_file",
        "delete_project_file",
        "rename_project_file",
        "grep_code",
        "extract_code_blocks",
        "apply_patch",
        "run_static_check",
    ]

    _SYSTEM_PROMPT = CODE_SYSTEM_PROMPT

    @staticmethod
    def _has_static_feedback(instruction: str) -> bool:
        """判断打回反馈是否包含静态检查报错（走轻量修复而不是全量重生成）."""
        return any(
            k in (instruction or "")
            for k in ("运行报错日志", "静态类型检查未通过", "静态检查")
        )

    async def _try_static_fix(
        self,
        ctx: WorkerContext,
        node: TaskNode,
        project_id: str,
        instruction: str,
        target_path,
        target_key,
        path_label: str,
    ) -> dict | None:
        """分层重试第 2 层：静态检查失败时，让模型只修报错附近（SEARCH/REPLACE 小补丁）.

        Returns 修复成功的结果；不适用/修复失败返回 None（走全量重生成）。
        """
        static = await self.run_skill(
            "run_static_check", {"project_id": project_id}, ctx
        )
        if static.get("passed") is True:
            # 打回前其实已通过（可能上次修复已生效）：无需重生成
            return {
                "success": True,
                "content": f"静态检查已通过 → {path_label}",
                "path": path_label,
                "instruction": instruction,
                "fixed_by": "static-pass",
                "step_title": f"静态检查通过 {path_label}",
            }
        if static.get("passed") is not False:
            return None  # 无可用的静态检查器 → 正常流程
        errors = str(static.get("output") or "")[:3000]
        read = await self.run_skill(
            "read_project_file",
            {
                "project_id": project_id,
                "path": target_path or target_key,
                "max_chars": 120000,
            },
            ctx,
        )
        if not read.get("success"):
            return None
        content = str(read.get("content") or "")
        if not content.strip():
            return None
        context = f"## 静态检查报错\n{errors}\n\n## 出错文件 {path_label}\n```\n{content[:30000]}\n```"
        await _report_progress(ctx.job_id, node.id, f"正在修复 {path_label} 的静态错误…")
        patch_text = await generate_code_content(
            ctx,
            instruction,
            path_label,
            "",
            FIX_SYSTEM_PROMPT,
            reasoning_effort="low",
            patch_mode=True,
            context_override=context,
            force_full=True,
            no_reasoning_override=True,
        )
        blocks = parse_search_replace(patch_text or "")
        if not blocks:
            return None
        res = await self.run_skill(
            "apply_patch",
            {
                "project_id": project_id,
                "path": target_path or target_key,
                "blocks": blocks,
            },
            ctx,
        )
        if not res.get("success"):
            return None
        static2 = await self.run_skill(
            "run_static_check", {"project_id": project_id}, ctx
        )
        if static2.get("passed") is True:
            return {
                "success": True,
                "content": f"已用补丁修复静态错误 → {path_label}",
                "path": path_label,
                "instruction": instruction,
                "fixed_by": "light-fix",
                "staged": True,
                "step_title": f"修复静态检查错误 {path_label}",
            }
        return None

    async def _retry_locate_real_file(
        self,
        ctx: WorkerContext,
        project_id: str,
        target_path,
        target_key,
    ) -> str | None:
        """目标文件在本机不存在（索引过期/路径变化）时，列出真实目录按文件名匹配重定位.

        Returns 修正后的相对路径；找不到返回 None。
        """
        rel = str(target_path or target_key or "")
        if not rel:
            return None
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        name = rel.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if not stem:
            return None
        try:
            res = await self.run_skill(
                "list_project",
                {"project_id": project_id, "path": parent, "include_hidden": False},
                ctx,
            )
        except Exception:  # noqa: BLE001
            return None
        if not res.get("success"):
            return None
        entries: list[tuple[str, bool]] = []
        for line in str(res.get("content") or "").splitlines():
            m = re.match(r"^\[(目录|文件)\]\s+(.+?)(?:\s+\(\d+ 字节\))?$", line.strip())
            if m:
                entries.append((m.group(2), m.group(1) == "文件"))
        if not entries:
            return None
        exact = [e for e in entries if e[0] == name]
        prefix = [e for e in entries if e[0].startswith(stem) and e[0] != name]
        picked = exact[0] if exact else (prefix[0] if prefix else None)
        if not picked:
            return None
        return (parent + "/" + picked[0]) if parent else picked[0]

    async def _read_full(self, ctx: WorkerContext, project_id: str, instruction: str, target_path, target_key) -> str | None:
        """补丁兜底用：读取目标文件全文."""
        read, _located = await _read_with_retry(
            self, ctx, project_id, instruction, target_path, target_key
        )
        if not read.get("success"):
            return None
        return str(read.get("content") or "")

    async def _full_rewrite(
        self,
        ctx: WorkerContext,
        node: TaskNode,
        project_id: str,
        instruction: str,
        target_path,
        target_key,
        path_label: str,
        project_files: list[str],
        original: str,
    ) -> dict:
        """全文重写路径：生成增量实时流式写盘 → 自检（最多修正 1 次，不升档）."""
        from app.services import code_stream

        await _report_progress(ctx.job_id, node.id, f"正在生成 {path_label} 代码…")
        effort = effort_start_for_task(
            instruction, len(original or ""), not bool((original or "").strip())
        )

        # 流式写盘：每次 LLM 尝试前发 start（客户端截断重写），增量逐段推送，
        # 结束发 end（ok=false 时客户端回滚备份）。
        async def _stream_begin() -> None:
            await code_stream.start_stream(
                ctx.job_id, node.id, project_id, path_label, "full"
            )

        async def _stream_chunk(chunk: str) -> None:
            await code_stream.push_chunk(ctx.job_id, node.id, chunk)

        new_content = await generate_code_content(
            ctx,
            instruction,
            path_label,
            original or "",
            self._SYSTEM_PROMPT,
            project_files=project_files,
            reasoning_effort=effort,
            # 兜底路径强制全文重写语义，避免大文件又掉回服务端补丁慢循环
            force_full=True,
            stream_begin_cb=_stream_begin,
            stream_cb=_stream_chunk,
        )
        if not new_content:
            await code_stream.end_stream(
                ctx.job_id, node.id, ok=False, error="模型未能生成修改内容"
            )
            return {
                "success": False,
                "error": "模型未能生成修改内容",
                "error_code": "EXEC_ERROR",
            }
        await code_stream.end_stream(ctx.job_id, node.id, ok=True)
        # 不再做 LLM 自检（省 token）：质量交给 COMMON_PITFALLS.md 自检清单 +
        # 测试节点的静态类型检查；写码时已按最小上下文契约约束检索范围。
        await _report_progress(ctx.job_id, node.id, f"流式写入完成 {path_label}…")
        # 内容已由客户端在生成期间实时写盘（streamWrite IPC），这里不再二次写入
        write_result = {
            "success": True,
            "content": f"已流式写入 {path_label}（生成期间实时落盘）",
            "path": path_label,
            "new_content": new_content,
            "instruction": instruction,
            "streamed": True,
        }
        write_result["step_title"] = await _step_title(ctx, node, write_result)
        return write_result

    async def execute(self, node: TaskNode, ctx: WorkerContext) -> dict:
        project_id = str(node.params.get("project_id") or "")
        instruction = str(node.params.get("instruction") or "").strip()
        if not project_id or not instruction:
            return {
                "success": False,
                "error": "缺少 project_id 或 instruction",
                "error_code": "INVALID_ARGS",
            }

        target_path = node.params.get("target_file")
        target_key = node.params.get("file_key")
        original = node.params.get("original_content")
        path_label = str(target_path or target_key or "")

        # 分层重试：打回反馈是静态检查错误时，先走轻量修复（不重新生成整个文件）
        if (
            original is None
            and (target_path or target_key)
            and self._has_static_feedback(instruction)
        ):
            fixed = await self._try_static_fix(
                ctx,
                node,
                project_id,
                instruction,
                target_path,
                target_key,
                path_label,
            )
            if fixed:
                return fixed

        # 大文件本地补丁模式标记（客户端提取返回 blocks 时启用）
        patch_info = None

        # 上游（如 code_reader）未传内容时，自行定位并获取编辑上下文
        if original is None:
            # 快路径：热缓存记忆命中（最近修改/高频文件）→ 不再向量检索
            located = await locate_from_memory(self, ctx, project_id, instruction)
            if not located:
                located = await locate_project_file(
                    ctx.user_id, project_id, instruction, target_path, target_key
                )
            if not located or not (located.get("path") or located.get("file_key")):
                # 显式指定了新文件路径（不在索引中）：允许创建
                if target_path and _looks_like_new_file(target_path):
                    target_path = str(target_path)
                    path_label = target_path
                    original = ""
                    located = {"path": target_path, "new_file": True}
                else:
                    return {
                        "success": False,
                        "error": "未能在项目索引中定位相关文件，请明确要修改的文件",
                        "error_code": "EXEC_ERROR",
                    }
            else:
                target_path = located.get("path") or target_path
                target_key = located.get("file_key") or target_key
                path_label = str(target_path or target_key)
                if located.get("new_file"):
                    original = ""
                else:
                    # 客户端提取：vue 走 blocks（template/script/style 分段），
                    # 其余小文件回传全文、大文件只回传相关块
                    ext = await self.run_skill(
                        "extract_code_blocks",
                        {
                            "project_id": project_id,
                            "path": target_path or target_key,
                            "instruction": instruction,
                            "context_lines": 10,
                        },
                        ctx,
                    )
                    if ext.get("mode") == "full":
                        original = ext.get("content") or ""
                    elif ext.get("success") and ext.get("blocks"):
                        patch_info = ext
                    else:
                        # 目标文件可能不存在（索引过期/路径变化）：先在真实目录里重定位
                        fixed = await self._retry_locate_real_file(
                            ctx, project_id, target_path, target_key
                        )
                        if fixed:
                            target_path = fixed
                            path_label = fixed
                            ext = await self.run_skill(
                                "extract_code_blocks",
                                {
                                    "project_id": project_id,
                                    "path": fixed,
                                    "instruction": instruction,
                                    "context_lines": 10,
                                },
                                ctx,
                            )
                            if ext.get("mode") == "full":
                                original = ext.get("content") or ""
                            elif ext.get("success") and ext.get("blocks"):
                                patch_info = ext
                                target_key = None
                        if original is None and patch_info is None:
                            # 仍失败：读全文兜底（保留原始目标，错误信息里带真实目录）
                            read, located = await _read_with_retry(
                                self, ctx, project_id, instruction, target_path, target_key
                            )
                            if not read.get("success"):
                                return read
                            if located:
                                target_path = located.get("path") or target_path
                                target_key = located.get("file_key") or target_key
                                path_label = str(target_path or target_key)
                            original = read.get("content") or ""

        # 删除意图：规划器显式传 action=delete 时才删除（避免把"实现删除功能"误判为删文件）
        if node.params.get("action") == "delete":
            await _report_progress(ctx.job_id, node.id, f"正在删除 {path_label}…")
            del_result = await self.run_skill(
                "delete_project_file",
                {
                    "project_id": project_id,
                    "path": target_path or target_key or "",
                    "recursive": False,
                },
                ctx,
            )
            if del_result.get("success"):
                del_result["step_title"] = f"删除 {path_label}"
                del_result["deleted"] = True
            return del_result

        project_files = await list_project_files(ctx.user_id, project_id)

        # 大文件本地补丁模式：客户端提取 → 补丁生成 → 本地应用（写入暂存缓冲）
        # 重试上限 2 次（初始 + 1 次修正）；补丁失败/耗尽一律回退全文重写，不再升档硬磕。
        if patch_info:
            await _report_progress(ctx.job_id, node.id, f"正在生成 {path_label} 补丁…")
            context = _format_patch_context(
                patch_info.get("blocks") or [],
                patch_info.get("imports") or [],
                int(patch_info.get("total_lines") or 0),
            )
            effort = effort_start_for_task(instruction, 0, False)
            retry_hint = ""
            region = ""
            applied = 0
            for _attempt in range(2):
                patch_text = await generate_code_content(
                    ctx,
                    instruction,
                    path_label,
                    "",
                    self._SYSTEM_PROMPT,
                    project_files=project_files,
                    reasoning_effort=effort,
                    patch_mode=True,
                    context_override=context + retry_hint,
                )
                if not patch_text:
                    return {
                        "success": False,
                        "error": "模型未能生成修改内容",
                        "error_code": "EXEC_ERROR",
                    }
                blocks = parse_search_replace(patch_text)
                if not blocks:
                    # 模型未按补丁格式输出 → 回退全文重写
                    original = await self._read_full(
                        ctx, project_id, instruction, target_path, target_key
                    )
                    if original is None:
                        return {
                            "success": False,
                            "error": "读取文件失败，无法完成修改",
                            "error_code": "EXEC_ERROR",
                        }
                    return await self._full_rewrite(
                        ctx, node, project_id, instruction,
                        target_path, target_key, path_label, project_files, original,
                    )
                res = await self.run_skill(
                    "apply_patch",
                    {
                        "project_id": project_id,
                        "path": target_path or target_key,
                        "blocks": blocks,
                        # 重试时基于原始文件重新应用，避免叠在上一轮补丁上
                        "reset_file": _attempt > 0,
                    },
                    ctx,
                )
                if res.get("success"):
                    region = str(res.get("region") or "")
                    applied = int(res.get("applied") or 0)
                    # 不再做 LLM 自检（省 token）：补丁应用成功即进入下一环节，
                    # 质量交给静态类型检查 + COMMON_PITFALLS.md 自检清单
                    break
                else:
                    retry_hint = (
                        f"\n\n【补丁应用失败】{res.get('error') or ''}。"
                        "请重新输出补丁：SEARCH 必须逐字符复制文件原文并带足够上下文唯一匹配。"
                    )
            else:
                # 补丁重试耗尽 → 回退全文重写
                original = await self._read_full(
                    ctx, project_id, instruction, target_path, target_key
                )
                if original is None:
                    return {
                        "success": False,
                        "error": "读取文件失败，无法完成修改",
                        "error_code": "EXEC_ERROR",
                    }
                return await self._full_rewrite(
                    ctx, node, project_id, instruction,
                    target_path, target_key, path_label, project_files, original,
                )
            # apply_patch 已把修改写入暂存缓冲（不落盘）
            write_result = {
                "success": True,
                "content": f"已应用 {applied} 块补丁 → {path_label}（暂存中）",
                "path": path_label,
                "region": region,
                "staged": True,
            }
            write_result["step_title"] = await _step_title(ctx, node, write_result)
            return write_result

        return await self._full_rewrite(
            ctx, node, project_id, instruction,
            target_path, target_key, path_label, project_files, original or "",
        )
