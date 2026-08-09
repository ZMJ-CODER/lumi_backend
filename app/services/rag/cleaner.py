"""RAG 文本清洗层 —— 提高文档有效转化率.

职责:
  1. 字符级清洗：控制符 / 零宽字符 / 替换符 / 乱码修复 / 全半角统一
  2. 空白级清洗：压缩连续空行、去行尾空格
  3. 结构级清洗：代码单行恢复换行、去重复页眉页脚噪声
  4. 质量门：输出 0~1 质量分与原因，低质量文档不入库

注意：清洗规则是启发式的，目标是把"明显不可用"的文本救回来，
质量门负责把救不回来的拒之门外，而不是追求完美还原。
"""

import re
from collections import Counter
from pathlib import Path

# ── 正则与字符集 ─────────────────────────────────────

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")
_REPLACEMENT_RE = re.compile(r"[\ufffd]")

# 可读字符：中日韩、拉丁、西里尔、字母数字
_READABLE_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
    r"A-Za-z0-9\u00c0-\u024f\u0400-\u04ff]"
)

# 常见 mojibake 标记（UTF-8 字节被误按 latin-1 解码）
_MOJIBAKE_MARKERS = ("Ã", "â€", "æ", "å", "ç", "œ", "Â")

# 全角 → 半角（ASCII 范围）
_FULLWIDTH_MAP = str.maketrans(
    "０１２３４５６７８９ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ"
    "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
    "！＂＃＄％＆＇（）＊＋，－．／：；＜＝＞？＠［＼］＾＿｀｛｜｝～　",
    "0123456789abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~ ",
)

# 代码文件扩展名（走代码清洗路径）
CODE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".c", ".cpp", ".h", ".hpp",
    ".java", ".go", ".rs", ".rb", ".php", ".cs", ".kt", ".swift", ".sh",
}

# 花括号语言（按 ; { } 恢复换行）
_BRACE_EXTS = {
    ".js", ".jsx", ".ts", ".tsx", ".c", ".cpp", ".h", ".hpp",
    ".java", ".go", ".rs", ".php", ".cs", ".kt", ".swift",
}

# 语句起始关键字：前面是标识符也断开（如 "os def"、"1 if"）
_PY_START_KEYWORDS = (
    "def ", "class ", "if ", "elif ", "else:", "for ", "while ", "return ",
    "with ", "try:", "except ", "finally:", "raise ", "assert ", "yield ",
    "print(", "async ", "await ",
)

# 导入关键字：仅在前一字符不是标识符时断开（避免拆散 "from x import y"）
_PY_IMPORT_KEYWORDS = ("import ", "from ")


class DocumentQualityError(ValueError):
    """文档质量不达标（终止处理，不重试）."""


# ── 低质量文档分类 ───────────────────────────────────

QUALITY_ISSUES: dict[str, str] = {
    "unparsable": "无法解析（如严重损坏的PDF）",
    "encoding_errors": "编码错误导致乱码",
    "corrupted_data": "数据损坏",
    "incomplete_parsing": "解析不完整",
    "security_vulnerabilities": "包含安全风险的代码",
    "malicious_content": "恶意内容",
    "extreme_noise": "噪声占比>50%",
    "unintelligible_text": "完全无法理解的文本",
}

# 命中即硬性拦截的分类（无论质量分高低都不入库）
HARD_FAIL_CODES = {
    "unparsable",
    "corrupted_data",
    "security_vulnerabilities",
    "malicious_content",
    "extreme_noise",
    "unintelligible_text",
}

