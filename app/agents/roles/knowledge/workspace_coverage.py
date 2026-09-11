"""工作区候选发现与覆盖读取（Acceptance 2 / 3）。

产品方案要求复杂工作区任务由 Agent 自主发现文件，而不是等用户给路径：

* **目标明确、文件未知**（"找出项目中负责登录的代码"）
  → ``search`` → 选择候选 → ``read``；搜索无命中时补 ``list`` 兜底。
  绝不让用户提供路径。
* **全目录处理**（"总结这个资料文件夹的全部文档"）
  → ``list`` 建立文件清单 → 逐个 ``read`` → 维护 completed/failed/skipped
  → 只有 ``coverage=ALL`` 才能声称全部完成。

本 Agent 把这两条统一成"有界循环 + 显式覆盖度记账"：每一步都调用**同一个**
``workspace_navigator`` 工具（因此 tool_started/tool_completed 与 action 都能被
审计与 SSE 观察到），并把候选文件、已读文件、失败/跳过文件与覆盖度写进
``tool_metadata.workspace_coverage`` 供下游步骤与最终回答使用。

它不做关键词业务路由：候选筛选用的是"命中分数 + 文件名/正文词元重合"这种
通用相关性度量。
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.agents.core.base import WorkerAgent, WorkerContext
from app.agents.core.progress import set_progress
from app.agents.skills.base import SkillContext

if TYPE_CHECKING:
    from app.agents.orchestration.models import TaskNode

# 覆盖度语义（与产品方案一致）
COVERAGE_TARGETED = "TARGETED"
COVERAGE_SELECTED = "SELECTED"
COVERAGE_ALL = "ALL"
COVERAGE_PARTIAL = "PARTIAL"

MAX_READ_FILES = 8           # SELECTED 模式下最多读取的候选文件数
MAX_COVERAGE_FILES = 12      # ALL 模式下单次任务允许处理的文件数
MAX_EVIDENCE_CHARS = 6000    # 单个文件注入后续步骤的正文上限
MAX_TOTAL_CHARS = 40000      # 整节点累计证据上限

_LATIN_TOKEN = re.compile(r"[A-Za-z0-9_.\-]{2,}")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOP_TOKENS = frozenset({
    "帮我", "一下", "这个", "那个", "哪些", "怎么", "如何", "什么", "代码", "文件",
    "目录", "文件夹", "工作区", "解释", "说明", "总结", "全部", "所有", "每个", "以及",
})
# 代码/文本类优先于二进制；命中分数相同时按这个顺序。
_PREFERRED_EXTS = (
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".rb", ".php",
    ".cs", ".c", ".h", ".cpp", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini",
)


def query_terms(text: str) -> set[str]:
    """通用词元：拉丁词 + 中文 2-gram（与 document_targeting 同口径）。"""
    value = str(text or "").casefold()
    terms = {token for token in _LATIN_TOKEN.findall(value) if token not in _STOP_TOKENS}
    for run in _CJK_RUN.findall(value):
        for width in (2, 3):
            terms.update(run[index:index + width] for index in range(max(0, len(run) - width + 1)))
    return {term for term in terms if term and term not in _STOP_TOKENS}


def rank_candidates(query: str, matches: list[dict], entries: list[dict]) -> list[dict]:
    """按通用相关性给候选文件排序（路径/命中片段/文件名词元重合 + 文本类优先）。"""
    terms = query_terms(query)
    scores: dict[str, float] = {}
    for match in matches or []:
        path = str(match.get("path") or "").strip()
        if not path:
            continue
        haystack = (path + " " + str(match.get("context") or "")).casefold()
        hit = sum(1 for term in terms if term in haystack)
        weight = 2.0 if str(match.get("match_type") or "") == "filename" else 1.0
        scores[path] = max(scores.get(path, 0.0), hit * weight + 1.0)
    for entry in entries or []:
        if str(entry.get("kind") or "") != "file":
            continue
        path = str(entry.get("path") or "").strip()
        if not path:
            continue
        hit = sum(1 for term in terms if term in path.casefold())
        score = hit * 3.0
        if path.casefold().endswith(_PREFERRED_EXTS):
            score += 0.5
        if score > 0:
            scores[path] = max(scores.get(path, 0.0), score)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [{"path": path, "score": score} for path, score in ranked if score > 0]


class WorkspaceCoverageAgent(WorkerAgent):
    """受控的工作区自主发现 + 逐文件读取，并显式记录覆盖度。"""

    name = "workspace_coverage"
    description = "在工作区中自主发现候选文件并逐个读取，维护候选/已读/失败/跳过清单与覆盖度"
    params_help = '{"query":"目标","mode":"selected|all","directory":"可选限定目录"}'
    skills = ["workspace_navigator"]

    def __init__(self, *, max_read_files: int = MAX_READ_FILES) -> None:
        self._max_read_files = max(1, int(max_read_files))

    @staticmethod
    def _navigator() -> object:
        from app.agents.skills.registry import ToolRegistry

        tool = ToolRegistry.get("workspace_navigator")
        if tool is None:
            raise RuntimeError("workspace_navigator 未注册：工作区读取能力不可用")
        return tool

    async def execute(self, node: "TaskNode", ctx: WorkerContext) -> dict:
        query = str(node.params.get("query") or node.params.get("instruction") or "").strip()
        mode = str(node.params.get("mode") or "selected").strip().casefold()
        directory = str(node.params.get("directory") or "").strip()
        if not query:
            return {"success": False, "error": "工作区发现步骤缺少 query", "error_code": "INVALID_ARGS"}
        if not str(ctx.workspace_id or "").strip():
            return {
                "success": False,
                "error": "当前任务没有已选择的工作区",
                "error_code": "WORKSPACE_NOT_BOUND",
                "retryable": False,
            }
        coverage_target = COVERAGE_ALL if mode == "all" else COVERAGE_SELECTED
        await set_progress(
            ctx.job_id, node.id,
            "正在盘点工作区文件…" if coverage_target == COVERAGE_ALL else "正在搜索相关工作区文件…",
        )

        candidates: list[str] = []
        read_files: list[str] = []
        failed_files: list[dict] = []
        skipped_files: list[dict] = []
        truncated_files: list[str] = []
        read_stats: dict[str, dict] = {}
        matches: list[dict] = []
        entries: list[dict] = []
        notes: list[str] = []

        # 1) 发现候选：ALL 模式以目录清单为准；SELECTED 模式先 search 再 list 兜底。
        if coverage_target != COVERAGE_ALL:
            search = await self._call(ctx, {"action": "search", "query": query, "search_path": directory})
            if search["ok"]:
                matches = list((search.get("data") or {}).get("matches") or [])
            else:
                notes.append(f"搜索不可用（{search.get('error_code') or 'SEARCH_FAILED'}），改用目录清单")
        listing = await self._call(ctx, {"action": "list", "path": directory})
        if listing["ok"]:
            entries = list((listing.get("data") or {}).get("entries") or [])
        elif coverage_target == COVERAGE_ALL:
            return self._failure(
                node, listing, "无法枚举工作区目录，不能保证覆盖全部文件", notes,
            )
        else:
            notes.append(f"目录枚举不可用（{listing.get('error_code') or 'LIST_FAILED'}）")

        if coverage_target == COVERAGE_ALL:
            candidates = [
                str(item.get("path") or "") for item in entries
                if str(item.get("kind") or "") == "file" and str(item.get("path") or "")
            ]
        else:
            ranked = rank_candidates(query, matches, entries)
            candidates = [item["path"] for item in ranked][: self._max_read_files]
            if not candidates:
                # 没有任何相关性信号：退回目录清单中的文本类文件（仍是有界选取）。
                candidates = [
                    str(item.get("path") or "") for item in entries
                    if str(item.get("kind") or "") == "file"
                    and str(item.get("path") or "").casefold().endswith(_PREFERRED_EXTS)
                ][:3]

        if not candidates:
            return {
                "success": False,
                "error": "工作区里没有找到与目标相关的可读文件，已停止而不是编造内容。",
                "error_code": "WORKSPACE_NO_CANDIDATE",
                "retryable": False,
                "tool_metadata": self._metadata(
                    coverage_target, candidates, read_files, failed_files, skipped_files,
                    matches, notes, coverage=COVERAGE_TARGETED,
                ),
            }

        # 2) 逐个读取（超过预算的明确记为 skipped，不静默丢弃）。
        budget = self._max_read_files if coverage_target != COVERAGE_ALL else MAX_COVERAGE_FILES
        evidence: list[str] = []
        total_chars = 0
        for index, path in enumerate(candidates):
            if index >= budget:
                skipped_files.append({"path": path, "reason": "budget_exceeded"})
                continue
            await set_progress(ctx.job_id, node.id, f"正在读取 {path}…")
            # navigator 的单次页预算只限制一次调用，不是文件总量上限。
            # 覆盖 Agent 保留有界行为：若返回 has_more，明确记为部分覆盖，
            # 后续步骤/用户可按 cursor 继续，不在一个节点里无界灌入上下文。
            read = await self._call(ctx, {"action": "read", "path": path})
            if not read["ok"]:
                failed_files.append({"path": path, "error_code": read.get("error_code") or "READ_FAILED"})
                continue
            sections = list((read.get("data") or {}).get("sections") or [])
            text = "\n".join(str(item.get("text") or "") for item in sections if isinstance(item, dict))
            if not text.strip():
                failed_files.append({"path": path, "error_code": "EMPTY_CONTENT"})
                continue
            read_files.append(path)
            # 记录这次读取的分页事实：跨页读完与"被页数上限截断"必须能区分。
            read_stats[path] = {
                "chars": int((read.get("meta") or {}).get("char_count") or len(text)),
                "pages_read": int((read.get("meta") or {}).get("pages_read") or 1),
                "page_budget_exhausted": bool(
                    (read.get("meta") or {}).get("page_budget_exhausted")
                ),
                "has_more": bool(read.get("has_more")),
            }
            if read.get("has_more"):
                # 分页契约：has_more=true 表示这份文件还没读完。记账时必须说明，
                # 否则后续回答可能把"一页"当成"整份"。
                truncated_files.append(path)
            if total_chars < MAX_TOTAL_CHARS:
                chunk = text[:MAX_EVIDENCE_CHARS]
                total_chars += len(chunk)
                evidence.append(f"===== 工作区文件：{path} =====\n{chunk}")

        if not read_files:
            return self._failure(
                node, {"error_code": "WORKSPACE_READ_FAILED"},
                "候选文件全部读取失败，未能获得正文", notes,
                extra=self._metadata(
                    coverage_target, candidates, read_files, failed_files, skipped_files,
                    matches, notes, coverage=COVERAGE_PARTIAL,
                ),
            )

        coverage = self._coverage(coverage_target, candidates, read_files, skipped_files)
        if truncated_files and coverage == COVERAGE_ALL:
            # 有文件只读到一页（has_more=true）时不算全量覆盖。
            coverage = COVERAGE_PARTIAL
        metadata = self._metadata(
            coverage_target, candidates, read_files, failed_files, skipped_files,
            matches, notes, coverage=coverage, truncated_files=truncated_files,
            read_stats=read_stats,
        )
        header = (
            f"已读取 {len(read_files)} 个工作区文件"
            f"（候选 {len(candidates)}，失败 {len(failed_files)}，跳过 {len(skipped_files)}，"
            f"coverage={coverage}）"
        )
        if truncated_files:
            header += (
                f"；其中 {len(truncated_files)} 个文件本次仍未读完"
                "（本次只读了一页批次/部分页面，has_more=true），可继续用 cursor 读取"
            )
        if coverage != COVERAGE_ALL and coverage_target == COVERAGE_ALL:
            header += "：未覆盖全部候选文件，回答时必须说明这是部分结果。"
        content = header + "\n\n" + "\n\n".join(evidence)
        return {
            "success": True,
            # 显式给出 status=ok：DAG 的 dependency payload 会 setdefault("status", ...)，
            # 写清楚可避免下游把这份正文证据当成"未知状态"而丢弃。
            "status": "ok",
            "content": content,
            "output": content,
            "read_evidence": True,
            "step_title": "盘点并读取工作区文件",
            "tool_metadata": metadata,
        }

    # ── 内部工具 ──────────────────────────────────────────────

    async def _call(self, ctx: WorkerContext, args: dict) -> dict:
        """调用 workspace_navigator（workspace_id 由 SkillContext 注入）。"""
        tool = self._navigator()
        context = SkillContext(
            user_id=ctx.user_id,
            scene=ctx.scene,
            conversation_id=ctx.job_id,
            job_id=ctx.job_id,
            workspace_id=ctx.workspace_id,
            llm_api_key=ctx.llm_api_key,
            llm_config=ctx.llm_config,
            on_notify=getattr(ctx, "on_notify", None),
            on_output=getattr(ctx, "on_output", None),
        )
        try:
            result = await tool.execute(args, context)
        except Exception as exc:  # noqa: BLE001 - 读取异常按失败记账，不中断整个节点
            return {"ok": False, "error_code": "WORKSPACE_READ_FAILED", "error": str(exc)[:200]}
        payload = result.data if isinstance(result.data, dict) else {}
        status = str(payload.get("status") or ("ok" if result.status == "success" else "error"))
        action = str(payload.get("action") or args.get("action") or "")
        return {
            "ok": status in {"ok", "partial", "empty"},
            "status": status,
            "action": action,
            "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
            "meta": payload.get("meta") if isinstance(payload.get("meta"), dict) else {},
            "error_code": (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None,
            "has_more": bool(payload.get("has_more")),
            "cursor": str(payload.get("cursor") or "") or None,
        }

    @staticmethod
    def _coverage(
        target: str, candidates: list[str], read_files: list[str], skipped: list[dict],
    ) -> str:
        if target != COVERAGE_ALL:
            return COVERAGE_SELECTED if read_files else COVERAGE_TARGETED
        if not skipped and len(read_files) >= len(candidates) and candidates:
            return COVERAGE_ALL
        return COVERAGE_PARTIAL

    @staticmethod
    def _metadata(
        target: str,
        candidates: list[str],
        read_files: list[str],
        failed_files: list[dict],
        skipped_files: list[dict],
        matches: list[dict],
        notes: list[str],
        *,
        coverage: str,
        truncated_files: list[str] | None = None,
        read_stats: dict[str, dict] | None = None,
    ) -> dict:
        truncated = list(truncated_files or [])
        return {
            "tool": "workspace_navigator",
            "workspace_coverage": {
                "coverage": coverage,
                "coverage_target": target,
                "candidate_files": list(candidates),
                "selected_files": list(candidates),
                "read_files": list(read_files),
                "failed_files": list(failed_files),
                "skipped_files": list(skipped_files),
                # has_more=true 的文件：本次只读了一页，需要继续 cursor 才算读完。
                "truncated_files": truncated,
                # 每个文件的读取分页事实（字符数/页数/是否被页数上限截断）。
                "read_stats": dict(read_stats or {}),
                "match_count": len(matches),
                "read_count": len(read_files),
                "failed_count": len(failed_files),
                "skipped_count": len(skipped_files),
                "truncated_count": len(truncated),
                "notes": list(notes),
            },
        }

    @staticmethod
    def _failure(node, result: dict, message: str, notes: list[str], *, extra: dict | None = None) -> dict:
        payload = {
            "success": False,
            "error": message,
            "error_code": str(result.get("error_code") or "WORKSPACE_READ_FAILED"),
            "retryable": False,
            "tool": "workspace_navigator",
        }
        metadata = dict(extra or {})
        coverage = (metadata.get("workspace_coverage") or {}).get("coverage")
        if coverage:
            payload["tool_metadata"] = metadata
        if notes:
            payload["notes"] = list(notes)
        return payload


__all__ = [
    "COVERAGE_ALL",
    "COVERAGE_PARTIAL",
    "COVERAGE_SELECTED",
    "COVERAGE_TARGETED",
    "MAX_COVERAGE_FILES",
    "MAX_READ_FILES",
    "WorkspaceCoverageAgent",
    "query_terms",
    "rank_candidates",
]
