"""工作区读取快路径的目标判定（B 方案：先读、再答）。

产品方案把工作区任务按“目标是否明确”分流：

======================  ==========================================
工作区 + 单文件目标明确   M1：atomic_step(workspace_navigator, read) → direct_llm
工作区 + 目标明确文件未知  M2/M3：由 Agent 先 search/list 再选文件读取
工作区 + 全目录/多文件     M2 顺序流程：逐个读取，不能走一次性快路径
======================  ==========================================

本模块只负责**第一类**：从用户请求里认出"唯一明确的单个文件"，从而避免工作区
内容问题落到无工具的 ``direct_llm`` 节点上（那会让模型只能输出内部路由标记）。

它有意保持保守：

* 只有显式给出文件名/路径（``config.yaml``、``src/main.py``、``README.md``）
  或请求串里恰好只出现工作区摘要中的一个文件名时才命中；
* 目标不明确时**不做任何猜测**，直接返回 None，让正常路由（M0/M2/M3）继续；
* 这里不产生工具调用、不读文件、不校验权限 —— 授权仍由 ``atomic_step`` →
  ``execute_tool_call`` 的工作区门最终裁决。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 明确的文件引用：带扩展名的路径形态。
# 关键点：用"无空白"来界定路径 token —— 否则中文句子里的"读取工作区里的
# config.yaml"会被整体当成一个文件名。目录段允许以 "/" 分隔，且每个段都不含空格。
_EXPLICIT_FILE_RE = re.compile(
    r"((?:[\w\u4e00-\u9fff][\w\u4e00-\u9fff.\-]*/)+[\w\u4e00-\u9fff][\w\u4e00-\u9fff.\-]*"  # 带目录的路径
    r"|[\w\u4e00-\u9fff][\w\u4e00-\u9fff.\-]*"                                              # 纯文件名
    r")"
    r"\.([A-Za-z0-9]{1,8})"
)
# 常见文件名（无扩展名），避免把普通词当路径。
_KNOWN_BARE_FILES = ("dockerfile", "makefile", "readme", "license", ".gitignore", ".env")
# 这些"扩展名"是版本号/小数/网址的常见形态，不是文件类型。
_NON_FILE_EXTENSIONS = frozenset({
    "com", "cn", "net", "org", "io", "http", "https", "www",
})

# 写入/执行意图：这些请求即使提到文件也不走"只读快路径"。
WRITE_INTENT_TOKENS = (
    "修改", "改成", "写入", "创建", "新建", "删除", "重命名", "移动", "复制", "替换",
    "保存", "导出", "提交", "回滚", "运行", "执行", "测试", "构建", "安装", "部署",
    " fix", " edit", " write", " create", " delete", " rename", " run ", " commit",
)
# 全目录/多文件意图：应走 M2 顺序流程而不是一次性 M1 读取。
BULK_INTENT_TOKENS = (
    "所有", "全部", "每个", "各个", "逐个", "整个目录", "整个文件夹", "所有文档",
    "所有文件", "批量", "汇总全部", "all files", "every file", "each file",
)


@dataclass(frozen=True, slots=True)
class WorkspaceReadTarget:
    """工作区读取快路径的目标（只描述"读哪一个"，不含任何授权结论）。"""

    path: str
    source: str  # explicit_reference | unique_summary_entry


def _is_plausible_file_reference(candidate: str) -> bool:
    """校验正则命中的 token 真的是"文件名"，而不是句子片段或版本号。"""
    text = str(candidate or "").strip()
    if not text or len(text) > 200 or any(char.isspace() for char in text):
        return False
    if ".." in text or text.startswith("/") or text.endswith("/"):
        return False
    if "." not in text:
        return False
    stem, _, ext = text.rpartition(".")
    if not stem or not ext:
        return False
    if ext.casefold() in _NON_FILE_EXTENSIONS:
        return False
    # 纯版本号/小数（v1.2、1.2.3）不是文件名。
    if re.fullmatch(r"v?\d+(\.\d+)*", stem.casefold()):
        return False
    # 全是数字/点的主干也不是文件名。
    if not re.search(r"[^\d.\s]", stem):
        return False
    return True


def _resolve_summary_entry(name: str, workspace_summary: str) -> str:
    """把请求里的小写文件名还原成摘要中的真实条目名（含扩展名）。"""
    target = str(name or "").casefold()
    if not target:
        return ""
    for entry in _summary_file_names(workspace_summary):
        lowered = entry.casefold()
        if lowered == target or lowered.rsplit(".", 1)[0] == target:
            return entry
    return ""


def _summary_file_names(workspace_summary: str) -> list[str]:
    """从工作区摘要的目录清单里提取条目名（摘要只列文件名与类型）。"""
    names: list[str] = []
    for line in str(workspace_summary or "").splitlines():
        text = line.strip()
        if not text.startswith(("- ", "* ")):
            continue
        entry = text[2:].strip()
        # 摘要形态：``- README.md（文件）`` / ``- src（目录）``
        entry = re.split(r"[（(]", entry, maxsplit=1)[0].strip()
        entry = entry.rstrip("/").strip()
        if not entry:
            continue
        names.append(entry)
    return names


def workspace_read_target(request: str, workspace_summary: str) -> WorkspaceReadTarget | None:
    """返回唯一明确的工作区读取目标；不确定时返回 None（绝不猜测）。"""
    text = str(request or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if any(token in lowered for token in WRITE_INTENT_TOKENS):
        return None
    # “通读整份 <单个文件>”仍是单文件完整读取，不应因为“整份/全部页面”
    # 被误分到全目录 coverage；只有没有唯一文件目标时才进入 bulk 路径。
    bulk_tokens = [token for token in BULK_INTENT_TOKENS if token in lowered]
    if bulk_tokens and not _EXPLICIT_FILE_RE.search(text):
        return None

    # 1) 请求里显式出现的文件引用（按出现顺序去重）。
    explicit: list[str] = []
    explicit_from_summary = False
    for match in _EXPLICIT_FILE_RE.finditer(text):
        candidate = f"{match.group(1)}.{match.group(2)}".strip().strip("。，,、；;：:！!？?")
        if _is_plausible_file_reference(candidate) and candidate not in explicit:
            explicit.append(candidate)
    # 无扩展名的常见文件名（README / Dockerfile 等）：先按请求原文判断出现，
    # 再优先用摘要里的真实条目名回填（`README` → `README.md`）。
    for bare in _KNOWN_BARE_FILES:
        if bare not in lowered:
            continue
        if any(item.casefold() == bare for item in explicit):
            continue
        resolved = _resolve_summary_entry(bare, workspace_summary)
        if resolved:
            explicit_from_summary = True
        explicit.append(resolved or bare)
    deduped = list(dict.fromkeys(explicit))
    if len(deduped) == 1:
        source = (
            "unique_summary_entry"
            if explicit_from_summary and not _EXPLICIT_FILE_RE.search(text)
            else "explicit_reference"
        )
        return WorkspaceReadTarget(path=deduped[0], source=source)
    if len(deduped) > 1:
        # 一次提到多个文件：属于多文件任务，交给正常路由（M2/顺序流程）。
        return None

    # 2) 请求串里恰好只命中摘要中的一个条目：也是"目标明确"。
    matched: list[str] = []
    for name in _summary_file_names(workspace_summary):
        if name and name.casefold() in lowered:
            matched.append(name)
    unique = list(dict.fromkeys(matched))
    if len(unique) == 1:
        return WorkspaceReadTarget(path=unique[0], source="unique_summary_entry")
    return None


def workspace_read_available(workspace_id: str, workspace_summary: str) -> bool:
    """快路径前置条件：工作区已绑定且上下文自报可访问。"""
    if not str(workspace_id or "").strip():
        return False
    return "已注册且可访问" in str(workspace_summary or "")


def workspace_bulk_intent(request: str) -> bool:
    """是否要求处理整个目录/多文件（需要 M2 顺序覆盖，而不是一次 M1 读取）。"""
    lowered = str(request or "").casefold()
    return any(token in lowered for token in BULK_INTENT_TOKENS)


def looks_like_workspace_request(request: str) -> bool:
    """保守判断请求是否面向本地工作区内容（只读意图 + 工作区词元）。

    只用词表，不做业务语义推断；真正的授权与能力边界仍由执行期的工作区门决定。
    """
    lowered = str(request or "").casefold()
    if any(token in lowered for token in WRITE_INTENT_TOKENS):
        return False
    markers = (
        "工作区", "项目里", "项目代码", "本地项目", "代码库", "仓库", "目录", "文件夹",
        "workspace", "repo", "src/", ".py", ".md", ".txt", ".json", ".yaml", ".yml",
    )
    return any(marker in lowered for marker in markers)


__all__ = [
    "BULK_INTENT_TOKENS",
    "WRITE_INTENT_TOKENS",
    "WorkspaceReadTarget",
    "looks_like_workspace_request",
    "workspace_bulk_intent",
    "workspace_read_available",
    "workspace_read_target",
]
