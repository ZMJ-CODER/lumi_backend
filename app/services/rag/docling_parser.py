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
                logger.info("Docling 初始化：OCR={}", pipeline_options.do_ocr)

                _converter = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
                    }
                )
    return _converter


def parse_with_docling(file_path: str, filename: str | None = None) -> str:
    """使用 Docling 解析文档并导出为 Markdown 文本."""
    converter = _get_converter()
    logger.info("Docling 解析文档: {}", filename or file_path)
    result = converter.convert(Path(file_path))
    markdown = result.document.export_to_markdown()
    logger.info("Docling 解析完成: {} -> {} 字符", filename or file_path, len(markdown))
    return markdown
