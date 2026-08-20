"""办公文档编辑 API：上传 → 读取结构 → LLM 规划编辑 → 缓冲 → 取回落盘/丢弃."""

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.throttling import consume_route_limit
from app.core.executors import run_in_compute
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


async def _ensure_session_or_404(user_id: str, doc_id: str) -> dict:
    """确保会话可用（磁盘缺失时从 DB 重建），失败转 404."""
    try:
        return await office_docs.ensure_session(user_id, doc_id)
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc


@router.post("")
async def upload_office_doc(
    request: Request,
    file: UploadFile = File(...),
    payload: dict = Depends(require_auth),
):
    """上传办公文档，建立编辑会话，返回文档结构与会话 id."""
    rate = await consume_route_limit(request, payload, "upload")
    if not rate.allowed:
        return JSONResponse(
            status_code=429,
            content={
                "code": 429,
                "message": "上传请求过于频繁，请稍后重试",
                "data": {"error_code": "UPLOAD_RATE_LIMIT", "retry_after": rate.retry_after},
            },
            headers={"Retry-After": str(rate.retry_after)},
        )
    content = await file.read()
    filename = file.filename or "document.txt"
    try:
        meta = office_docs.create_session(payload["sub"], filename, content)
        # OCR / Docling 解析是 CPU 密集同步操作，放线程池避免阻塞事件循环
        info = await run_in_compute(office_docs.read_structure, payload["sub"], meta["doc_id"])
    except ValueError as exc:
        raise BadRequestException(str(exc)) from exc
    # 聊天框链路：临时会话写入 DB（跨实例可恢复）+ 顺带清理过期会话
    await office_docs.persist_session_record(
        payload["sub"], meta, content, content_text=info.get("structure")
    )
    try:
        await run_in_compute(office_docs.cleanup_expired_sessions)
    except Exception:  # noqa: BLE001
        pass
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


# 通用脚本产物（新建文件，无源文档）：按任务/会话 id 隔离
@router.get("/outputs")
async def list_generic_outputs(conv_id: str, payload: dict = Depends(require_auth)):
    """列出某个任务/会话的通用产物（脚本新建的文件）."""
    items = office_docs.list_generic_outputs(payload["sub"], conv_id)
    return {"code": 0, "data": {"items": items}}


@router.get("/outputs/{name}")
async def get_generic_output(
    conv_id: str,
    name: str,
    payload: dict = Depends(require_auth),
):
    """下载通用产物文件字节."""
    path = office_docs.resolve_generic_output(payload["sub"], conv_id, name)
    if path is None:
        raise NotFoundException("产物不存在")
    return FileResponse(str(path), filename=path.name)


@router.get("/outputs/{name}/preview")
async def preview_generic_output(
    conv_id: str,
    name: str,
    payload: dict = Depends(require_auth),
):
    """预览当前用户任务生成的安全文本/表格内容。"""
    path = office_docs.resolve_generic_output(payload["sub"], conv_id, name)
    if path is None:
        raise NotFoundException("产物不存在")
    return {"code": 0, "data": office_docs.preview_generated_output(path)}


@router.delete("/outputs/{name}")
async def delete_generic_output(
    conv_id: str,
    name: str,
    payload: dict = Depends(require_auth),
):
    """删除通用产物（客户端已投递到本地下载目录后清理后端副本）."""
    removed = office_docs.delete_generic_output(payload["sub"], conv_id, name)
    if not removed:
        raise NotFoundException("产物不存在或已删除")
    return {"code": 0, "data": {"deleted": True}}


@router.get("/{doc_id}")
async def get_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """查看文档结构与缓冲状态."""
    await _ensure_session_or_404(payload["sub"], doc_id)
    info = await run_in_compute(office_docs.read_structure, payload["sub"], doc_id)
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
    await _ensure_session_or_404(payload["sub"], doc_id)
    info = await run_in_compute(office_docs.read_structure, payload["sub"], doc_id)
    ops = await office_docs.plan_edits(
        instruction,
        info["structure"],
        info["kind"],
        payload["sub"],
        api_key=None,
    )
    records = await run_in_compute(office_docs.apply_edits, payload["sub"], doc_id, ops)
    # 只写入缓冲副本：用户在前端预览确认（保留/撤销）后才落盘
    after = await run_in_compute(office_docs.read_structure, payload["sub"], doc_id)
    return {
        "code": 0,
        "data": {
            "doc_id": doc_id,
            "records": records,
            "committed": False,
            "pending_commit": True,
            "message": "修改已生成预览，请在前端确认后写入实际文档文件",
            "ops": ops,
            "structure_after": after["structure"][:30000],
            "has_buffer": True,
        },
    }


