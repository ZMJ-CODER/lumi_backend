"""基于 Docling 的办公文档解析（PDF / Word / PPT / Excel / 图片 OCR 等）.

Docling 首次使用时需要下载布局 / OCR 模型（HuggingFace，已配置国内镜像兜底）。
纯文本格式仍走 document_parser 的内置解析，避免不必要的模型加载。
"""

import os
import contextlib
import io
import threading
from pathlib import Path

from loguru import logger

from app.core.config import settings

# 国内镜像兜底（用户已设置 HF_ENDPOINT 时尊重原值）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
# huggingface_hub 1.x 默认走 xet 协议，文件实际从 cas-server.xethub.hf.co 拉取，
# 国内网络不可达（401/超时）；禁用后回退普通 HTTP，经 HF_ENDPOINT 镜像下载。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
# Windows 无符号链接权限时静默降级缓存（避免每次下载打印警告）
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
# Docling 布局模型默认 torch.compile，GPU 路径依赖 Triton（Windows 无 Triton 会直接失败）；
# 关闭编译走 eager 推理，牺牲一点速度换取 Windows 可用。
os.environ.setdefault("DOCLING_INFERENCE_COMPILE_TORCH_MODELS", "false")
# 后端 stderr 被重定向到文件时，tqdm 进度条刷新 sys.stderr 会抛 OSError(22) 并中断模型加载；
# 禁用进度条（若 tqdm 尚未 import 则生效）。
os.environ.setdefault("TQDM_DISABLE", "1")

# huggingface_hub 的 HF_HUB_DISABLE_XET / HF_ENDPOINT 在 constants 模块 import 时即被冻结。
# 应用启动时 embedding（sentence-transformers）已先导入 huggingface_hub，此时再 setdefault
# 环境变量不生效，模型下载仍走 XET CAS（cas-server.xethub.hf.co）→ 国内网络 401 失败。
# 这里直接改写其常量，保证无论 import 顺序都走镜像的普通 HTTP 下载。
try:
    import huggingface_hub.constants as _hf_const  # noqa: PLC0415

    _hf_const.HF_HUB_DISABLE_XET = True
    _hf_const.HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
except Exception:  # noqa: BLE001
    pass

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

                # 兜底：无论环境变量是否已生效，强制关闭 torch.compile（Windows 无 Triton）
                try:
                    from docling.datamodel import settings as _dl_settings

                    _dl_settings.settings.inference.compile_torch_models = False
                except Exception:  # noqa: BLE001
                    pass
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
        # tqdm 的 status_printer 会 flush 全局 sys.stderr；后端 stderr 指向被重定向的文件句柄时
        # Windows 下 flush 可能抛 OSError(22)。转换期间把 stderr 重定向到内存缓冲，彻底绕开。
        with contextlib.redirect_stderr(io.StringIO()):
            result = converter.convert(Path(file_path))
        markdown = result.document.export_to_markdown()
        logger.debug("Docling 解析完成: {} -> {} 字符", filename or file_path, len(markdown))
        if markdown.strip():
            return markdown
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=True).warning(
            "Docling 解析失败（尝试降级 pypdf）: {}", str(exc)[:300]
        )
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
        logger.opt(exception=True).warning("pypdf 降级解析失败: {}", str(exc)[:300])
    raise ValueError("文档解析失败：无可提取文本（扫描件需要 Docling OCR，当前模型不可用）")
