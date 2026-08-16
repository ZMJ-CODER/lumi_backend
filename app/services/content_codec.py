"""消息内容编解码 —— assistant 消息按"一次交互"存储.

存储约定：assistant 的 content 字段存 JSON 数组（多条短句合并为一个数组），
无论 AI 回复拆成多少条短句，在数据库/Redis 中都只占一个对象、计一次交互。
旧数据仍是纯文本字符串，读取时两种形态都兼容。
"""

from __future__ import annotations

import json


def _looks_like_array(text: str) -> bool:
    stripped = text.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def serialize_content(content) -> str:
    """把 assistant 回复序列化为 JSON 数组字符串（多短句合并为一次交互）.

    - list → 直接序列化（过滤空段）
    - 已是数组格式的字符串 → 原样返回
    - 普通文本 → 包成单元素数组
    """
    if isinstance(content, list):
        segments = [str(seg) for seg in content if str(seg).strip()]
        return json.dumps(segments, ensure_ascii=False)
    if isinstance(content, str) and _looks_like_array(content):
        try:
            arr = json.loads(content)
            if isinstance(arr, list):
                return content
        except (ValueError, TypeError):
            pass
    return json.dumps([str(content or "")], ensure_ascii=False)


def normalize_content(content) -> str:
    """把存储内容统一转成可读/可送模型的纯文本（数组 → 多行文本）."""
    if isinstance(content, list):
        return "\n".join(str(seg) for seg in content if str(seg).strip())
    if isinstance(content, str) and _looks_like_array(content):
        try:
            arr = json.loads(content)
            if isinstance(arr, list):
                return "\n".join(str(seg) for seg in arr if str(seg).strip())
        except (ValueError, TypeError):
            pass
        return content.strip()
    return content or ""


def split_segments(content) -> list[str]:
    """返回消息的短句数组（存储形态解包；普通文本视为单段）."""
    if isinstance(content, list):
        return [str(seg) for seg in content if str(seg).strip()]
    if isinstance(content, str) and _looks_like_array(content):
        try:
            arr = json.loads(content)
            if isinstance(arr, list):
                return [str(seg) for seg in arr if str(seg).strip()]
        except (ValueError, TypeError):
            pass
    text = str(content or "").strip()
    return [text] if text else []