@router.post("/{doc_id}/commit")
async def commit_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """确认修改：把缓冲写入实际文档文件（用户预览后点"保留"）."""
    try:
        await _ensure_session_or_404(payload["sub"], doc_id)
        await office_docs.commit_session(payload["sub"], doc_id)
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    return {"code": 0, "message": "已写入实际文档文件"}


@router.post("/{doc_id}/revert")
async def revert_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """撤销修改：丢弃缓冲，文档保持原样（用户预览后点"撤销"）."""
    try:
        await _ensure_session_or_404(payload["sub"], doc_id)
        office_docs.revert_edits(payload["sub"], doc_id)
    except LookupError as exc:
        raise NotFoundException(str(exc)) from exc
    return {"code": 0, "message": "已撤销修改，文档保持原样"}


@router.get("/{doc_id}/file")
async def download_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """获取实际文档文件（未确认的修改不进入下载，用户需先"保留"）."""
    await _ensure_session_or_404(payload["sub"], doc_id)
    meta = _session_or_404(payload["sub"], doc_id)
    from pathlib import Path

    path = Path(meta["_session"]) / f"original{meta['ext']}"
    return FileResponse(
        str(path),
        media_type="application/octet-stream",
        filename=meta["filename"],
    )


@router.get("/{doc_id}/preview")
async def preview_office_doc(doc_id: str, payload: dict = Depends(require_auth)):
    """获取修改后的预览文件（缓冲优先；未编辑时返回原文件）—— 前端 WYSIWYG 渲染用."""
    await _ensure_session_or_404(payload["sub"], doc_id)
    meta = _session_or_404(payload["sub"], doc_id)
    from pathlib import Path

    buffered = Path(meta["_session"]) / f"buffered{meta['ext']}"
    path = buffered if buffered.exists() else Path(meta["_session"]) / f"original{meta['ext']}"
    return FileResponse(
        str(path),
        media_type="application/octet-stream",
        filename=meta["filename"],
    )


@router.get("/{doc_id}/outputs")
async def list_office_outputs(doc_id: str, payload: dict = Depends(require_auth)):
    """列出脚本产物（格式转换/批量处理生成的文件，如 csv）. """
    await _ensure_session_or_404(payload["sub"], doc_id)
    items = office_docs.list_doc_outputs(payload["sub"], doc_id)
    return {"code": 0, "data": {"items": items}}


@router.get("/{doc_id}/outputs/{name}")
async def download_office_output(
    doc_id: str,
    name: str,
    payload: dict = Depends(require_auth),
):
    """下载脚本产物文件."""
    await _ensure_session_or_404(payload["sub"], doc_id)
    from pathlib import Path

    out_dir = office_docs.doc_output_dir(payload["sub"], doc_id)
    # 防目录穿越
    safe = Path(name).name
    target = out_dir / safe
    if not target.is_file():
        raise NotFoundException("产物文件不存在")
    return FileResponse(
        str(target),
        media_type="application/octet-stream",
        filename=safe,
    )


@router.get("/{doc_id}/outputs/{name}/preview")
async def preview_office_output(
    doc_id: str,
    name: str,
    payload: dict = Depends(require_auth),
):
    """预览当前用户办公文档任务生成的产物，不暴露服务端路径。"""
    await _ensure_session_or_404(payload["sub"], doc_id)
    from pathlib import Path

    out_dir = office_docs.doc_output_dir(payload["sub"], doc_id)
    target = out_dir / Path(name).name
    if not target.is_file():
        raise NotFoundException("产物文件不存在")
    return {"code": 0, "data": office_docs.preview_generated_output(target)}


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
