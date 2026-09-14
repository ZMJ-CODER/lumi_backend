"""鉴权配置诊断守卫：JWT 密钥指纹 + `.env` 重复键 + 令牌校验的行为与告警。

## 背景（一次真实事故）

`.env` 里 `JWT_SECRET_KEY` 被写了两次（旧的 48 位留在上面、新的 64 位追加在末尾）。
dotenv/pydantic 的语义是**最后一条生效**，于是密钥"悄悄轮换"了：客户端手里由旧密钥签发的
access/refresh 全部校验失败——

```
POST /api/v1/auth/refresh   401
GET  /api/v1/user/models    401
GET  /api/v1/conversations  401
```

而 `require_auth` 把异常吞成 401，日志里只有一串 401 状态码，看不出原因。
本文件把"这类事故必须一眼可诊断"钉成可执行约束：

1. `jwt_secret_fingerprint()` 只暴露指纹（8 位 hex），可安全进日志；
2. `decode_token()` 遇到**签名不符**要在告警里带上指纹与后果说明；**过期**属于正常现象；
3. `duplicate_env_keys()` 能查出 `.env` 里被定义多次的键（只返回键名，不返回值）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt

from app.core.config import duplicate_env_keys, settings
from app.platform.security.security import (
    create_access_token,
    decode_token,
    jwt_secret_fingerprint,
)


def test_secret_fingerprint_is_short_stable_and_not_the_secret():
    fingerprint = jwt_secret_fingerprint()
    assert len(fingerprint) == 8
    assert all(ch in "0123456789abcdef" for ch in fingerprint)
    assert fingerprint == jwt_secret_fingerprint(), "同一密钥多次调用必须稳定"
    assert fingerprint not in settings.JWT_SECRET_KEY, "指纹不得泄露密钥本身"
    assert settings.JWT_SECRET_KEY not in fingerprint


def test_access_token_round_trips_with_the_current_secret():
    token = create_access_token("u1", "alice", "user")
    payload = decode_token(token)
    assert payload["sub"] == "u1"
    assert payload["type"] == "access"


def test_token_signed_with_another_secret_is_rejected():
    """**事故复现**：旧密钥签发的令牌在当前密钥下必然失败（客户端必须重新登录）。"""
    forged = jwt.encode(
        {"sub": "u1", "type": "access", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        "some-other-secret",
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(Exception) as excinfo:
        decode_token(forged)
    assert type(excinfo.value).__name__ in {"JWTError", "JWSError", "JWTClaimsError"}


def test_expired_token_is_rejected_without_being_treated_as_an_attack():
    """过期是正常现象：仍然拒绝，但语义上不属于"密钥变了"。"""
    expired = jwt.encode(
        {"sub": "u1", "type": "access", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )
    with pytest.raises(Exception) as excinfo:
        decode_token(expired)
    assert type(excinfo.value).__name__ == "ExpiredSignatureError"


def test_duplicate_env_keys_reports_only_repeated_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# 注释不算",
                "JWT_SECRET_KEY=old-value",
                "CORS_ORIGINS=[\"*\"]",
                "",
                "JWT_SECRET_KEY=new-value",
                "DEBUG=false",
            ]
        ),
        encoding="utf-8",
    )
    assert duplicate_env_keys(env) == ["JWT_SECRET_KEY"]


def test_duplicate_env_keys_is_empty_for_a_missing_file(tmp_path):
    assert duplicate_env_keys(tmp_path / "nope.env") == []


def test_repo_env_has_no_duplicate_keys():
    """仓库里的 `.env`（开发者本地文件）不允许有重复键。

    这正是那次全站 401 的根因：重复定义让"改了密钥看起来没生效"变成"没改却悄悄轮换"。
    文件不存在时（CI/容器）本条自动通过。
    """
    assert duplicate_env_keys() == [], (
        ".env 存在重复键：只有最后一条生效。请合并为一条后重启，"
        "并注意轮换 JWT_SECRET_KEY 会让所有已发出的令牌失效（客户端需重新登录）"
    )
