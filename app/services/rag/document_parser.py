"""RAG 文档解析与分块 —— 纯文本 v1，解析器按扩展名注册."""

from datetime import datetime
from pathlib import Path
import csv
import io
from dataclasses import dataclass


@dataclass(slots=True)
class ParsedSegment:
    """解析器向分块器提供的最小结构化契约。"""

    text: str
    page_start: int | None = None
    page_end: int | None = None
    heading_path: str | None = None


@dataclass(slots=True)
class ParsedDocument:
    """正文与可验证来源元数据；缺失字段必须保持 None，禁止猜测。"""

    text: str
    segments: list[ParsedSegment]
    parser: str


def _parse_pdf_segments(file_path: str) -> list[ParsedSegment]:
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    segments: list[ParsedSegment] = []
    for page_no, page in enumerate(reader.pages, 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001
            text = ""
        if text.strip():
            segments.append(ParsedSegment(text=text.strip(), page_start=page_no, page_end=page_no))
    return segments


# 支持的纯文本扩展名
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css",
    ".yaml", ".yml", ".ini", ".toml", ".xml",
}

# 结构化文本格式：不走 Docling，单独解析成可读文本
_STRUCTURED_TEXT_EXTS = {".eml", ".ics"}


def _strip_html(html: str) -> str:
    """粗略剥离 HTML 标签，保留文本（用于 HTML 邮件正文）."""
    from html.parser import HTMLParser

    class _TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            if data.strip():
                self.parts.append(data.strip())

        def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ARG002
            if tag in ("p", "br", "div", "li", "tr", "h1", "h2", "h3"):
                self.parts.append("\n")

    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 - HTML 异常不阻塞正文提取
        return html
    return "\n".join(x for x in parser.parts if x)


def parse_eml(file_path: str) -> str:
    """EML 邮件 → 可读文本（发件人/收件人/主题/日期 + 正文）."""
    import email
    from email import policy

    raw = Path(file_path).read_bytes()
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:  # noqa: BLE001 - 解析失败降级为原始文本
        return raw.decode("utf-8", errors="replace")

    lines: list[str] = []

    def _field(name: str, label: str) -> None:
        v = (msg.get(name) or "").strip()
        if v:
            lines.append(f"{label}：{v}")

    _field("From", "发件人")
    _field("To", "收件人")
    _field("Cc", "抄送")
    _field("Subject", "主题")
    _field("Date", "日期")

    body_parts: list[str] = []
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    try:
                        body_parts.append(str(part.get_content() or ""))
                    except Exception:  # noqa: BLE001
                        payload = part.get_payload(decode=True) or b""
                        body_parts.append(payload.decode("utf-8", errors="replace"))
                elif ctype == "text/html" and not body_parts:
                    html = str(part.get_content() or "")
                    body_parts.append(_strip_html(html))
        else:
            ctype = msg.get_content_type()
            if ctype == "text/plain":
                body_parts.append(str(msg.get_content() or ""))
            elif ctype == "text/html":
                body_parts.append(_strip_html(str(msg.get_content() or "")))
    except Exception:  # noqa: BLE001
        pass

    body = "\n".join(p.strip() for p in body_parts if p and p.strip())
    if body:
        lines.append("\n正文：\n" + body)
    return "\n".join(lines) or "（空邮件）"


