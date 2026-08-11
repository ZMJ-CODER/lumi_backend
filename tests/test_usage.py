"""token 用量统计纯函数测试."""

from app.services.usage import estimate_tokens


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_chinese():
    # 中文 1 字符 ≈ 1 token
    assert estimate_tokens("你好") >= 2


def test_estimate_tokens_latin():
    assert estimate_tokens("hello world") > 0
