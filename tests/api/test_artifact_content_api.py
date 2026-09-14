"""统一归档内容读取接口回归（前端唯一入口：``GET /api/v1/artifacts/{ref}/content``）。

前后端确认的契约（阶段 1 冻结项）：

* 文本类 → ``{artifact_id, filename, mime_type, size_bytes, content, truncated, encoding}``；
* 二进制/超大 → 字节流（不带 JSON 包装）；
* 过期 → 401 + ``data.error_code=RESULT_REF_EXPIRED``；不存在/他人 → 404；任务/工作区不匹配 → 403；
* **不暴露对象存储真实地址或服务端绝对路径**；
* 只有这一个归档读取路径，不再新增 ``/archive/{id}`` / ``/job-log/{id}`` 专用接口。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.api.v1 import artifacts as artifacts_api
from app.core.exceptions import ForbiddenException, NotFoundException, UnauthorizedException
from app.services import artifacts


def test_content_route_is_registered_once_and_replaces_archive_specific_paths():
    paths = [getattr(route, "path", "") for route in artifacts_api.router.routes]
    assert "/{artifact_id}/content" in paths
    # 不再有 archive/job-log 专用接口（避免第二套权限与生命周期体系）
    assert not any("archive" in path or "job-log" in path for path in paths)


@pytest.fixture()
def artifact_factory(monkeypatch, tmp_path):
    """在临时目录里造一个真实产物（复用既有 artifact_id 签名 + 归属校验）。"""

    def _make(name: str, body: bytes, user_id: str = "u1") -> dict:
        root = tmp_path / user_id
        root.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(body)
        monkeypatch.setattr(
            artifacts, "artifact_path",
            lambda uid, aid: (root / name) if uid == user_id else None,
        )
        artifact_id = artifacts.make_artifact_id("conv-1", name)
        monkeypatch.setattr(
            artifacts, "artifact_record",
            lambda uid, aid: (
                {"artifact_id": aid, "filename": name, "mime_type": artifacts.media_type_for(name),
                 "size_bytes": len(body)}
                if uid == user_id else None
            ),
        )
        return {"artifact_id": artifact_id, "user_id": user_id, "path": root / name}

    return _make


def _content(artifact_id: str, *, user: str = "u1", job_id: str = "", workspace_id: str = ""):
    return asyncio.run(
        artifacts_api.get_artifact_content(
            artifact_id, job_id=job_id, workspace_id=workspace_id, max_bytes=65_536,
            payload={"sub": user},
        )
    )


def test_text_artifact_returns_content_in_json(artifact_factory):
    ref = artifact_factory("report.md", "# 报告\n正文".encode("utf-8"))
    body = _content(ref["artifact_id"])
    assert body["code"] == 0
    data = body["data"]
    assert data["content"] == "# 报告\n正文"
    assert data["mime_type"].startswith("text/") or data["mime_type"] == ""
    assert data["size_bytes"] > 0 and data["truncated"] is False
    assert data["encoding"] == "utf-8"
    # 不暴露服务端路径/对象存储地址
    blob = str(body)
    assert str(ref["path"]) not in blob and "http" not in blob


def test_binary_artifact_returns_byte_stream(artifact_factory):
    ref = artifact_factory("blob.bin", b"\x00\x01\x02binary")
    response = _content(ref["artifact_id"])
    assert getattr(response, "media_type", "") == "application/octet-stream"
    assert str(ref["path"]) == response.path, "字节流指回产物文件（由受权限保护的路径解析得到）"


def test_expired_artifact_reports_result_ref_expired(artifact_factory):
    ref = artifact_factory("old.md", b"x")
    parsed = artifacts.parse_artifact_id(ref["artifact_id"]) or {}
    old_issued = float(parsed.get("issued_at") or 0) - artifacts.ARTIFACT_TTL_SECONDS - 10
    expired_id = artifacts.make_artifact_id("conv-1", "old.md", issued_at=old_issued)
    with pytest.raises(UnauthorizedException) as exc:
        _content(expired_id)
    assert exc.value.error_code == "RESULT_REF_EXPIRED"
    assert exc.value.data == {"error_code": "RESULT_REF_EXPIRED"}


def test_unknown_or_foreign_artifact_is_404(artifact_factory):
    ref = artifact_factory("report.md", b"data")
    with pytest.raises(NotFoundException):
        _content("not-a-valid-artifact-id")
    with pytest.raises(NotFoundException):
        _content(ref["artifact_id"], user="someone-else")


def test_job_and_workspace_permission_are_enforced(monkeypatch, artifact_factory):
    ref = artifact_factory("report.md", b"data")

    class _Job:
        user_id = "u2"
        routing = {"workspace_id": "ws-other"}

    async def _get_job(job_id: str):
        return _Job() if job_id == "job-1" else None

    from app.agents.orchestration.orchestrator import orchestrator

    monkeypatch.setattr(orchestrator, "get_job", _get_job)
    with pytest.raises(ForbiddenException) as exc:
        _content(ref["artifact_id"], job_id="job-1")
    assert exc.value.error_code == "PERMISSION_DENIED"


def test_workspace_mismatch_is_denied(monkeypatch, artifact_factory):
    ref = artifact_factory("report.md", b"data")

    class _Job:
        user_id = "u1"
        routing = {"workspace_id": "ws-1"}

    async def _get_job(job_id: str):
        return _Job()

    from app.agents.orchestration.orchestrator import orchestrator

    monkeypatch.setattr(orchestrator, "get_job", _get_job)
    with pytest.raises(ForbiddenException):
        _content(ref["artifact_id"], job_id="job-1", workspace_id="ws-2")
    # 一致时放行
    body = _content(ref["artifact_id"], job_id="job-1", workspace_id="ws-1")
    assert body["code"] == 0


def test_text_detection_prefers_mime_then_suffix():
    assert artifacts_api._is_text_like("a.unknown", "text/plain") is True
    assert artifacts_api._is_text_like("a.json", "application/json") is True
    assert artifacts_api._is_text_like("a.md", "") is True
    assert artifacts_api._is_text_like("a.png", "image/png") is False


def test_expiry_window_matches_artifact_ttl():
    assert artifacts.ARTIFACT_TTL_SECONDS == artifacts.ARTIFACT_TTL_DAYS * 24 * 3600
    now = datetime.now(timezone.utc)
    assert artifacts.expires_at_for(now.timestamp()) > now.isoformat()
    assert (datetime.fromisoformat(artifacts.expires_at_for(now.timestamp()))
            - now) <= timedelta(days=artifacts.ARTIFACT_TTL_DAYS, seconds=1)
