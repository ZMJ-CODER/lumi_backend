"""代码补丁工具：tree-sitter 提取代码块 + SEARCH/REPLACE 解析与应用.

大文件改动策略（小文件全文重写，大文件精准打补丁）：
  - extract_code_blocks：用 tree-sitter 提取函数/类定义范围（未装语法/解析失败回退正则）
  - build_edit_context：大文件只把相关代码块 + 引用导入发给模型
  - parse_search_replace / apply_search_replace：解析模型输出的 SEARCH/REPLACE 块并应用，
    支持精确匹配 + 空白归一化兜底；匹配失败返回具体原因供模型重试
"""

import re

from loguru import logger

_LANG_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "vue",
}

_NODE_TYPES = {
    "python": {"function_definition", "class_definition"},
    "javascript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
    },
    "typescript": {
        "function_declaration",
        "class_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
    },
}


def detect_language(file_path: str) -> str:
    ext = (file_path or "").rsplit(".", 1)[-1].lower()
    return _LANG_EXT.get("." + ext, "")


def _load_language(lang: str):
    try:
        from tree_sitter import Language

        if lang == "python":
            from tree_sitter_python import language as fn
        elif lang == "javascript":
            from tree_sitter_javascript import language as fn
        else:
            from tree_sitter_typescript import language as fn
        return Language(fn())
    except Exception as exc:  # noqa: BLE001
        logger.debug("tree-sitter 语法加载失败({}): {}", lang, exc)
        return None


def _regex_extract_blocks(content: str, lang: str) -> list[dict]:
    """正则兜底提取（def/class/function 行范围），精度低于 tree-sitter."""
    lines = content.splitlines()
    blocks: list[dict] = []
    if lang == "python":
        pattern = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)|^\s*class\s+(\w+)")
    else:
        pattern = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)|^\s*class\s+(\w+)|^\s*(?:const|let)\s+(\w+)\s*=\s*(?:\([^)]*\)\s*=>|async\s*\(|function)")
    stack: list[tuple[int, str]] = []
    indent = None
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if m:
            name = next((g for g in m.groups() if g), "")
            if stack:
                s, nm = stack.pop()
                blocks.append({"name": nm, "start_line": s, "end_line": i - 1})
            stack.append((i, name))
            indent = len(line) - len(line.lstrip())
            continue
        if stack and line.strip() and not line.strip().startswith(("#", "//", "/*", "*")):
            cur = len(line) - len(line.lstrip())
            if cur <= (indent or 0) and stack:
                s, nm = stack.pop()
                blocks.append({"name": nm, "start_line": s, "end_line": i - 1})
                indent = None
    while stack:
        s, nm = stack.pop()
        blocks.append({"name": nm, "start_line": s, "end_line": len(lines) - 1})
    for b in blocks:
        b["text"] = "\n".join(lines[b["start_line"] : b["end_line"] + 1])
    return blocks


def _find_tag_end(s: str, from_: int) -> int:
    """找标签结束符 >（跳过引号包裹的属性值）."""
    quote = ""
    for i in range(from_, len(s)):
        c = s[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in ('"', "'"):
            quote = c
        elif c == ">":
            return i
    return len(s) - 1


def _split_template_segments(inner: str, line_offset: int) -> list[dict]:
    """Vue template 内部按顶层元素切段（跳过 {{}} 与 <!-- -->），返回带行号的段."""
    segs: list[dict] = []
    depth = 0
    start = -1
    tag = ""
    i = 0
    n = len(inner)
    tag_re = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w:-]*)")
    while i < n:
        if inner.startswith("{{", i):
            end = inner.find("}}", i + 2)
            i = end + 2 if end >= 0 else n
            continue
        if inner[i] != "<":
            i += 1
            continue
        if inner.startswith("<!--", i):
            end = inner.find("-->", i + 4)
            i = end + 3 if end >= 0 else n
            continue
        m = tag_re.match(inner, i)
        if not m:
            i += 1
            continue
        close_idx = _find_tag_end(inner, i)
        is_close = m.group(1) == "/"
        is_self_close = close_idx > i and inner[close_idx - 1] == "/"
        if is_close:
            if depth > 0:
                depth -= 1
            if depth == 0 and start >= 0:
                segs.append(
                    {
                        "tag": tag,
                        "start_line": line_offset + inner[:start].count("\n"),
                        "text": inner[start : close_idx + 1],
                    }
                )
                start = -1
                tag = ""
        else:
            if depth == 0:
                start = i
                tag = m.group(2)
            if not is_self_close:
                depth += 1
            elif depth == 0 and start >= 0:
                segs.append(
                    {
                        "tag": tag,
                        "start_line": line_offset + inner[:start].count("\n"),
                        "text": inner[start : close_idx + 1],
                    }
                )
                start = -1
                tag = ""
        i = close_idx + 1
    if start >= 0:
        segs.append(
            {
                "tag": tag,
                "start_line": line_offset + inner[:start].count("\n"),
                "text": inner[start:],
            }
        )
    return segs


