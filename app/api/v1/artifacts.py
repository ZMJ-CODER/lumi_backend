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
* 💡 元数据接口 ``GET /api/v1/artifacts/{artifact_id}`` 用于卡片上的文件名/大小/过期提示；
  它同时下发**保留策略**字段（``retention_class`` / ``requested_expires_at`` /
  ``effective_expires_at`` / ``retention_policy_source`` / ``retention_clamp_reason``）
  与 ``days_until_expiry`` / ``retention_notice``，前端据此提示
  "该产物受当前存储策略限制，将于 N 天后过期"（被夹取时才带"受策略限制"字样）。

产物字节仍由既有通用产物目录持有（`/office/docs/outputs/...` 的同一份文件），
这里只提供**以 artifact_id 为键**的稳定入口，避免前端拼接 ``conv_id + name``。
响应里永不出现服务端绝对路径、签名密钥或下载令牌以外的内部信息。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.core.deps import require_auth
from app.core.exceptions import ForbiddenException, NotFoundException, UnauthorizedException
from app.services import artifacts
from lumi_contracts import ARTIFACT_RETENTION_FIELDS

router = APIRouter()

#: 过期属于**正常路径**（前端会重取一次 download-url），因此用机器可读的错误标识
#: 而不是 500；同时通过 ``data.error_code`` 出现在统一 ``{code, message, data}`` 响应体里。
RESULT_REF_EXPIRED = "RESULT_REF_EXPIRED"

#: 归档内容接口一次性返回文本的字节上限（超过改走字节流，不塞 JSON）。
CONTENT_TEXT_MAX_BYTES = 65_536

#: 按后缀识别的文本类产物（mime 缺失时的兜底）。
TEXT_FILE_SUFFIXES: frozenset[str] = frozenset({
    ".txt", ".md", ".markdown", ".json", ".jsonl", ".csv", ".tsv", ".log",
    ".py", ".js", ".ts", ".yaml", ".yml", ".xml", ".html", ".css", ".sql", ".ini", ".toml",
})

#: 保留策略字段（元数据 / 内容 / 下载响应与响应头共用同一份提取逻辑）：
#: 契约要求的五个字段 + 前端展示用的"夹取标记 / 剩余天数 / 到期提示"。
RETENTION_FIELDS: tuple[str, ...] = (
    *ARTIFACT_RETENTION_FIELDS,
    "retention_clamped",
    "days_until_expiry",
    "retention_notice",
)


def _retention_of(record: dict) -> dict:
    """从产物记录里取保留策略字段（字段恒定存在，缺失回落空值）。"""
    return {name: record.get(name, "") for name in RETENTION_FIELDS}


def _retention_headers(record: dict) -> dict[str, str]:
    """字节流响应的保留策略响应头（前端不必额外再调一次元数据接口）。"""
    return {
        "X-Artifact-Retention-Class": str(record.get("retention_class") or ""),
        "X-Artifact-Expires-At": str(record.get("effective_expires_at") or ""),
        "X-Artifact-Retention-Clamped": "true" if record.get("retention_clamped") else "false",
    }


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
    record = artifacts.artifact_record(user_id, artifact_id)
    if record is None:
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
            # 令牌到期 ≠ 产物到期：这里额外给出**产物**的保留策略，避免前端把两者混淆。
            "artifact_expires_at": record.get("effective_expires_at", ""),
            **_retention_of(record),
        },
    }


