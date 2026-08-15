"""办公文档编辑 API：上传 → 读取结构 → LLM 规划编辑 → 缓冲 → 取回落盘/丢弃."""

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.services import office_docs

router = APIRouter()


class EditRequest(BaseModel):
    instruction: str


class AnalyzeRequest(BaseModel):
    instruction: str
    mode: str = "qa"  # qa / summary


def _session_or_404(user_id: str, doc_id: str) -> dict:
    try:
        return office_docs.load_session(user_id, doc_id)
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc


@router.post("")
async def upload_office_doc(
    file: UploadFile = File(...),
    payload: dict = Depends(require_auth),
):
    """上传办公文档，建立编辑会话，返回文档结构与会话 id."""
    content = await file.read()
    filename = file.filename or "document.txt"
    try:
        meta = office_docs.create_session(payload["sub"], filename, content)
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    info = office_docs.read_structure(payload["sub"], meta["doc_id"])
    return {
        "code": 0,
        "data": {
            "doc_id": info["doc_id"],
            "kind": info["kind"],
            "filename": info["filename"],
            "structure": info["structure"],
            "size": len(content),
        },
    }


@router.get("/{doc_id}")
async def get_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """查看文档结构与缓冲状态."""
    info = office_docs.read_structure(payload["sub"], doc_id)
    meta = _session_or_404(payload["sub"], doc_id)
    from pathlib import Path

    buffered = Path(meta["_session"]) / f"buffered{meta['ext']}"
    return {
        "code": 0,
        "data": {
            **info,
            "has_buffer": buffered.exists(),
            "committed": meta.get("committed", False),
        },
    }


@router.post("/{doc_id}/edit")
async def edit_office_doc(
    doc_id: str,
    req: EditRequest,
    payload: dict = Depends(require_auth),
):
    """按自然语言指令编辑文档（缓冲副本）：结构 → LLM 规划操作 → 应用 → 返回预览."""
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise BadRequestException("缺少编辑指令 instruction")
    info = office_docs.read_structure(payload["sub"], doc_id)
    ops = await office_docs.plan_edits(
        instruction,
        info["structure"],
        info["kind"],
        payload["sub"],
        api_key=None,
    )
    records = office_docs.apply_edits(payload["sub"], doc_id, ops)
    after = office_docs.read_structure(payload["sub"], doc_id)
    return {
        "code": 0,
        "data": {
            "doc_id": doc_id,
            "records": records,
            "ops": ops,
            "structure_after": after["structure"][:30000],
            "has_buffer": True,
        },
    }


@router.get("/{doc_id}/file")
async def download_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """获取编辑后的缓冲文件（用户审核通过后落盘）."""
    meta = _session_or_404(payload["sub"], doc_id)
    from pathlib import Path

    buffered = Path(meta["_session"]) / f"buffered{meta['ext']}"
    path = buffered if buffered.exists() else Path(meta["_session"]) / f"original{meta['ext']}"
    return FileResponse(
        str(path),
        media_type="application/octet-stream",
        filename=meta["filename"],
    )


@router.post("/{doc_id}/analyze")
async def analyze_office_doc(
    doc_id: str,
    req: AnalyzeRequest,
    payload: dict = Depends(require_auth),
):
    """分析/问答/总结文档：先把文档转成会话级 RAG 索引，再检索 + LLM 作答."""
    instruction = (req.instruction or "").strip()
    if not instruction:
        raise BadRequestException("缺少分析指令 instruction")
    try:
        result = await office_docs.analyze_doc(
            payload["sub"],
            doc_id,
            instruction,
            mode=req.mode,
        )
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    return {"code": 0, "data": result}


@router.delete("/{doc_id}")
async def discard_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """丢弃办公文档会话（缓冲 + 原件）."""
    await office_docs.discard_session(payload["sub"], doc_id)
    return {"code": 0, "message": "已丢弃"}
