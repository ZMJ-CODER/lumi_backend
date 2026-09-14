"""产物短时下载 URL（「方案 2」§5.1）的后端契约回归。

前端契约：

* ``artifact_created`` 只带引用（``artifact_id`` + 元数据），**不带下载地址**；
* 用户点击卡片时调 ``GET /api/v1/artifacts/{artifact_id}/download-url``，服务端
  **重新校验当前用户归属**后签发一个 ~5 分钟的短时 URL；
* 短链下载返回 401/403 时，前端只重取一次 ``download-url``，仍失败才提示
  "无权访问或产物已过期"——所以**过期必须是可判别的正常路径**
  （``data.error_code=RESULT_REF_EXPIRED``），而不是 500 或"无权访问"；
* 令牌绑定 ``user_id``：泄露的 URL 换个人打开是 403，不能重放。

本文件钉死上述形状与状态码，前端不需要再改一行。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.api.v1 import artifacts as artifacts_api
from app.core.config import settings
from app.core.exceptions import AppException, ForbiddenException, NotFoundException, UnauthorizedException
from app.services import artifacts
from app.office import docs as office_docs

OWNER = {"sub": "owner-user", "username": "owner"}
OTHER = {"sub": "another-user", "username": "other"}


@pytest.fixture(autouse=True)
def _fixed_default_ttl(monkeypatch):
    """把 TTL 固定为契约默认值，避免本机 .env 影响断言（单测另行覆盖夹取行为）。"""
    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", 300)


def _make_output(
    monkeypatch,
    tmp_path,
    *,
    user: str = "owner-user",
    job: str = "job-1",
    name: str = "report.csv",
):
    """在隔离的产物目录里造一个真实文件（不污染仓库 data/），返回安全引用与路径。"""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    out_dir = office_docs.generic_outputs_dir(user, job)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    target.write_text("a,b\n1,2\n", encoding="utf-8")
    ref = artifacts.artifact_from_output(job, {"name": name, "size": target.stat().st_size})
    assert ref is not None
    return ref, target


def _extract_token(issued: dict) -> str:
    """从 download-url 的响应里取出令牌（前端拿到的就是这个 URL）。"""
    url = issued["data"]["url"]
    assert url.startswith("/api/v1/artifacts/")
    return url.split("token=", 1)[1]


def _issue(artifact_id: str, payload: dict = OWNER) -> dict:
    return asyncio.run(artifacts_api.create_artifact_download_url(artifact_id, payload))


def _download(artifact_id: str, token: str | None, payload: dict = OWNER):
    return asyncio.run(artifacts_api.download_artifact(artifact_id, token, payload))


def _error_body(exc: AppException) -> tuple[int, dict]:
    """走全局异常处理器，拿到前端真正收到的 ``{code, message, data}`` 响应体。"""
    from starlette.requests import Request

    from app.core.exception_handlers import _app_exception_handler

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/artifacts/art_x/download",
        "query_string": b"",
        "headers": [],
    }
    response = asyncio.run(_app_exception_handler(Request(scope), exc))
    return response.status_code, json.loads(response.body)


def test_download_url_route_is_registered():
    """路径必须与前端契约完全一致（否则前端要改代码）。"""
    paths = {route.path for route in artifacts_api.router.routes}
    assert "/{artifact_id}/download-url" in paths
    assert "/{artifact_id}/download" in paths
    assert "/{artifact_id}" in paths


def test_download_url_response_shape_and_ttl(monkeypatch, tmp_path):
    """响应形状 + 正的 ``expires_in`` + UTC ISO-8601 ``expires_at``。"""
    ref, _ = _make_output(monkeypatch, tmp_path)
    body = _issue(ref["artifact_id"])

    assert body["code"] == 0
    data = body["data"]
    # 前端冻结契约（electron/stream-fixtures.cases.cjs::artifact_signing）：
    # 响应字段必须是 download_url / expires_at / expires_in，前端**只认**
    # download_url（不做别名猜测）；``url`` 保留为同值兼容别名。
    assert {"download_url", "url", "expires_at", "expires_in"} <= set(data)
    assert data["download_url"] == data["url"]
    assert data["expires_in"] == 300 and isinstance(data["expires_in"], int)
    # 相对路径（本仓库没有配置公开基址），令牌就是签发的短时凭据
    assert data["download_url"].startswith(
        f"/api/v1/artifacts/{ref['artifact_id']}/download?token=artdl_"
    )
    assert _extract_token(body)

    expires = datetime.fromisoformat(data["expires_at"])
    assert expires.utcoffset() == timedelta(0), "expires_at 必须是 UTC"
    delta = (expires - datetime.now(timezone.utc)).total_seconds()
    assert 0 < delta <= data["expires_in"] + 1


def test_token_download_works_for_owner(monkeypatch, tmp_path):
    """所有者带令牌可以真正拿到字节。"""
    ref, target = _make_output(monkeypatch, tmp_path)
    token = _extract_token(_issue(ref["artifact_id"]))

    response = _download(ref["artifact_id"], token)
    assert Path(response.path) == target
    assert response.filename == "report.csv"
    assert response.media_type == "text/csv"


def test_token_download_rejected_with_403_for_other_user(monkeypatch, tmp_path):
    """令牌有效但不属于当前用户 → 403（泄露的 URL 不能重放）。"""
    ref, _ = _make_output(monkeypatch, tmp_path)
    token = _extract_token(_issue(ref["artifact_id"]))

    with pytest.raises(ForbiddenException) as excinfo:
        _download(ref["artifact_id"], token, OTHER)
    exc = excinfo.value
    assert exc.status_code == 403
    status, body = _error_body(exc)
    assert status == 403
    assert body == {"code": 403, "message": "无权访问该产物", "data": None}


def test_expired_token_is_401_result_ref_expired_and_reissue_works(monkeypatch, tmp_path):
    """过期是正常路径：401 + ``RESULT_REF_EXPIRED``；重新签发后必须能下载。"""
    ref, target = _make_output(monkeypatch, tmp_path)
    artifact_id = ref["artifact_id"]

    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", 1)
    stale_token = _extract_token(_issue(artifact_id))
    time.sleep(1.05)

    with pytest.raises(UnauthorizedException) as excinfo:
        _download(artifact_id, stale_token)
    exc = excinfo.value
    assert exc.status_code == 401
    assert exc.error_code == "RESULT_REF_EXPIRED"
    status, body = _error_body(exc)
    assert status == 401
    assert body["code"] == 401
    assert body["data"] == {"error_code": "RESULT_REF_EXPIRED"}, "前端据此自动重取一次 download-url"

    # 前端自动重取：新 URL 必须与旧的不同的、且立即可用
    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", 300)
    fresh_token = _extract_token(_issue(artifact_id))
    assert fresh_token != stale_token
    assert Path(_download(artifact_id, fresh_token).path) == target


def test_tampered_blank_and_mismatched_tokens_are_401(monkeypatch, tmp_path):
    """篡改签名 / 空令牌 / 张冠李戴（别的产物的令牌）一律 401。"""
    ref, _ = _make_output(monkeypatch, tmp_path)
    other_ref, _ = _make_output(monkeypatch, tmp_path, job="job-2", name="other.csv")
    artifact_id = ref["artifact_id"]
    good = _extract_token(_issue(artifact_id))
    other_token = _extract_token(_issue(other_ref["artifact_id"]))

    tampered = good[:-2] + ("aa" if not good.endswith("aa") else "bb")
    for bad in (tampered, "", "artdl_bogus.deadbeefdeadbeef", good[len("artdl_"):], other_token):
        with pytest.raises(UnauthorizedException) as excinfo:
            _download(artifact_id, bad)
        assert excinfo.value.status_code == 401
        assert excinfo.value.error_code != "RESULT_REF_EXPIRED", "篡改不能伪装成'已过期'"


def test_download_url_and_error_bodies_leak_no_path_or_secret(monkeypatch, tmp_path):
    """响应里不得出现绝对路径、产物目录名或签名密钥。"""
    monkeypatch.setattr(settings, "JWT_SECRET_KEY", "unit-test-secret-DO-NOT-LEAK")
    ref, target = _make_output(monkeypatch, tmp_path)
    artifact_id = ref["artifact_id"]

    texts = [json.dumps(_issue(artifact_id), ensure_ascii=False)]
    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", 1)
    stale = _extract_token(_issue(artifact_id))
    time.sleep(1.05)
    with pytest.raises(AppException) as excinfo:
        _download(artifact_id, stale)
    texts.append(json.dumps(_error_body(excinfo.value)[1], ensure_ascii=False))

    for text in texts:
        assert "unit-test-secret-DO-NOT-LEAK" not in text
        assert str(tmp_path) not in text
        assert str(target) not in text
        assert "office_outputs" not in text
        assert not re.search(r"[A-Za-z]:[\\/]", text), "不得下发服务端绝对路径"


def test_issuance_404_for_unknown_or_foreign_artifact(monkeypatch, tmp_path):
    """未知 artifact_id 与"别人的产物"都不能换来 URL（签发期即拦下）。"""
    ref, _ = _make_output(monkeypatch, tmp_path)

    with pytest.raises(NotFoundException) as excinfo:
        _issue("art_bm90LWZvdW5k.0000000000000000")
    assert excinfo.value.status_code == 404

    with pytest.raises(NotFoundException):
        _issue(ref["artifact_id"], OTHER)


def test_download_without_token_keeps_legacy_behaviour(monkeypatch, tmp_path):
    """向后兼容：不传 token 时仍按登录态 + 归属校验下载（历史行为不变）。"""
    ref, target = _make_output(monkeypatch, tmp_path)

    assert Path(_download(ref["artifact_id"], None).path) == target
    with pytest.raises(NotFoundException):
        _download(ref["artifact_id"], None, OTHER)


def test_ttl_is_clamped_and_bad_config_falls_back(monkeypatch, tmp_path):
    """配置写错也不能把"短期令牌"变成长期凭据：夹到 [1, 3600]。"""
    ref, _ = _make_output(monkeypatch, tmp_path)
    artifact_id = ref["artifact_id"]

    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", 999999)
    assert _issue(artifact_id)["data"]["expires_in"] == 3600

    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", "not-a-number")
    assert _issue(artifact_id)["data"]["expires_in"] == 300

    monkeypatch.setattr(settings, "ARTIFACT_DOWNLOAD_URL_TTL_SECONDS", 0)
    assert _issue(artifact_id)["data"]["expires_in"] == 1