@router.get("/{artifact_id}/content")
async def get_artifact_content(
    artifact_id: str,
    job_id: str = Query(default="", description="可选：校验该产物属于此任务（任务权限）"),
    workspace_id: str = Query(default="", description="可选：校验产物所属工作区"),
    max_bytes: int = Query(default=CONTENT_TEXT_MAX_BYTES, ge=1, le=CONTENT_TEXT_MAX_BYTES),
    payload: dict = Depends(require_auth),
):
    """**统一归档内容读取接口**（前端唯一入口，替代任何 archive/job-log 专用接口）。

    契约（前后端已确认）：

    * 文本类（``text/*`` / json / xml / csv / md / log …）且未超限 →
      ``{"code":0,"data":{"artifact_id","filename","mime_type","size_bytes",
      "content","truncated","encoding":"utf-8"}}``，并附保留策略字段
      （``retention_class`` / ``requested_expires_at`` / ``effective_expires_at`` /
      ``retention_policy_source`` / ``retention_clamp_reason`` / ``retention_notice``）；
    * 二进制或超大 → 直接返回字节流（``FileResponse``），保留策略走
      ``X-Artifact-Retention-Class`` / ``X-Artifact-Expires-At`` 响应头；
    * 过期 → 401 且 ``data.error_code=RESULT_REF_EXPIRED``（正常路径，前端提示"已过期"）；
    * 不存在/不属于当前用户 → 404；任务/工作区不匹配 → 403；
    * **永不返回对象存储真实地址或服务端绝对路径**（只有 ``artifact_id`` 这个稳定引用）；
    * 响应错误体沿用统一 ``{code, message, data:{error_code}}`` 形状。
    """
    user_id = str(payload.get("sub") or "")
    parsed = artifacts.parse_artifact_id(artifact_id)
    if parsed is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")
    # 有效期按**保留类别**判定（归档 / 用户产物各有策略），不再一律 7 天。
    if artifacts.is_expired(parsed):
        raise UnauthorizedException(
            "产物已过期，请重新生成",
            error_code=RESULT_REF_EXPIRED,
            data={"error_code": RESULT_REF_EXPIRED},
        )

    await _assert_job_and_workspace_access(user_id, job_id=job_id, workspace_id=workspace_id)

    record = artifacts.artifact_record(user_id, artifact_id)
    path = artifacts.artifact_path(user_id, artifact_id)
    if record is None or path is None:
        raise NotFoundException("产物不存在、不属于当前用户或已过期")

    mime_type = str(record.get("mime_type") or artifacts.media_type_for(path.name))
    size_bytes = int(record.get("size_bytes") or 0)
    filename = str(record.get("filename") or path.name)
    if _is_text_like(filename, mime_type) and 0 <= size_bytes <= max_bytes:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # noqa: BLE001 - 读取失败按不可用处理，不泄露路径
            raise NotFoundException("产物不存在、不属于当前用户或已过期") from exc
        return {
            "code": 0,
            "data": {
                "artifact_id": artifact_id,
                "filename": filename,
                "mime_type": mime_type,
                "size_bytes": size_bytes,
                "content": content,
                "truncated": False,
                "encoding": "utf-8",
                **_retention_of(record),
            },
        }
    # 二进制/超大：走字节流（前端按 mime_type 处理），不把二进制塞进 JSON
    return FileResponse(
        str(path),
        media_type=mime_type,
        filename=filename,
        headers=_retention_headers(record),
    )


async def _assert_job_and_workspace_access(user_id: str, *, job_id: str, workspace_id: str) -> None:
    """任务/工作区权限校验（给了才校验；缺省沿用"用户归属"这一层）。"""
    target_job = str(job_id or "").strip()
    if target_job:
        from app.agents.orchestration.orchestrator import orchestrator

        job = await orchestrator.get_job(target_job)
        if job is None or str(job.user_id) != user_id:
            raise ForbiddenException(
                "无权访问该任务的产物",
                error_code="PERMISSION_DENIED",
                data={"error_code": "PERMISSION_DENIED"},
            )
        target_workspace = str(workspace_id or "").strip()
        if target_workspace:
            bound = str((job.routing or {}).get("workspace_id") or "")
            if bound and bound != target_workspace:
                raise ForbiddenException(
                    "无权访问该工作区的产物",
                    error_code="PERMISSION_DENIED",
                    data={"error_code": "PERMISSION_DENIED"},
                )


def _is_text_like(filename: str, mime_type: str) -> bool:
    """文本类判定（决定返回 JSON 文本还是字节流）。"""
    mime = str(mime_type or "").strip().casefold()
    if mime.startswith("text/"):
        return True
    if mime in {"application/json", "application/xml", "application/x-ndjson", "application/yaml"}:
        return True
    suffix = Path(str(filename or "")).suffix.casefold()
    return suffix in TEXT_FILE_SUFFIXES


__all__ = ["router"]


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
    record = artifacts.artifact_record(user_id, artifact_id) or {}
    return FileResponse(
        str(path),
        media_type=artifacts.media_type_for(path.name),
        filename=path.name,
        headers=_retention_headers(record),
    )
