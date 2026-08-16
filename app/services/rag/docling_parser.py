"""基于 Docling 的办公文档解析（PDF / Word / PPT / Excel / 图片 OCR 等）.

Docling 首次使用时需要下载布局 / OCR 模型（HuggingFace，已配置国内镜像兜底）。
纯文本格式仍走 document_parser 的内置解析，避免不必要的模型加载。
"""

import os
import threading
from pathlib import Path

from loguru import logger

from app.core.config import settings

# 国内镜像兜底（用户已设置 HF_ENDPOINT 时尊重原值）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_converter = None
_lock = threading.Lock()


def _get_converter():
    """懒加载 Docling DocumentConverter 单例."""
    global _converter
    if _converter is None:
        with _lock:
            if _converter is None:
                from docling.datamodel.base_models import InputFormat
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption

                pipeline_options = PdfPipelineOptions()
                pipeline_options.do_ocr = settings.DOCLING_ENABLE_OCR
                logger.debug("Docling 初始化：OCR={}", pipeline_options.do_ocr)

                _converter = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                    }
                )
    return _converter


def parse_with_docling(file_path: str, filename: str | None = None) -> str:
    """使用 Docling 解析文档并导出为 Markdown 文本."""
    name = filename or file_path
    is_pdf = Path(name).suffix.lower() == ".pdf"
    # 纯文本 PDF 优先走 pypdf（无需下载布局/OCR 模型，快且稳）；扫描件再由 Docling OCR 兜底
    if is_pdf:
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            parts = []
            for page in reader.pages:
                try:
                    t = page.extract_text() or ""
                except Exception:  # noqa: BLE001
                    t = ""
                if t.strip():
                    parts.append(t)
            text = "\n".join(parts).strip()
            if text:
                logger.debug("pypdf 解析完成: {} -> {} 字符", name, len(text))
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("pypdf 解析失败（走 Docling）: {}", str(exc)[:200])
    try:
        converter = _get_converter()
        logger.debug("Docling 解析文档: {}", filename or file_path)
        result = converter.convert(Path(file_path))
        markdown = result.document.export_to_markdown()
        logger.debug("Docling 解析完成: {} -> {} 字符", filename or file_path, len(markdown))
        if markdown.strip():
            return markdown
    except Exception as exc:  # noqa: BLE001
        logger.warning("Docling 解析失败（尝试降级 pypdf）: {}", str(exc)[:200])
    # 降级：纯文本 PDF 用 pypdf 直接提取（无需下载布局/OCR 模型）
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(file_path))
        parts = []
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if t.strip():
                parts.append(t)
        text = "\n".join(parts).strip()
        if text:
            logger.debug("pypdf 降级解析完成: {} -> {} 字符", filename or file_path, len(text))
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("pypdf 降级解析失败: {}", str(exc)[:200])
    raise ValueError("PDF 解析失败：文档无可提取文本（扫描件需要 Docling OCR，当前模型不可用）")
