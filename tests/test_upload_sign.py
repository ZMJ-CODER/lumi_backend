"""附件签名鉴权测试（无需数据库；get_upload_file 只依赖文件与签名）."""

import asyncio
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app.api.v1.uploads import _sign_upload, get_upload_file
from app.core.config import settings
from app.core.exceptions import BadRequestException


def _make_file(tmp_path, uid, name="a.png", data=b"pngdata"):
    d = tmp_path / "chat" / uid
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return f"/uploads/{uid}/{name}"


def test_valid_signature(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    path = _make_file(tmp_path, "user-1")
    exp = int(time.time()) + 3600
    token = _sign_upload(f"{path}:{exp}")
    resp = asyncio.run(get_upload_file("user-1", "a.png", token=token, exp=exp))
    assert resp.status_code == 200
    assert "image" in (resp.headers.get("content-type") or "")


def test_bad_signature_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    _make_file(tmp_path, "user-1")
    exp = int(time.time()) + 3600
    with pytest.raises(BadRequestException):
        asyncio.run(get_upload_file("user-1", "a.png", token="bad", exp=exp))


def test_expired_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    path = _make_file(tmp_path, "user-1")
    exp = int(time.time()) - 10
    token = _sign_upload(f"{path}:{exp}")
    with pytest.raises(BadRequestException):
        asyncio.run(get_upload_file("user-1", "a.png", token=token, exp=exp))


def test_missing_file_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    path = "/uploads/user-1/missing.png"
    exp = int(time.time()) + 3600
    token = _sign_upload(f"{path}:{exp}")
    from app.core.exceptions import NotFoundException

    with pytest.raises(NotFoundException):
        asyncio.run(get_upload_file("user-1", "missing.png", token=token, exp=exp))
