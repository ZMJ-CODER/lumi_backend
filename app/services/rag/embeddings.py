"""RAG 嵌入模块 —— 本地模型推理（sentence-transformers + bge）.

设计:
  - 默认模型 BAAI/bge-small-zh-v1.5（512 维），后期可直接切换 BAAI/bge-m3（1024 维）
  - 模型懒加载单例，CPU 推理；异步接口通过线程池执行，避免阻塞事件循环
  - 查询向量带检索指令前缀（bge 中文检索最佳实践），文档向量不带
  - 输出统一 L2 归一化，保证 pgvector 的余弦距离 = 1 - 余弦相似度
"""

import asyncio
import os
import threading

from loguru import logger

from app.core.config import settings

# 国内镜像兜底（仅影响模型下载；用户已设置 HF_ENDPOINT 时尊重原值）
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_model = None
_model_lock = threading.Lock()
_model_failed = False  # 加载失败后缓存，避免每次请求都重新尝试下载


def _resolve_device() -> str:
    """解析嵌入设备：配置了 cuda 但 torch 无 CUDA 支持时自动回退 cpu，避免整条链路失败."""
    device = settings.EMBEDDING_DEVICE or "cpu"
    if device == "cuda":
        try:
            import torch

            if not torch.cuda.is_available():
                logger.warning(
                    "EMBEDDING_DEVICE=cuda 但 torch 未编译 CUDA 支持（{}），回退到 cpu",
                    torch.__version__,
                )
                device = "cpu"
        except Exception:  # noqa: BLE001
            device = "cpu"
    return device


def _get_model():
    """懒加载模型单例，并校验输出维度与配置一致."""
    global _model, _model_failed
    if _model_failed:
        return None
    if _model is None:
        with _model_lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                device = _resolve_device()
                logger.info(
                    "⏳ 加载嵌入模型 {} (device={}) ...",
                    settings.EMBEDDING_MODEL,
                    device,
                )
                # 优先本地缓存加载（离线可用）；缓存不存在时回退在线下载
                try:
                    try:
                        _model = SentenceTransformer(
                            settings.EMBEDDING_MODEL,
                            device=device,
                            cache_folder=settings.EMBEDDING_CACHE_DIR or None,
                            local_files_only=True,
                        )
                    except Exception as e:
                        logger.info("本地无模型缓存，尝试在线下载: {}", e)
                        _model = SentenceTransformer(
                            settings.EMBEDDING_MODEL,
                            device=device,
                            cache_folder=settings.EMBEDDING_CACHE_DIR or None,
                        )
                    if hasattr(_model, "get_embedding_dimension"):
                        dim = _model.get_embedding_dimension()
                    else:
                        dim = _model.get_sentence_embedding_dimension()
                    if dim != settings.EMBEDDING_DIMENSION:
                        raise RuntimeError(
                            f"嵌入模型维度 {dim} 与 EMBEDDING_DIMENSION={settings.EMBEDDING_DIMENSION} 不一致，"
                            "请检查配置或数据库向量列维度"
                        )
                except Exception as e:  # noqa: BLE001
                    logger.error("嵌入模型加载失败（后续请求将降级跳过语义检索）: {}", e)
                    _model = None
                    _model_failed = True
                    return None
                logger.info(
                    "✅ 嵌入模型加载完成，维度={} device={}",
                    dim,
                    device,
                )
    if _model_failed:
        return None
    return _model


def _encode_sync(texts: list[str], is_query: bool = False) -> list[list[float]]:
    """同步编码（在子线程中执行）."""
    model = _get_model()
    if model is None:
        return []
    if is_query and settings.EMBEDDING_QUERY_INSTRUCTION:
        texts = [settings.EMBEDDING_QUERY_INSTRUCTION + t for t in texts]
    vectors = model.encode(
        texts,
        batch_size=settings.EMBEDDING_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
        convert_to_numpy=True,
    )
    return vectors.tolist()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量生成文档向量（不附加检索指令）."""
    if not texts:
        return []
    return await asyncio.to_thread(_encode_sync, list(texts), False)


async def embed_query(text: str) -> list[float]:
    """生成查询向量（附加 bge 检索指令前缀）."""
    if not text:
        return []
    vectors = await asyncio.to_thread(_encode_sync, [text], True)
    return vectors[0] if vectors else []