def extract_code_blocks(content: str, file_path: str) -> list[dict]:
    """用 tree-sitter 提取函数/类定义范围（含文本）；失败回退正则."""
    lang = detect_language(file_path)
    if not lang:
        return []
    if lang == "vue":
        # Vue SFC：template 顶层分段 + script 函数/类 + style 块都进上下文，
        # 保证改模板/样式时模型能看到对应区域（否则 SEARCH/REPLACE 必然匹配失败）。
        blocks: list[dict] = []
        tmpl = re.search(r"<template[^>]*>([\s\S]*?)</template>", content, re.I | re.S)
        if tmpl:
            inner = tmpl.group(1)
            # 内容起点 = 开头标签的 > 之后（group(0) 是整个匹配，含闭合标签）
            inner_start = tmpl.start() + tmpl.group(0).index(">") + 1
            offset = content[:inner_start].count("\n")
            segs = _split_template_segments(inner, offset)
            if segs:
                for s in segs[:3]:
                    blocks.append(
                        {
                            "name": f"template:{s['tag']}",
                            "start_line": s["start_line"],
                            "end_line": s["start_line"] + s["text"].count("\n"),
                            "text": s["text"],
                        }
                    )
            else:
                start_line = content[: tmpl.start()].count("\n")
                blocks.append(
                    {
                        "name": "template",
                        "start_line": start_line,
                        "end_line": start_line + tmpl.group(0).count("\n"),
                        "text": tmpl.group(0),
                    }
                )
        script = re.search(r"<script[^>]*>([\s\S]*?)</script>", content, re.I | re.S)
        if script:
            inner = script.group(1)
            inner_start = script.start() + script.group(0).index(">") + 1
            offset = content[:inner_start].count("\n")
            inner_lang = "typescript" if "ts" in script.group(0).lower() else "javascript"
            fn_blocks = _extract_with_parser(inner, inner_lang) or _regex_extract_blocks(inner, inner_lang)
            for b in fn_blocks:
                blocks.append(
                    {
                        "name": f"script:{b.get('name') or '<匿名>'}",
                        "start_line": b["start_line"] + offset,
                        "end_line": b["end_line"] + offset,
                        "text": b["text"],
                    }
                )
            if not fn_blocks:
                start_line = content[: script.start()].count("\n")
                blocks.append(
                    {
                        "name": "script",
                        "start_line": start_line,
                        "end_line": start_line + script.group(0).count("\n"),
                        "text": script.group(0),
                    }
                )
        style = re.search(r"<style[^>]*>([\s\S]*?)</style>", content, re.I | re.S)
        if style:
            start_line = content[: style.start()].count("\n")
            blocks.append(
                {
                    "name": "style",
                    "start_line": start_line,
                    "end_line": start_line + style.group(0).count("\n"),
                    "text": style.group(0),
                }
            )
        return blocks[:5]
    return _extract_with_parser(content, lang) or _regex_extract_blocks(content, lang)


def _extract_with_parser(content: str, lang: str) -> list[dict] | None:
    lang_obj = _load_language(lang)
    if lang_obj is None:
        return None
    try:
        from tree_sitter import Parser

        parser = Parser(lang_obj)
        tree = parser.parse(content.encode("utf-8"))
        types = _NODE_TYPES.get(lang, _NODE_TYPES["javascript"])
        blocks: list[dict] = []

        def walk(node) -> None:
            if node.type in types:
                name_node = node.child_by_field_name("name")
                bname = name_node.text.decode("utf-8") if name_node and name_node.text else ""
                blocks.append({"name": bname, "start_line": node.start_point[0], "end_line": node.end_point[0]})
            for c in node.children:
                walk(c)

        walk(tree.root_node)
        lines = content.splitlines()
        for b in blocks:
            b["text"] = "\n".join(lines[b["start_line"] : b["end_line"] + 1])
        return blocks
    except Exception as exc:  # noqa: BLE001
        logger.debug("tree-sitter 解析失败: {}", exc)
        return None