# 安全风险代码模式（仅对代码文件检测）
_SECURITY_PATTERNS = (
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\bos\.system\s*\(",
    r"subprocess[^;\n]{0,60}shell\s*=\s*True",
    r"__import__\s*\(",
    r"pickle\.loads\s*\(",
    r"\bnew\s+Function\s*\(",
    r"innerHTML\s*=",
    r"document\.write\s*\(",
    r"dangerouslySetInnerHTML",
    r"rm\s+-(r)?f\s+[/~]",
    r"base64\s+-\s*d",
)

# 恶意内容模式（任意文档检测）
_MALICIOUS_PATTERNS = (
    r"powershell[^;\n]{0,80}-enc",
    r"certutil\s+-urlcache",
    r"mshta\s+",
    r"regsvr32[^;\n]{0,30}/s",
    r"wscript\.shell",
    r"rundll32\s+",
    r"vbaProject\.bin",
    r"hxxps?://",
    r"eval\(base64",
)


# ── 字符级清洗 ───────────────────────────────────────

def _readable_ratio(text: str) -> float:
    if not text:
        return 0.0
    return len(_READABLE_RE.findall(text)) / len(text)


def _fix_mojibake(text: str) -> str:
    """尝试修复 UTF-8 被误按 latin-1 解码的乱码（如 ä¸­æ–‡ → 中文）."""
    if not any(m in text for m in _MOJIBAKE_MARKERS):
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # 只有修复后可读性明显提升才采用
    if _readable_ratio(fixed) > _readable_ratio(text) + 0.05:
        return fixed
    return text


def _normalize_whitespace(text: str) -> str:
    """压缩连续空行（最多 2 个）、去行尾空格."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if not ln.strip():
            blank += 1
            if blank <= 2:
                out.append("")
        else:
            blank = 0
            out.append(ln)
    return "\n".join(out).strip("\n") + "\n"


def _remove_repeated_noise_lines(text: str) -> str:
    """去掉重复出现的短行（常见于页眉/页脚/页码噪声）."""
    lines = text.splitlines()
    counts = Counter(ln.strip() for ln in lines if len(ln.strip()) <= 60)
    repeated = {ln for ln, c in counts.items() if c >= 6}
    if not repeated:
        return text
    return "\n".join(ln for ln in lines if ln.strip() not in repeated)


def _repetition_ratio(text: str) -> float:
    """单个字符在去空白文本中的最高占比（重复噪声检测）."""
    non_space = re.sub(r"\s+", "", text)
    if not non_space:
        return 1.0
    top = Counter(non_space).most_common(1)[0][1]
    return top / len(non_space)


# ── 结构级清洗：代码单行恢复 ─────────────────────────

def _restore_brace_code(text: str) -> str:
    """把被压成单行的花括号语言代码恢复为多行."""
    text = re.sub(r";(?=\s)", ";\n", text)
    text = re.sub(r"\{\s*", "{\n", text)
    text = re.sub(r"\s*\}", "\n}", text)
    return text


def _restore_python_code(text: str) -> str:
    """把被压成单行的 Python 代码按语句关键字恢复换行."""
    for kw in _PY_IMPORT_KEYWORDS:
        pattern = rf"(?<![\w.])(?<!\n)\s+({re.escape(kw)})"
        text = re.sub(pattern, r"\n\1", text)
    for kw in _PY_START_KEYWORDS:
        pattern = rf"(?<!\n)\s+({re.escape(kw)})"
        text = re.sub(pattern, r"\n\1", text)
    text = re.sub(r";\s+", ";\n", text)
    return text


# ── 对外接口 ─────────────────────────────────────────

def clean_text(text: str) -> str:
    """通用文本清洗."""
    if not text:
        return ""
    text = _fix_mojibake(text)
    text = _CONTROL_RE.sub("", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = _REPLACEMENT_RE.sub("", text)
    text = text.translate(_FULLWIDTH_MAP)
    text = _normalize_whitespace(text)
    text = _remove_repeated_noise_lines(text)
    return text.strip() + "\n" if text.strip() else ""


def clean_code_text(text: str, ext: str) -> str:
    """代码文本清洗：先通用清洗，再尝试恢复被压成单行的代码."""
    if not text:
        return ""
    text = clean_text(text)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""

    # 平均行已经足够短 → 结构正常，无需恢复
    total_len = sum(len(ln) for ln in lines)
    if len(lines) >= 3 and total_len / len(lines) < 200:
        return text

    if ext in _BRACE_EXTS:
        text = _restore_brace_code(text)
    else:
        text = _restore_python_code(text)
    return _normalize_whitespace(text)


def clean_document(text: str, filename: str | None = None) -> str:
    """按文件类型选择清洗路径：代码走代码清洗，其余走通用清洗."""
    ext = Path(filename or "").suffix.lower()
    if ext in CODE_EXTS:
        return clean_code_text(text, ext)
    return clean_text(text)


def assess_document(
    text: str,
    filename: str | None = None,
    file_size: int | None = None,
) -> tuple[float, list[dict[str, str]]]:
    """评估清洗后文档质量，输出分类问题清单.

    Returns:
        (0~1 分数, 问题列表 [{code, message}])
    """
    issues: list[dict[str, str]] = []
    if not text or not text.strip():
        return 0.0, [{"code": "unparsable", "message": QUALITY_ISSUES["unparsable"]}]

    total = len(text)
    # 可读率：先剥掉 Markdown 表格/代码的纯语法字符（| - # ` * _ > ~），避免误判
    content_text = re.sub(r"[|\-`#*_>~]", "", text)
    readable = _readable_ratio(content_text)
    bad_chars = (
        len(_CONTROL_RE.findall(text))
        + len(_ZERO_WIDTH_RE.findall(text))
        + len(_REPLACEMENT_RE.findall(text))
    )
    mojibake_chars = sum(text.count(m) for m in _MOJIBAKE_MARKERS)

    # 编码错误导致乱码
    if mojibake_chars > total * 0.05:
        issues.append({"code": "encoding_errors", "message": QUALITY_ISSUES["encoding_errors"]})

    # 数据损坏：控制字符/替换符/乱码占比过高
    if total and (bad_chars + mojibake_chars) / total > 0.2:
        issues.append({"code": "corrupted_data", "message": QUALITY_ISSUES["corrupted_data"]})

    # 解析不完整：文件很大但提取文本很少
    if file_size and file_size > 50_000 and len(text.strip()) < 200:
        issues.append({"code": "incomplete_parsing", "message": QUALITY_ISSUES["incomplete_parsing"]})

    # 极端噪声：可读字符占比 < 50%
    ext = Path(filename or "").suffix.lower()
    noise_threshold = 0.2 if ext in CODE_EXTS else 0.5  # 代码文件本身含大量标点，放宽
    if readable < noise_threshold:
        issues.append({"code": "extreme_noise", "message": QUALITY_ISSUES["extreme_noise"]})

    # 完全无法理解：单个字符重复占比过高
    if len(text.strip()) >= 20 and _repetition_ratio(text) > 0.6:
        issues.append({"code": "unintelligible_text", "message": QUALITY_ISSUES["unintelligible_text"]})

    # 安全风险代码（仅代码文件）
    if ext in CODE_EXTS and any(re.search(p, text, re.IGNORECASE) for p in _SECURITY_PATTERNS):
        issues.append({"code": "security_vulnerabilities", "message": QUALITY_ISSUES["security_vulnerabilities"]})

    # 恶意内容（任意文档）
    if any(re.search(p, text, re.IGNORECASE) for p in _MALICIOUS_PATTERNS):
        issues.append({"code": "malicious_content", "message": QUALITY_ISSUES["malicious_content"]})

    # 综合质量分
    score = max(0.0, 1.0 - (bad_chars + mojibake_chars) / total)
    if readable < 0.3:
        score = min(score, readable * 2)
    if len(text.strip()) < 20:
        score = min(score, 0.3)
    if any(i["code"] in HARD_FAIL_CODES for i in issues):
        score = min(score, 0.1)  # 硬性问题压到阈值之下，保证拦截

    return round(score, 4), issues


def quality_score(text: str) -> tuple[float, list[str]]:
    """兼容接口：仅按文本评估，返回（分数, 原因文本列表）."""
    score, issues = assess_document(text)
    return score, [i["message"] for i in issues]
