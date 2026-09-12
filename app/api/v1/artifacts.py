"""产物 API：``artifact_created`` 事件对应的受权限保护下载接口。

前端约定（联调清单第 1 项）：

* 事件只给 ``artifact_id`` + 元数据（``filename`` / ``mime_type`` / ``size_bytes`` /
  ``expires_at``），**不给下载地址与令牌**；
* 用户点击时前端调 ``GET /api/v1/artifacts/{artifact_id}/download``；
* 需要登录：接口按"当前登录用户 + 其自己的产物目录"授权，并校验签名与有效期；
* 💡 元数据接口 ``GET /api/v1/artifacts/{artifact_id}`` 用于卡片上的文件名/大小/过期提示。

产物字节仍由既有通用产物目录持有（`/office/docs/outputs/...` 的同一份文件），
这里只提供**以 artifact_id 为键**的稳定入口，避免前端拼接 ``conv_id + name``。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.core.deps import require_auth
from app.core.exceptions import NotFoundException
from app.services import artifacts

router = APIRouter()


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, payload: dict = Depends(require_auth)):
    """产物元数据（不含字节、不含服务端路径、不含下载令牌）。"""
    record = artifacts.artifact_record(payload["sub"], artifact_id)
    if record is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")
    record = {**record, "download_path": f"/api/v1/artifacts/{artifact_id}/download"}
    return {"code": 0, "data": record}


@router.get("/{artifact_id}/download")
async def download_artifact(artifact_id: str, payload: dict = Depends(require_auth)):
    """下载产物字节（受权限保护：登录 + 归属 + 有效期 + 路径越权校验）。"""
    path = artifacts.artifact_path(payload["sub"], artifact_id)
    if path is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")
    return FileResponse(
        str(path),
        media_type=artifacts.media_type_for(path.name),
        filename=path.name,
    )
