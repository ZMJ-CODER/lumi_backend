"""聊天附件上传接口.

当前开放图片上传；语音/视频等后续扩展（type 字段已预留）。
文件存到 uploads/chat/{user_id}/，通过 /uploads 静态路径访问。
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, UploadFile

from app.core.config import settings
from app.core.deps import require_auth
from app.core.exceptions import BadRequestException

router = APIRouter()

MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB

# 允许的附件扩展名（图片 + 语音/视频，为后续语音留位）
ALLOWED_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".wav", ".ogg", ".m4a", ".aac",
    ".mp4", ".webm", ".mov",
}


@router.post("")
async def upload_chat_attachment(
    file: UploadFile = File(...),
    payload: dict = Depends(require_auth),
):
    """上传聊天附件，返回可访问的 URL."""
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
