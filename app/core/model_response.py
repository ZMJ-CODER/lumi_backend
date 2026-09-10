"""模型响应归一化：把供应商差异收敛为统一工具调用协议。"""

from __future__ import annotations

import html
import json
import re
from typing import Any


_DSML_INVOKE_RE = re.compile(
    r"<(?:｜｜DSML｜｜|\|\|DSML\|\|)\s*invoke\b(?P<attrs>.*?)(?:/\s*>|>\s*.*?</(?:｜｜DSML｜｜|\|\|DSML\|\|)\s*invoke\s*>)",
    re.IGNORECASE | re.DOTALL,
)
_ATTR_RE = re.compile(
    r'''(?P<key>name|arguments|args)\s*=\s*(?P<quote>["'])(?P<value>(?:\\.|(?!\2).)*)(?P=quote)''',
    re.IGNORECASE | re.DOTALL,
)
_NAME_RE = re.compile(r'''\bname\s*=\s*(["'])(?P<value>.*?)(?:\1)''', re.IGNORECASE | re.DOTALL)


def _decode_arguments(value: str | None) -> tuple[dict[str, Any], str | None]:
    if not value:
        return {}, None
    raw = html.unescape(value).strip()
    candidates = [raw, raw.replace('\\"', '"').replace("\\'", "'")]
    parsed = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            break
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    if parsed is None:
        return {}, "工具参数 JSON 无法解析"
    if isinstance(parsed, dict):
        return parsed, None
    return {}, "工具参数必须是 JSON 对象"


def normalize_tool_response(content: object, tool_calls: list[dict] | None = None) -> tuple[str, list[dict], list[str]]:
    """统一标准 tool_calls 与 DeepSeek DSML 调用。"""
    text = str(content or "").replace("｜", "|")
    normalized: list[dict] = []
    warnings: list[str] = []
    for index, call in enumerate(tool_calls or []):
        function = call.get("function") if isinstance(call, dict) else {}
        function = function if isinstance(function, dict) else {}
        name = str(function.get("name") or call.get("name") or "").strip()
        args = function.get("arguments", call.get("args", {}))
        if isinstance(args, str):
            args, warning = _decode_arguments(args)
            if warning:
                warnings.append(f"{name or '未知工具'}：{warning}")
        if not isinstance(args, dict):
            args = {}
            warnings.append(f"{name or '未知工具'}：工具参数必须是对象")
        normalized_item = {
            "id": str(call.get("id") or f"call-{index + 1}"),
            "type": "function",
            "function": {"name": name, "arguments": args},
        }
        # Thinking-mode providers (notably DeepSeek) require the opaque
        # reasoning_content to be replayed on the assistant tool-call message
        # that precedes the tool result.  Keep it as transport metadata; it is
        # never rendered in the user-facing answer.
        if isinstance(call, dict) and call.get("reasoning_content") is not None:
            normalized_item["reasoning_content"] = call.get("reasoning_content")
        normalized.append(normalized_item)

    for match in _DSML_INVOKE_RE.finditer(text):
        attrs = match.group("attrs") or match.group("attrs_ascii") or ""
        parsed_attrs = {item.group("key").casefold(): item.group("value") for item in _ATTR_RE.finditer(attrs)}
        name_match = _NAME_RE.search(attrs)
        name = str(name_match.group("value") if name_match else parsed_attrs.get("name") or "").strip()
        args, warning = _decode_arguments(parsed_attrs.get("arguments") or parsed_attrs.get("args"))
        # Nested DeepSeek parameter tags are not JSON arguments. Convert the
        # string-valued parameter elements into the standard arguments object.
        nested_args = {}
        for param in re.finditer(
            r"<\|\|DSML\|\|\s*(?:参数|param|parameter)\s+([^>]*)>(.*?)</\|\|DSML\|\|\s*(?:参数|param|parameter)\s*>",
            match.group(0), re.IGNORECASE | re.DOTALL,
        ):
            attrs = {m.group(1).casefold(): m.group(3) for m in re.finditer(r'([\w.-]+)\s*=\s*(["\'])(.*?)\2', param.group(1))}
            key = str(attrs.get("name") or "").strip()
            if key:
                nested_args[key] = param.group(2).strip()
        if nested_args:
            args.update(nested_args)
        if warning:
            warnings.append(f"{name or '未知工具'}：{warning}")
        normalized.append({
            "id": f"dsml-{len(normalized) + 1}",
            "type": "function",
            "function": {"name": name, "arguments": args},
        })

    # Ollama 等本地模型可能不支持原生 Function Calling，但可以按约定
    # 输出一个很小的 JSON 对象。仅在完整文本看起来就是对象时解析，避免
    # 误把普通回答中的 JSON 片段当成工具调用。
    if not normalized:
        candidate = text.strip()
        if candidate.startswith("```") and candidate.endswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE | re.DOTALL).strip()
        if candidate.startswith("{") and candidate.endswith("}"):
            try:
                payload = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                raw_calls = payload.get("tool_calls") or payload.get("calls")
                if isinstance(raw_calls, list):
                    iterable = raw_calls
                elif payload.get("name") or payload.get("tool"):
                    iterable = [payload]
                else:
                    iterable = []
                for index, item in enumerate(iterable):
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or item.get("tool") or "").strip()
                    args = item.get("arguments", item.get("args", {}))
                    if isinstance(args, str):
                        args, warning = _decode_arguments(args)
                        if warning:
                            warnings.append(f"{name or '未知工具'}：{warning}")
                    if name:
                        normalized.append({
                            "id": f"json-{index + 1}",
                            "type": "function",
                            "function": {"name": name, "arguments": args if isinstance(args, dict) else {}},
                        })
                if normalized:
                    text = ""
                elif payload.get("answer") is not None:
                    text = str(payload.get("answer") or "")

    clean = _DSML_INVOKE_RE.sub("", text)
    clean = re.sub(r"</?\|\|DSML\|\|\s*(?:tool_calls|/tool_calls)\s*>", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"</?\|DSML\|\s*(?:tool_calls|/tool_calls)\s*>", "", clean, flags=re.IGNORECASE)
    return clean.strip(), normalized, warnings


__all__ = ["normalize_tool_response"]
