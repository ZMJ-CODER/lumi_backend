"""文档解析与分块 —— 纯文本 v1，解析器按扩展名注册."""

from pathlib import Path


# 支持的纯文本扩展名
_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".csv", ".tsv", ".log",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css",
    ".yaml", ".yml", ".ini", ".toml", ".xml",
}


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
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # 兜底：常见中文编码
        for encoding in ("gb18030", "utf-8-sig", "big5"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("文件编码无法识别（支持 UTF-8 / GB18030）")


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
