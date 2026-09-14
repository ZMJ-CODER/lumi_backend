"""``code.scan``：代码结构提取与行区间切片（**纯函数**，不依赖工作区/网络）。

为什么需要：现有读取能力很强，但**代码文件没有任何结构解析**——`.py/.ts/.java/...`
在读入时和纯文本一样按行顺序分段。于是"先看骨架再决定读哪段"这件事只能靠模型翻页，
既慢又容易把整篇正文灌进上下文。

本模块只做两件事，都是确定性的：

1. :func:`scan_text` 提取**骨架**：导入、类、函数/方法（名字、签名、行号区间、首行
   文档串）。函数体**一律不取**——骨架不是正文；
2. :func:`slice_lines` 按**行区间**切片：拿到骨架后精确读 `start_line..end_line`，
   而不是"从头顺序读"。文件不长时（≤ ``DEFAULT_READ_MAX_LINES``）直接给全文。

支持的语言：Python 走 ``ast``（精确、含嵌套类），其余（JS/TS/Java/Go/Rust/C/C++/Ruby/
PHP/C#/Kotlin/Scala/Swift/Shell/SQL…）走**保守正则**——宁可漏一个符号，也不乱报
（乱报会让模型去读一段根本不存在的结构）。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any

# ── 语言判定 ──────────────────────────────────────────────────────
_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".vue": "typescript",
    ".svelte": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".m": "objectivec",
    ".mm": "objectivec",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
}

#: 走 ``ast`` 精确解析的语言；其余走正则。
AST_LANGUAGES: frozenset[str] = frozenset({"python"})

#: 骨架条目上限（超出截断并标记，避免"清单本身"变成正文）。
MAX_SYMBOLS = 400
#: 单个符号签名长度上限。
MAX_SIGNATURE_CHARS = 240
#: 文件不超过这么多行时，``slice_text`` 直接给全文（省一次往返）。
DEFAULT_READ_MAX_LINES = 400
#: 切片上限（防止一次要 50 万行）。
MAX_SLICE_LINES = 2000


def language_for(path: str) -> str:
    """按扩展名判定语言；未知返回 ``"text"``。"""
    lowered = str(path or "").casefold()
    for ext, lang in _LANG_BY_EXT.items():
        if lowered.endswith(ext):
            return lang
    return "text"


def is_code_language(language: str) -> bool:
    return str(language or "") in set(_LANG_BY_EXT.values())


@dataclass(slots=True)
class CodeSymbol:
    """骨架里的一个符号（类/函数/方法/导入）。"""

    kind: str                      # class / function / method / import
    name: str
    line: int = 0
    end_line: int = 0
    signature: str = ""
    doc: str = ""
    parent: str = ""               # 方法所属类（Python 精确；其余留空）
    children: list["CodeSymbol"] = field(default_factory=list)

    def to_dict(self, *, include_children: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "line": int(self.line),
            "end_line": int(self.end_line),
        }
        if self.signature:
            payload["signature"] = self.signature
        if self.doc:
            payload["doc"] = self.doc
        if self.parent:
            payload["parent"] = self.parent
        if include_children and self.children:
            payload["children"] = [item.to_dict() for item in self.children]
        return payload


def _doc_first_line(node: Any, *, limit: int = 120) -> str:
    """只取文档串**首行**：骨架不搬运正文。"""
    try:
        raw = ast.get_docstring(node, clean=True) or ""
    except Exception:  # noqa: BLE001
        return ""
    first = next((line.strip() for line in str(raw).splitlines() if line.strip()), "")
    return first[:limit]


def _python_signature(node: Any) -> str:
    """``def f(a, b=1) -> int`` 形状的签名（不含函数体）。"""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args) if hasattr(ast, "unparse") else ""
    returns = ""
    if getattr(node, "returns", None) is not None and hasattr(ast, "unparse"):
        returns = f" -> {ast.unparse(node.returns)}"
    return f"{prefix} {node.name}({args}){returns}"[:MAX_SIGNATURE_CHARS]


def _python_symbols(source: str) -> tuple[list[CodeSymbol], list[str], list[str]]:
    """``ast`` 精确提取：导入、类（含嵌套类与方法）、顶层函数。

    只列**顶层**函数：函数体里的局部 ``def``（闭包）属于实现细节，列进骨架只会
    让清单变长而不帮助定位；类里的方法/嵌套类则会随类一起列出。

    返回 ``(符号, 导入, 说明)``；语法错误时符号为空、说明里给出**具体行号与原因**
    （"解析失败"必须如实报告，不能伪装成"空文件"）。
    """
    imports: list[str] = []
    symbols: list[CodeSymbol] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [], [], [f"Python 语法错误：第 {exc.lineno} 行 {exc.msg}"]

    def _import_name(node: ast.AST) -> list[str]:
        rows: list[str] = []
        if isinstance(node, ast.Import):
            rows.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            prefix = "." * int(getattr(node, "level", 0) or 0)
            for alias in node.names:
                rows.append(f"{prefix}{module}.{alias.name}" if module else f"{prefix}{alias.name}")
        return rows

    def _func(node: Any, *, parent: str = "") -> CodeSymbol:
        return CodeSymbol(
            kind="method" if parent else "function",
            name=str(node.name),
            line=int(getattr(node, "lineno", 0) or 0),
            end_line=int(getattr(node, "end_lineno", 0) or 0),
            signature=_python_signature(node),
            doc=_doc_first_line(node),
            parent=parent,
        )

    def _class(node: ast.ClassDef, *, parent: str = "") -> CodeSymbol:
        symbol = CodeSymbol(
            kind="class",
            name=str(node.name),
            line=int(getattr(node, "lineno", 0) or 0),
            end_line=int(getattr(node, "end_lineno", 0) or 0),
            signature=(
                f"class {node.name}({', '.join(ast.unparse(base) for base in node.bases)})"
                if node.bases and hasattr(ast, "unparse")
                else f"class {node.name}"
            )[:MAX_SIGNATURE_CHARS],
            doc=_doc_first_line(node),
            parent=parent,
        )
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol.children.append(_func(child, parent=node.name))
            elif isinstance(child, ast.ClassDef):
                symbol.children.append(_class(child, parent=node.name))
        return symbol

    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.extend(_import_name(node))
        elif isinstance(node, ast.ClassDef):
            symbols.append(_class(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(_func(node))
    return symbols, imports, []


# ── 保守正则（其余语言）──────────────────────────────────────────
# 每条都要求行首（允许前导空白）出现声明关键字，避免把调用/字符串当成声明。
_PATTERNS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "javascript": (
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function)")),
        ("import", re.compile(r"^\s*import\s+(?:[\w${}*,\s]+\s+from\s+)?['\"]([^'\"]+)['\"]")),
    ),
    "java": (
        ("class", re.compile(r"^\s*(?:public|private|protected|abstract|final|static|\s)*\s*(?:class|interface|enum|record)\s+([A-Za-z_$][\w$]*)")),
        ("method", re.compile(r"^\s*(?:public|private|protected|static|final|synchronized|abstract|native|\s)+[\w<>\[\],.\s?]+\s+([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?:throws [\w,.\s]+)?\{")),
        ("import", re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;")),
    ),
    "go": (
        ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
        ("class", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+struct\b")),
        ("class", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+interface\b")),
        ("import", re.compile(r"^\s*import\s+(?:\(\s*)?\"?([\w./-]+)\"?")),
    ),
    "rust": (
        ("function", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)")),
        ("class", re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")),
        ("import", re.compile(r"^\s*(?:pub\s+)?use\s+([\w:{}_,\s*]+);")),
    ),
    "c": (
        ("function", re.compile(r"^\s*(?:static\s+|inline\s+|extern\s+)*[\w*]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{")),
        ("class", re.compile(r"^\s*(?:typedef\s+)?(?:struct|union|enum)\s+([A-Za-z_]\w*)")),
        ("import", re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]")),
    ),
    "ruby": (
        ("class", re.compile(r"^\s*(?:class|module)\s+([A-Z]\w*(?:::\w+)*)")),
        ("function", re.compile(r"^\s*def\s+(self\.)?([A-Za-z_]\w*[?!=]?)")),
    ),
    "php": (
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)*(?:class|interface|trait)\s+([A-Za-z_]\w*)")),
        ("function", re.compile(r"^\s*(?:public|private|protected|static|\s)*function\s+([A-Za-z_]\w*)")),
        ("import", re.compile(r"^\s*use\s+([\w\\]+)")),
    ),
    "shell": (
        ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*\(\)\s*\{")),
    ),
    "sql": (
        ("class", re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w.\"`\[\]]+)", re.IGNORECASE)),
    ),
}
# TS/C#/Kotlin/Scala/Swift/PowerShell 复用 JS/Java 的形态再加各自关键字。
_PATTERNS["typescript"] = _PATTERNS["javascript"]
_PATTERNS["csharp"] = (
    ("class", re.compile(r"^\s*(?:public|private|protected|internal|abstract|sealed|static|partial|\s)*\s*(?:class|interface|struct|enum|record)\s+([A-Za-z_]\w*)")),
    ("method", re.compile(r"^\s*(?:public|private|protected|internal|static|virtual|override|async|sealed|\s)+[\w<>\[\],.\s?]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?")),
    ("import", re.compile(r"^\s*using\s+([\w.]+)\s*;")),
)
_PATTERNS["kotlin"] = (
    ("class", re.compile(r"^\s*(?:public|private|internal|open|abstract|sealed|data|\s)*\s*(?:class|interface|object)\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:public|private|internal|open|override|suspend|inline|\s)*fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)")),
    ("import", re.compile(r"^\s*import\s+([\w.*]+)")),
)
_PATTERNS["scala"] = _PATTERNS["kotlin"]
_PATTERNS["swift"] = (
    ("class", re.compile(r"^\s*(?:public|private|internal|open|final|\s)*\s*(?:class|struct|enum|protocol|extension)\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:public|private|internal|open|static|override|\s)*func\s+([A-Za-z_]\w*)")),
)
_PATTERNS["objectivec"] = _PATTERNS["c"]
_PATTERNS["cpp"] = _PATTERNS["c"]
_PATTERNS["powershell"] = (
    ("function", re.compile(r"^\s*function\s+([A-Za-z_][\w-]*)", re.IGNORECASE)),
    ("import", re.compile(r"^\s*(?:Import-Module|using\s+module)\s+([\w.\-\\/]+)", re.IGNORECASE)),
)


def _regex_symbols(source: str, language: str) -> tuple[list[CodeSymbol], list[str]]:
    patterns = _PATTERNS.get(language, ())
    symbols: list[CodeSymbol] = []
    imports: list[str] = []
    lines = source.splitlines()
    for index, line in enumerate(lines, start=1):
        for kind, pattern in patterns:
            match = pattern.match(line)
            if not match:
                continue
            name = str(match.group(match.lastindex or 1) or "").strip()
            if not name:
                continue
            cleaned = " ".join(line.split())[:MAX_SIGNATURE_CHARS]
            if kind == "import":
                if name not in imports:
                    imports.append(name)
                break
            symbols.append(
                CodeSymbol(
                    kind=kind,
                    name=name,
                    line=index,
                    # 正则无法可靠判定结束行：显式置 0 表示"未知"，不要瞎猜。
                    end_line=0,
                    signature=cleaned,
                    parent=name.split(".")[0] if kind == "function" and name.startswith("self.") else "",
                )
            )
            break
    return symbols, imports


def scan_text(
    source: str,
    *,
    path: str = "",
    max_symbols: int = MAX_SYMBOLS,
    include_imports: bool = True,
    include_children: bool = True,
) -> dict[str, Any]:
    """提取代码骨架（导入 + 类 + 函数/方法），不含函数体。

    返回 always-JSON-safe 的 dict：``{path, language, ok, imports, symbols, stats, notes}``。
    解析失败**不抛异常**：``ok=False`` + ``notes`` 说明原因，并退化为正则/纯文本统计。
    """
    text = str(source or "")
    language = language_for(path)
    lines = text.splitlines()
    notes: list[str] = []
    stats = {
        "lines": len(lines),
        "chars": len(text),
        "classes": 0,
        "functions": 0,
        "methods": 0,
        "imports": 0,
    }
    if language in AST_LANGUAGES:
        symbols, imports, notes = _python_symbols(text)
        if not symbols and not imports and not notes:
            notes.append("未提取到任何符号（文件可能为空或只有注释）")
    else:
        symbols, imports = _regex_symbols(text, language)
        if language == "text":
            notes.append("未知语言：按纯文本处理，未做结构解析")
        elif not symbols:
            notes.append("未识别到符号（该语言的声明写法可能不被保守正则覆盖）")
    # 统计必须**递归**：嵌套类里的方法也要算进去，否则"这个类有 3 个方法"会被
    # 报成 2 个，模型据此判断文件复杂度就会失真。
    counts = {"class": 0, "function": 0, "method": 0}

    def _count(items: list[CodeSymbol]) -> None:
        for item in items:
            if item.kind in counts:
                counts[item.kind] += 1
            if item.children:
                _count(item.children)

    _count(symbols)
    stats["classes"] = counts["class"]
    stats["functions"] = counts["function"]
    stats["methods"] = counts["method"]
    stats["imports"] = len(imports)

    truncated = False
    if len(symbols) > max_symbols:
        symbols = symbols[:max_symbols]
        truncated = True
        notes.append(f"符号超过 {max_symbols} 个，已截断")
    payload: dict[str, Any] = {
        "path": str(path or ""),
        "language": language,
        "ok": not any("语法错误" in note for note in notes),
        "symbols": [item.to_dict(include_children=include_children) for item in symbols],
        "stats": stats,
        "truncated": truncated,
        "notes": notes,
    }
    if include_imports:
        payload["imports"] = imports
    return payload


def slice_lines(
    source: str,
    *,
    start_line: int = 1,
    end_line: int = 0,
    max_lines: int = MAX_SLICE_LINES,
) -> dict[str, Any]:
    """按**行区间**切片（1-based，含端点）；不合法区间收敛为错误说明。"""
    lines = str(source or "").splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    end = int(end_line or 0) or total
    end = min(max(end, start), total)
    if start > total:
        return {
            "ok": False,
            "reason": f"start_line={start} 超出文件总行数 {total}",
            "total_lines": total,
            "start_line": start,
            "end_line": start,
            "text": "",
            "truncated": False,
        }
    if end - start + 1 > max_lines:
        end = start + max_lines - 1
    return {
        "ok": True,
        "reason": "",
        "total_lines": total,
        "start_line": start,
        "end_line": end,
        "text": "\n".join(lines[start - 1 : end]),
        "truncated": end < total,
    }


def slice_text(
    source: str,
    *,
    start_line: int = 0,
    end_line: int = 0,
    max_lines: int = DEFAULT_READ_MAX_LINES,
) -> dict[str, Any]:
    """读取入口用的"智能切片"：没给区间且文件不长 → 给全文；否则按区间给。"""
    total = len(str(source or "").splitlines())
    if not start_line and not end_line and total <= max_lines:
        return slice_lines(source, start_line=1, end_line=total, max_lines=max_lines) | {
            "full": True
        }
    result = slice_lines(
        source,
        start_line=start_line or 1,
        end_line=end_line or (start_line + max_lines - 1 if start_line else max_lines),
        max_lines=max_lines,
    )
    result["full"] = bool(result["ok"] and result["start_line"] == 1 and result["end_line"] == total)
    return result


def render_skeleton_lines(
    symbols: Any,
    *,
    imports: list[str] | None = None,
    stats: dict[str, Any] | None = None,
    max_lines: int = 200,
    max_signature_chars: int = 120,
) -> list[str]:
    """把骨架渲染成**紧凑文本**（模型读这一份，不必吞 JSON）。

    形如::

        - Outer [class] L8-20 — 外层类。
          - __init__ [method] L15-16 def __init__(self, name: str='x') -> None

    仅用于展示：函数体永远不在这里（骨架本来就没有正文）。
    """
    out: list[str] = []
    if stats:
        out.append(
            "统计：{classes} 个类 / {functions} 个函数 / {methods} 个方法 / "
            "{imports} 个导入 / 共 {lines} 行".format(
                classes=stats.get("classes", 0),
                functions=stats.get("functions", 0),
                methods=stats.get("methods", 0),
                imports=stats.get("imports", 0),
                lines=stats.get("lines", 0),
            )
        )
    if imports:
        shown = ", ".join(str(item) for item in imports[:40])
        more = f" 等 {len(imports)} 个" if len(imports) > 40 else ""
        out.append(f"导入：{shown}{more}")

    def _walk(items: Any, depth: int) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if len(out) >= max_lines:
                return
            start = int(item.get("line") or 0)
            end = int(item.get("end_line") or 0)
            where = f"L{start}" if not end or end <= start else f"L{start}-{end}"
            line = f"{'  ' * depth}- {item.get('name')} [{item.get('kind')}] {where}"
            signature = str(item.get("signature") or "")
            # 类的 signature 就是 "class X(Base)"，名字已经有了，重复没信息量。
            if signature and item.get("kind") != "class":
                line += f" {signature[:max_signature_chars]}"
            doc = str(item.get("doc") or "")
            if doc:
                line += f" — {doc[:60]}"
            out.append(line)
            _walk(item.get("children"), depth + 1)

    _walk(symbols, 0)
    if len(out) > max_lines:
        out = out[:max_lines]
        out.append(f"…（符号较多，仅显示前 {max_lines} 行；可用 kind/find 过滤）")
    return out


def find_symbol(payload: dict[str, Any], name: str) -> dict[str, Any] | None:
    """在骨架里按名字找符号（**递归**含子符号）；用于"定位到某个函数再读它的行区间"。"""
    target = str(name or "").strip()
    if not target:
        return None

    def _search(items: Any) -> dict[str, Any] | None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name")) == target:
                return item
            hit = _search(item.get("children"))
            if hit is not None:
                return hit
        return None

    nested = _search(payload.get("symbols"))
    if nested is not None:
        return nested
    # 顶层没直接命中时，允许用 "类.方法" 或 "类::方法" 定位（读骨架的人常这么写）。
    if "." in target or "::" in target:
        parts = [part for part in target.replace("::", ".").split(".") if part]
        if len(parts) == 2:
            owner = _search([item for item in payload.get("symbols") or [] if isinstance(item, dict)
                             and str(item.get("name")) == parts[0]])
            if owner is not None:
                return _search(owner.get("children"))
    return None


__all__ = [
    "AST_LANGUAGES",
    "CodeSymbol",
    "DEFAULT_READ_MAX_LINES",
    "MAX_SLICE_LINES",
    "MAX_SIGNATURE_CHARS",
    "MAX_SYMBOLS",
    "find_symbol",
    "is_code_language",
    "language_for",
    "render_skeleton_lines",
    "scan_text",
    "slice_lines",
    "slice_text",
]