def build_edit_context(
    content: str,
    file_path: str,
    instruction: str,
    max_lines: int = 150,
    max_blocks: int = 5,
    context_lines: int = 10,
) -> str:
    """大文件只提取相关代码块 + 引用导入，小文件返回全文."""
    lines = content.splitlines()
    if len(lines) <= max_lines:
        return content
    blocks = extract_code_blocks(content, file_path)
    if not blocks:
        return "\n".join(lines[:max_lines]) + f"\n…（文件共 {len(lines)} 行，仅展示前 {max_lines} 行）"
    # 第二层上下文：每个相关代码块前后附加 context_lines 行上下文代码
    if context_lines > 0:
        blocks = [
            {
                **b,
                "start_line": max(0, b["start_line"] - context_lines),
                "end_line": min(len(lines) - 1, b["end_line"] + context_lines),
            }
            for b in blocks
        ]
        for b in blocks:
            b["text"] = "\n".join(lines[b["start_line"] : b["end_line"] + 1])
    toks = [
        t.lower()
        for t in re.findall(r"[A-Za-z_]\w{2,}", instruction)
        + re.findall(r"[\u4e00-\u9fff]{2,}", instruction)
    ]

    def score(b: dict) -> int:
        s = str(b.get("name", "")).lower()
        txt = str(b.get("text", "")).lower()
        return sum(1 for t in toks if t and (t in s or t in txt))

    blocks.sort(key=lambda b: (score(b), -b["start_line"]), reverse=True)
    top = blocks[:max_blocks]
    imports = [
        ln
        for ln in lines[:80]
        if re.match(r"^\s*(import |from |const .*require\(|using |#include )", ln)
    ]
    parts = [f"文件共 {len(lines)} 行，下面是相关代码块（行号基于原文件，1 起始）："]
    for b in sorted(top, key=lambda x: x["start_line"]):
        parts.append(f"\n### {b['name'] or '<匿名>'}(第 {b['start_line'] + 1}-{b['end_line'] + 1} 行)\n{b['text']}")
    if imports:
        parts.append("\n### 文件头部引用/导入\n" + "\n".join(imports[:30]))
    return "\n".join(parts)


_SEARCH_REPLACE_RE = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE", re.S
)


def parse_search_replace(text: str) -> list[dict]:
    """解析模型输出的 SEARCH/REPLACE 块."""
    blocks = []
    for m in _SEARCH_REPLACE_RE.finditer(text or ""):
        old = m.group(1)
        if old.strip():
            blocks.append({"old": old, "new": m.group(2)})
    return blocks


def apply_search_replace(content: str, blocks: list[dict]) -> tuple[str, list[str]]:
    """应用 SEARCH/REPLACE 块：精确匹配 → 空白归一化兜底；失败返回具体原因."""
    new_content = content
    failures: list[str] = []
    for i, b in enumerate(blocks, 1):
        old, new = b["old"], b["new"]
        cnt = new_content.count(old)
        if cnt == 1:
            new_content = new_content.replace(old, new)
            continue
        if cnt > 1:
            failures.append(f"块 {i}：SEARCH 文本出现 {cnt} 次，请补充上下文使匹配唯一（首行：{old.splitlines()[0][:60]}）。")
            continue
        # 空白归一化兜底：逐行 strip 后按滑动窗口找唯一匹配
        norm_lines = new_content.splitlines()
        norm_old = [ln.strip() for ln in old.splitlines()]
        if not norm_old:
            failures.append(f"块 {i}：SEARCH 为空。")
            continue
        idxs = [
            j
            for j in range(len(norm_lines) - len(norm_old) + 1)
            if norm_lines[j : j + len(norm_old)] == norm_old
        ]
        if len(idxs) == 1:
            j0 = idxs[0]
            new_lines = norm_lines[:j0] + new.splitlines() + norm_lines[j0 + len(norm_old) :]
            new_content = "\n".join(new_lines)
            continue
        first = old.splitlines()[0][:80] if old.splitlines() else ""
        failures.append(
            f"块 {i}：未找到 SEARCH 文本（含空白归一化后）。请逐字符复制文件原文，首行应为：{first}"
        )
    return new_content, failures