def _fmt_ics_datetime(value: str) -> str:
    """ICS 时间值（20260814T070000Z / 20260814）→ 可读时间."""
    if not value:
        return ""
    v = value.replace("Z", "").replace("T", " ")
    try:
        if " " in v:
            return datetime.strptime(v[:15], "%Y%m%d %H%M%S").strftime("%Y-%m-%d %H:%M")
        return datetime.strptime(v[:8], "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return value


def parse_ics(file_path: str) -> str:
    """ICS 日历 → 可读文本（逐个事件：时间/主题/地点/说明）."""
    raw = Path(file_path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    # 展开折叠行（续行以空格/制表符开头）
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)

    events: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    in_event = False
    for line in lines:
        key, _, value = line.partition(":")
        # DTSTART;TZID=Asia/Shanghai 等属性不属于字段名本身。
        key = key.split(";", 1)[0].upper().strip()
        if key == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {}
            in_event = True
            continue
        if key == "END" and value.strip().upper() == "VEVENT":
            if cur.get("SUMMARY"):
                events.append(cur)
            cur = {}
            in_event = False
            continue
        if not in_event or key not in (
            "SUMMARY", "DTSTART", "DTEND", "LOCATION", "DESCRIPTION", "STATUS",
        ):
            continue
        cur[key] = (
            value.strip()
            .replace("\\n", "\n")
            .replace("\\,", ",")
            .replace("\\;", ";")
            .replace("\\\\", "\\")
        )

    out: list[str] = []
    for i, ev in enumerate(events, 1):
        out.append(f"事件{i}：{ev.get('SUMMARY', '')}")
        start = _fmt_ics_datetime(ev.get("DTSTART", ""))
        end = _fmt_ics_datetime(ev.get("DTEND", ""))
        if start:
            out.append(f"  开始：{start}" + (f"  结束：{end}" if end else ""))
        if ev.get("LOCATION"):
            out.append(f"  地点：{ev['LOCATION']}")
        if ev.get("DESCRIPTION"):
            out.append(f"  说明：{ev['DESCRIPTION']}")
        if ev.get("STATUS"):
            out.append(f"  状态：{ev['STATUS']}")
    return "\n".join(out) or "（日历中没有事件）"


def parse_file(file_path: str, filename: str | None = None) -> str:
    """读取文件内容为纯文本.

    Args:
        file_path: 磁盘上的文件路径
        filename: 原始文件名（用于扩展名判断，缺省用 file_path）

    Raises:
        ValueError: 不支持的格式或文件无法解码
    """
    name = filename or file_path
    ext = Path(name).suffix.lower()
    if ext not in _TEXT_EXTS:
        raise ValueError(
            f"暂不支持 {ext or '无扩展名'} 格式，请使用文本文件（txt / md / json / csv 等）"
        )

    raw = Path(file_path).read_bytes()
    text = None
    # utf-8-sig 放在 utf-8 前，顺便移除 CSV/文本常见 BOM。
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("文件编码无法识别（支持 UTF-8 / GB18030 / Big5）")
    if ext in {".csv", ".tsv"}:
        return _format_delimited_text(text, delimiter="\t" if ext == ".tsv" else None)
    return text


def _format_delimited_text(text: str, delimiter: str | None = None) -> str:
    """CSV/TSV 转成带行号的稳定表格文本，保留引号、换行字段和列关系。"""
    sample = text[:8192]
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        return "（空表格）"
    width = max(len(row) for row in rows)
    header = rows[0] + [""] * (width - len(rows[0]))
    lines = ["表头：" + " | ".join(str(cell).strip() for cell in header)]
    for index, row in enumerate(rows[1:], 1):
        padded = row + [""] * (width - len(row))
        lines.append(f"第{index}行：" + " | ".join(str(cell).strip() for cell in padded))
    return "\n".join(lines)


def parse_document(file_path: str, filename: str | None = None) -> str:
    """统一文档解析入口：纯文本格式走内置解析，其余格式（PDF/Office/图片等）走 Docling.

    Args:
        file_path: 磁盘上的文件路径
        filename: 原始文件名（用于扩展名判断，缺省用 file_path）

    Raises:
        ValueError: 解析失败（Docling 不支持或模型不可用）
    """
    name = filename or file_path
    ext = Path(name).suffix.lower()
    if ext in _TEXT_EXTS:
        return parse_file(file_path, filename)
    if ext == ".eml":
        return parse_eml(file_path)
    if ext == ".ics":
        return parse_ics(file_path)

    # 延迟导入 Docling，避免纯文本流程加载重型依赖
    from app.services.rag.docling_parser import parse_with_docling

    return parse_with_docling(file_path, filename)


def parse_document_with_metadata(file_path: str, filename: str | None = None) -> ParsedDocument:
    """解析正文并保留可验证 provenance。

    旧调用继续使用 ``parse_document``。PDF 优先按页解析，因此页码不会在进入
    分块阶段前丢失；Docling 尚未暴露稳定映射时只返回无页码 segment，绝不推算页码。
    """
    name = filename or file_path
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        try:
            segments = _parse_pdf_segments(file_path)
            if segments:
                return ParsedDocument(
                    text="\n\n".join(segment.text for segment in segments),
                    segments=segments,
                    parser="pypdf",
                )
        except Exception:  # noqa: BLE001
            pass
    if ext not in _TEXT_EXTS and ext not in {".eml", ".ics"}:
        try:
            from app.services.rag.docling_parser import parse_with_docling_metadata

            return parse_with_docling_metadata(file_path, filename)
        except Exception:  # noqa: BLE001
            pass
    text = parse_document(file_path, filename)
    return ParsedDocument(text=text, segments=[ParsedSegment(text=text)], parser="text_or_docling")


# 分隔符优先级：先按大块切，再按句子、标点、空格兜底
_SEPARATORS = ["\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; ", "，", ", ", " ", ""]


def split_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """按优先级切分长文本为重叠分块.

    Args:
        text: 原始文本
        chunk_size: 单块最大字符数
        overlap: 相邻块重叠字符数
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            # 在当前窗口内找最后一个分隔符，尽量在语义边界切断
            best = -1
            for sep in _SEPARATORS:
                idx = text.rfind(sep, start + 1, end)
                if idx != -1:
                    best = idx + len(sep)
                    break
            if best != -1:
                end = best

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)

        if end >= len(text):
            break
        # 带重叠前进；保证至少前进 1 字符，避免死循环
        start = max(end - overlap, start + 1)

    return chunks
