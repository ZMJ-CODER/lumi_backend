"""聊天附件上传接口.

当前开放图片上传；语音/视频等后续扩展（type 字段已预留）。
文件存到 uploads/chat/{user_id}/，通过签名 URL 访问（不再静态裸挂）：
  1. 前端用 GET /uploads/sign?path=... 换取限时签名 URL
  2. GET /uploads/{user_id}/{filename}?token=...&exp=... 校验签名后返回文件
"""

import hashlib
import hmac
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.core.config import settings
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException, NotFoundException
from app.core.throttling import consume_route_limit

router = APIRouter()

MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB

# 允许的附件扩展名（图片 + 语音/视频，为后续语音留位）
ALLOWED_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".wav", ".ogg", ".m4a", ".aac",
    ".mp4", ".webm", ".mov",
}


def _sign_upload(data: str) -> str:
    """HMAC-SHA256 签名（密钥用 JWT_SECRET_KEY）."""
    return hmac.new(
        settings.JWT_SECRET_KEY.encode("utf-8"), data.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@router.get("/sign")
async def sign_upload_url(
    path: str = Query(..., description="附件路径，形如 /uploads/{user_id}/{filename}"),
    payload: dict = Depends(require_auth),
):
    """为聊天附件生成限时签名 URL（防止 /uploads 裸奔，他人拿到 URL 也访问不了）."""
    parts = [p for p in (path or "").split("/") if p]
    if len(parts) < 3 or parts[0] != "uploads":
        raise BadRequestException("路径无效")
    if parts[1] != payload.get("sub"):
        raise BadRequestException("无权签名该文件")
    exp = int(time.time()) + settings.UPLOAD_TOKEN_TTL_SECONDS
    token = _sign_upload(f"{path}:{exp}")
    return {"code": 0, "data": {"url": f"{path}?token={token}&exp={exp}"}}


@router.get("/{user_id}/{filename}")
async def get_upload_file(
    user_id: str,
    filename: str,
    token: str = Query(...),
    exp: int = Query(...),
):
    """带签名校验的附件访问：签名有效期内返回文件，否则拒绝."""
    if exp < time.time():
        raise BadRequestException("链接已过期，请重新生成")
    path = f"/uploads/{user_id}/{filename}"
    if not hmac.compare_digest(_sign_upload(f"{path}:{exp}"), token):
        raise BadRequestException("签名无效")
    base = (Path(settings.UPLOAD_DIR) / "chat" / str(user_id)).resolve()
    target = (base / filename).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise NotFoundException("文件不存在")
    return FileResponse(target)


@router.post("")
async def upload_chat_attachment(
    request: Request,
    file: UploadFile = File(...),
    payload: dict = Depends(require_auth),
):
    """上传聊天附件，返回可访问的 URL."""
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
    user_id = payload["sub"]
    filename = file.filename or "attachment"
    ext = Path(filename).suffix.lower() or ".bin"
    if ext not in ALLOWED_EXTS:
        raise BadRequestException(f"不支持的文件类型：{ext or filename}")

    content = await file.read()
    if not content:
        raise BadRequestException("文件内容为空")
    if len(content) > MAX_ATTACHMENT_SIZE:
        raise BadRequestException("文件超过 20MB 限制")

    file_id = uuid.uuid4()
    # 文件存 data/uploads/chat/{user_id}/，/uploads 静态挂载指向 chat 目录，
    # 因此 URL 为 /uploads/{user_id}/{file_id}{ext}
    dest = Path(settings.UPLOAD_DIR) / "chat" / user_id / f"{file_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)

    url = f"/uploads/{user_id}/{file_id}{ext}"
    return {
        "code": 0,
        "data": {
            "url": url,
            "name": filename,
            "size": len(content),
            "mime_type": file.content_type or "",
            "type": "audio" if ext in {".mp3", ".wav", ".ogg", ".m4a", ".aac"}
            else "video" if ext in {".mp4", ".webm", ".mov"}
            else "image",
        },
    }
