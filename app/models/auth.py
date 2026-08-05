"""认证模块数据模型 —— 服务端单向哈希方案 v1.0.

密码通过 HTTPS 传输，服务端用 argon2id 加盐哈希存储.
"""

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """注册请求."""
    account: str = Field(..., description="账号（邮箱/手机号）")
    password: str = Field(..., min_length=8, max_length=128, description="明文密码（HTTPS 保护）")
    captcha_id: str = Field(..., description="图形验证码 ID")
    captcha_result: str = Field(..., description="验证码计算结果")


class LoginRequest(BaseModel):
    """登录请求."""
    account: str
    password: str = Field(..., min_length=1, max_length=128, description="明文密码（HTTPS 保护）")
    captcha_id: str = Field(..., description="图形验证码 ID")
    captcha_result: str = Field(..., description="验证码计算结果")


class TokenRefreshRequest(BaseModel):
    """刷新令牌请求."""
    refresh_token: str = Field(..., description="刷新令牌")


class UserInfo(BaseModel):
    user_id: str
    username: str
    avatar_url: str = ""
    role: str = "user"
