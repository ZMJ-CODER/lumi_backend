"""产物 API：``artifact_created`` 事件对应的受权限保护下载接口。

前端约定（联调清单第 1 项）：

* 事件只给 ``artifact_id`` + 元数据（``filename`` / ``mime_type`` / ``size_bytes`` /
  ``expires_at``），**不给下载地址与令牌**；
* 用户点击卡片时前端先调 ``GET /api/v1/artifacts/{artifact_id}/download-url``，
  由服务端**重新校验当前用户归属**后签发短时（默认 5 分钟）URL；
* 下载接口 ``GET /api/v1/artifacts/{artifact_id}/download`` 同时接受
  ①短时 ``token`` 查询参数（校验签名/有效期/归属/产物一致）与 ②原有的登录态直连
  （不传 ``token`` 时行为与历史完全一致，向后兼容）；
* 短链过期返回 401 且 ``data.error_code=RESULT_REF_EXPIRED``（正常路径，前端据此自动
  重取一次 ``download-url``，仍失败才提示"无权访问或产物已过期"）；
* 令牌有效但不属于当前用户返回 403（泄露的 URL 换个人用不了）；
* 💡 元数据接口 ``GET /api/v1/artifacts/{artifact_id}`` 用于卡片上的文件名/大小/过期提示。

产物字节仍由既有通用产物目录持有（`/office/docs/outputs/...` 的同一份文件），
这里只提供**以 artifact_id 为键**的稳定入口，避免前端拼接 ``conv_id + name``。
响应里永不出现服务端绝对路径、签名密钥或下载令牌以外的内部信息。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.core.deps import require_auth
from app.core.exceptions import ForbiddenException, NotFoundException, UnauthorizedException
from app.services import artifacts

router = APIRouter()

#: 过期属于**正常路径**（前端会重取一次 download-url），因此用机器可读的错误标识
#: 而不是 500；同时通过 ``data.error_code`` 出现在统一 ``{code, message, data}`` 响应体里。
RESULT_REF_EXPIRED = "RESULT_REF_EXPIRED"


@router.get("/{artifact_id}")
async def get_artifact(artifact_id: str, payload: dict = Depends(require_auth)):
    """产物元数据（不含字节、不含服务端路径、不含下载令牌）。"""
    record = artifacts.artifact_record(payload["sub"], artifact_id)
    if record is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")
    record = {**record, "download_path": f"/api/v1/artifacts/{artifact_id}/download"}
    return {"code": 0, "data": record}


@router.get("/{artifact_id}/download-url")
async def create_artifact_download_url(artifact_id: str, payload: dict = Depends(require_auth)):
    """签发短时下载 URL（登录 + 归属 + 存在性 + 有效期 **同样**校验一遍）。

    只有仍属于当前用户、且服务端文件确实存在的产物才会签发；签发的令牌绑定
    ``user_id`` 与 ``artifact_id``，因此 URL 泄露后换人打不开、几分钟后自然失效。

    未配置公开基址（本仓库没有 ``PUBLIC_BASE_URL`` 一类的设置）时返回**相对路径**，
    由前端按当前 API 基址拼接。

    响应字段与前端契约（``electron/stream-fixtures.cases.cjs::artifact_signing``）
    一致：``download_url`` / ``expires_at`` / ``expires_in``——前端**只认**
    ``download_url``，不再猜测别名；``url`` 作为同值别名保留，避免旧调用方回归。
    """
    user_id = str(payload.get("sub") or "")
    # 复用下载接口同一份归属校验（artifact_record 内部即 artifact_path：
    # 只在该用户自己的产物目录里解析，并校验签名、有效期与路径越权）。
    if artifacts.artifact_record(user_id, artifact_id) is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")
    issued = artifacts.make_download_token(artifact_id, user_id)
    url = f"/api/v1/artifacts/{artifact_id}/download?token={issued['token']}"
    return {
        "code": 0,
        "data": {
            "download_url": url,
            "url": url,
            "expires_at": issued["expires_at"],
            "expires_in": issued["expires_in"],
        },
    }


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    token: str | None = Query(default=None, description="download-url 签发的短时令牌（缺省时按登录态直连）"),
    payload: dict = Depends(require_auth),
):
    """下载产物字节（受权限保护：登录 + 归属 + 有效期 + 路径越权校验）。

    传 ``token`` 时额外校验短时令牌；不传时保持历史行为（仅凭登录态的归属校验）。
    """
    user_id = str(payload.get("sub") or "")
    if token is not None:
        reason = artifacts.verify_download_token(token, artifact_id=artifact_id, user_id=user_id)
        if reason == artifacts.DOWNLOAD_TOKEN_EXPIRED:
            raise UnauthorizedException(
                "下载链接已过期，请重新获取",
                error_code=RESULT_REF_EXPIRED,
                data={"error_code": RESULT_REF_EXPIRED},
            )
        if reason == artifacts.DOWNLOAD_TOKEN_FOREIGN:
            raise ForbiddenException("无权访问该产物")
        if reason:
            # 缺省/篡改/产物不匹配：不区分具体原因，避免给出探测信号。
            raise UnauthorizedException("下载链接无效，请重新获取")
    path = artifacts.artifact_path(user_id, artifact_id)
    if path is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")
    return FileResponse(
        str(path),
        media_type=artifacts.media_type_for(path.name),
        filename=path.name,
    )
