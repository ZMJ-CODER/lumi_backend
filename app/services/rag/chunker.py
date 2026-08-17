"""结构化分块器 —— 按文档类型路由到不同分块策略.

路由:
  - 代码类（py/js/ts/c/java 等）→ 行级分块，按语句/缩进边界，不切语句中间
  - 配置类（json/yaml/xml）  → 按顶层结构边界分块，不拆散结构项
  - 表格类（csv/tsv）        → 表头 + 每 N 行一组
  - Markdown / Docling 输出   → 结构化分块：先识别 代码块/表格/标题/段落，
                                再按块类型选策略，标题作为上下文前缀
  - 纯文本（txt/log）        → 沿用递归字符切分

使用注册表（dict）分发：新增格式只需注册一个分块函数，无需改动分发逻辑。
"""

import re
from pathlib import Path

from app.services.rag.document_parser import split_text


# ── 代码分块 ─────────────────────────────────────────

_PY_TOP_LEVEL_RE = re.compile(r"^(def |class |if __name__|import |from |@)")


def chunk_code(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """行级代码分块：优先在语句/缩进边界断，保留完整逻辑单元."""
    lines = text.splitlines()
    if not lines:
        return []
    overlap_lines = max(0, overlap // 25)  # 代码按行重叠，默认 50 字符 → 2 行
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    prev_indent: int | None = None

    for line in lines:
        indent = len(line) - len(line.lstrip(" \t"))
        is_top = indent == 0 and bool(line.strip())

        # 断点 1：顶格新语句（def/class/import 等）且当前块已积累 → 新块
        if cur and is_top and _PY_TOP_LEVEL_RE.match(line) and len(cur) >= 2:
            chunks.append("\n".join(cur))
            cur = cur[-overlap_lines:]
            cur_len = sum(len(ln) + 1 for ln in cur)
        # 断点 2：缩进从深层回退到 0（代码块结束）且当前块已达半满
        elif cur and prev_indent is not None and prev_indent > 0 and indent == 0 and cur_len >= chunk_size // 2:
            chunks.append("\n".join(cur))
            cur = cur[-overlap_lines:]
            cur_len = sum(len(ln) + 1 for ln in cur)

        cur.append(line)
        cur_len += len(line) + 1
        prev_indent = indent

        # 超长强制断
        if cur_len >= chunk_size:
            chunks.append("\n".join(cur))
            cur = cur[-overlap_lines:]
            cur_len = sum(len(ln) + 1 for ln in cur)

    if cur:
        chunks.append("\n".join(cur))
    return [c.strip() for c in chunks if c.strip()]


# ── 配置类分块（json/yaml/xml）───────────────────────

def chunk_config(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按顶层结构边界分块：括号/方括号深度归零时作为候选断点."""
    lines = text.splitlines()
    if not lines:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    depth = 0
    cur_len = 0
    for line in lines:
        cur.append(line)
        depth += line.count("{") + line.count("[") - line.count("}") - line.count("]")
        cur_len += len(line) + 1
        if depth <= 0 and cur_len >= chunk_size:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
    if cur:
        chunks.append("\n".join(cur))
    return [c.strip() for c in chunks if c.strip()]


# ── 表格分块 ─────────────────────────────────────────

def chunk_table(text: str, chunk_size: int = 500, overlap: int = 50, max_rows: int = 10) -> list[str]:
    """Markdown 表格分块：小表整体保留，大表按"表头 + N 行"切."""
    lines = text.strip().splitlines()
    if not lines:
        return []
    if len(lines) <= max_rows + 2:
        return [text.strip()]
    header = lines[:2]  # 表头 + 分隔行
    rows = lines[2:]
    chunks = []
    for i in range(0, len(rows), max_rows):
        chunks.append("\n".join(header + rows[i : i + max_rows]))
    return chunks


def chunk_csv(text: str, chunk_size: int = 500, overlap: int = 50, max_rows: int = 50) -> list[str]:
    """CSV/TSV 分块：表头 + 每 N 行一组."""
    lines = text.strip().splitlines()
    if not lines:
        return []
    header = lines[0]
    rows = lines[1:]
    if len(rows) <= max_rows:
        return [text.strip()]
    return ["\n".join([header] + rows[i : i + max_rows]) for i in range(0, len(rows), max_rows)]


# ── Markdown / Docling 结构化分块 ─────────────────────

_CODE_FENCE_RE = re.compile(r"^\s*```")
_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


def _emit_section(lines: list[str], title: str, chunk_size: int, overlap: int) -> list[str]:
    """把一个小节切块；标题作为上下文前缀挂在每个子块上."""
    text = "\n".join(lines).strip()
    if not text:
        return []
    parts = split_text(text, chunk_size, overlap)
    if not title:
        return parts
    return [f"{title}\n\n{p}" for p in parts]


def chunk_structured(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """结构化 Markdown 分块：识别 代码块/表格/标题/段落，按块类型选策略."""
    lines = text.splitlines()
    chunks: list[str] = []
    section: list[str] = []
    section_title = ""
    i = 0

    while i < len(lines):
        line = lines[i]

        # 代码块：整块提取，按代码策略分块
        if _CODE_FENCE_RE.match(line):
            if section:
                chunks.extend(_emit_section(section, section_title, chunk_size, overlap))
                section, section_title = [], ""
            code_lines = [line]
            i += 1
            while i < len(lines) and not _CODE_FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                code_lines.append(lines[i])
                i += 1
            inner = "\n".join(code_lines[1:-1]) if len(code_lines) >= 2 else "\n".join(code_lines)
            chunks.extend(chunk_code(inner, chunk_size, overlap))
            continue

        # 表格：连续表格行整组提取，按表格策略分块
        if _TABLE_LINE_RE.match(line):
            if section:
                chunks.extend(_emit_section(section, section_title, chunk_size, overlap))
                section, section_title = [], ""
            table = [line]
            i += 1
            while i < len(lines) and _TABLE_LINE_RE.match(lines[i]):
                table.append(lines[i])
                i += 1
            chunks.extend(chunk_table("\n".join(table), chunk_size, overlap))
            continue

        # 标题：结束当前小节，开启新小节
        if _HEADING_RE.match(line):
            if section:
                chunks.extend(_emit_section(section, section_title, chunk_size, overlap))
            section_title = line.strip()
            section = []
            i += 1
            continue

        # 普通行 → 当前小节
        section.append(line)
        i += 1

    if section:
        chunks.extend(_emit_section(section, section_title, chunk_size, overlap))
    return [c for c in chunks if c.strip()]


# ── 注册表与统一入口 ──────────────────────────────────

CHUNKERS: dict[str, object] = {
    # 代码类
    ".py": chunk_code, ".js": chunk_code, ".jsx": chunk_code, ".ts": chunk_code, ".tsx": chunk_code,
    ".c": chunk_code, ".cpp": chunk_code, ".h": chunk_code, ".hpp": chunk_code,
    ".java": chunk_code, ".go": chunk_code, ".rs": chunk_code, ".rb": chunk_code,
    ".php": chunk_code, ".cs": chunk_code, ".kt": chunk_code, ".swift": chunk_code, ".sh": chunk_code,
    # 配置类
    ".json": chunk_config, ".yaml": chunk_config, ".yml": chunk_config, ".xml": chunk_config,
    # 表格类
    ".csv": chunk_csv, ".tsv": chunk_csv,
    # 纯文本
    ".txt": split_text, ".log": split_text,
}


def chunk_document(
    text: str,
    filename: str | None = None,
    chunk_size: int = 500,
    overlap: int = 50,
) -> list[str]:
    """统一分块入口：按扩展名路由到对应策略，未注册的走结构化 Markdown 分块."""
    ext = Path(filename or "").suffix.lower()
    chunker = CHUNKERS.get(ext, chunk_structured)
    return chunker(text, chunk_size, overlap)
