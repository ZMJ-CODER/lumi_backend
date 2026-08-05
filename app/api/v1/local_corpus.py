"""本地加速支持 API —— 语料库版本 & 包列表."""

from fastapi import APIRouter, Depends

from app.core.deps import require_auth

router = APIRouter()


@router.get("/version")
async def get_local_corpus_version(payload: dict = Depends(require_auth)):
    """获取本地语料库版本."""
    # TODO: 返回最新版本号、更新说明
    return {"code": 0, "data": {"version": "1.0.0", "release_notes": ""}}


@router.get("/packages")
async def get_local_corpus_packages(payload: dict = Depends(require_auth)):
    """获取本地语料库包列表."""
    # TODO: 返回可下载的语料包列表
    return {"code": 0, "data": {"packages": []}}
