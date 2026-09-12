"""模型输出统一协议防火墙与解析层。

所有模型输出在进入 SSE 之前都应经过本层：

    模型原始输出 → 增量缓冲 → DSML / 原生 Function Calling / JSON 识别
        → 统一 NormalizedToolCall → text / process / tool 分通道输出

支持三种工具协议：
  - 原生 Function Calling（provider 已给出结构化 tool_calls，直接归一化）；
  - 属性式 DSML：``<||DSML||invoke name="workspace_read" arguments="{...}"/>``；
  - 嵌套参数式 DSML：``<||DSML||invoke name="...">`` + ``<||DSML||参数 name="path">值</||DSML||参数>``。

流式规则：
  - 普通文字以小窗口（默认约 200 字符）暂存，确认不是协议前缀后再作为正文发出；
  - 一旦检测到 ``<||DSML||`` 起始标记就停止发送正文，把后续内容全部放入协议缓冲，
    标签闭合后才归一化为 ToolCall；DSML 原文永不进入最终答复正文；
  - 工具调用前的自然语言（如“我先查看一下……”）在工具解析成功后作为
    process_delta 输出（可转过程事件），失败/无工具时回退为正文；
  - 解析失败产出 warning（不直接抛给调用方），允许调用方做一次“格式纠正重试”。

Chat / ReAct / Planner 都应调用同一个解析器，不在各自维护一套协议解析代码。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from lumi_contracts.events.process import (
    SUMMARY_MAX_CHARS,
    TITLE_MAX_CHARS,
    derive_kind,
    sanitize_process_text,
)

# 过程事件的展示标题（协议层没有步骤对象，只能用通用阶段标签）。
PROCESS_ENTRY_TITLE = "执行过程"


def _normalize_protocol_text(text: str) -> str:
    """Normalize provider-specific full-width DSML punctuation.

    DeepSeek-compatible endpoints may emit ``<｜｜DSML｜｜...>`` while the
    parser historically accepted only the ASCII ``<||DSML||...>`` spelling.
    Normalizing at the stream boundary keeps the rest of the parser single
    sourced and, importantly, prevents the protocol text from falling through
    as answer text.
    """
    return str(text or "").replace("｜", "|")

# ── 统一数据结构 ─────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class NormalizedToolCall:
    """归一化后的工具调用（与供应商协议解耦）。"""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    protocol: str = ""  # native_function_calling | dsml_attribute | dsml_nested | json


@dataclass(slots=True)
class ParsedModelChunk:
    answer_delta: str = ""
    process_delta: str = ""
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    protocol_pending: bool = False
    warnings: list[str] = field(default_factory=list)


class ProtocolParseError(ValueError):
    """DSML 无法解析；调用方可据此做一次格式纠正重试。"""


# ── 标记 ────────────────────────────────────────────────
_START_RE = re.compile(r"<\|\|DSML\|\|", re.IGNORECASE)
_CLOSE_INVOKE_RE = re.compile(r"</\|\|DSML\|\|\s*(?:invoke|召唤)\s*>", re.IGNORECASE)
# 前缀匹配（用于决定“小窗口是否可安全发送正文”）
_PARTIAL_PREFIXES = ("<", "<|", "<||", "<||D", "<||DS", "<||DSM", "<||DSML", "<||DSML|", "<||DSML||")

# ── 其它工具调用形态（DSML 之外的模型幻觉/厂商文本协议）─────
# 已知的只读/工具型标签名前缀（出现即视为“非正文”工具残留）。
_KNOWN_TOOL_PREFIXES = (
    "workspace_", "read_document", "office_doc_", "document_read", "document_analyze",
    "document_edit", "query_knowledge", "kb_search", "web_search", "web_fetch",
    "tool_call", "function_call", "invoke", "call", "skill", "mcp__", "shell_",
)
# 自闭合或带属性的 XML 形态：<workspace_read path="..."/> / <workspace_read .../>
_SELF_CLOSE_TAG = re.compile(r"<(?P<name>[A-Za-z_][\w.:-]*)(?P<attrs>[^<>]*?)/\s*>", re.DOTALL)
# 开闭对形态：<workspace_read ...>...</workspace_read>
_PAIR_TAG_OPEN = re.compile(
    r"<(?P<name>[A-Za-z_][\w.:-]*)(?P<attrs>[^<>]*?)>(?P<body>.*?)"
    r"</(?P=name)\s*>", re.DOTALL | re.IGNORECASE,
)
# 属性式（含 =）标签的“疑似工具”信号：<name attr=...> / <name attr1=...
_ATTR_TOOL_SIGNAL = re.compile(r"<\s*[A-Za-z_][\w.:-]*\s[^<>]*?=", re.DOTALL)


def _tag_is_tool_like(name: str, attrs: str) -> bool:
    """保守判断某个 XML 形态标签是否像工具调用（避免误伤普通强调标签）。"""
    lowered = str(name or "").strip().casefold()
    if any(lowered.startswith(prefix.casefold()) for prefix in _KNOWN_TOOL_PREFIXES):
        return True
    # 带属性且属性名像工具参数（path/arguments/doc_id/input…）视为工具形态。
    attribute_names = set(re.findall(r"([\w:.-]+)\s*=", str(attrs or "")))
    if attribute_names & {"path", "arguments", "doc_id", "file", "filename", "name", "query", "input", "url", "tool"}:
        return True
    return False


def looks_like_tool_markup(text: str) -> bool:
    """粗检测一段文本是否包含工具调用形态（用于流中快速中断）。"""
    text = _normalize_protocol_text(text)
    if not text:
        return False
    if _START_RE.search(text) or _CLOSE_INVOKE_RE.search(text):
        return True
    for match in _PAIR_TAG_OPEN.finditer(text):
        if _tag_is_tool_like(match.group("name"), match.group("attrs")):
            return True
    for match in _SELF_CLOSE_TAG.finditer(text):
        if _tag_is_tool_like(match.group("name"), match.group("attrs")):
            return True
    # 仅剩“带属性标签”形态时，要求属性名明确像参数，降低误判。
    for match in _ATTR_TOOL_SIGNAL.finditer(text):
        segment = match.group(0)
        name = re.match(r"<\s*([A-Za-z_][\w.:-]*)", segment)
        if name is not None and _tag_is_tool_like(name.group(1), segment):
            return True
    return False


# 疑似标签起始（用于跨增量捕获：<workspace_read / <name attr=… / <||DSML||）
_OPEN_TAG_HOLD = re.compile(r"<\s*[A-Za-z_][\w.:-]*\s*$")
_OPEN_TAG_PARTIAL = re.compile(r"<\s*[A-Za-z_][\w.:-]*(\s[^<>]*?)?$")
_PROCESS_LEAD_RE = re.compile(r"^\s*(?:我来|我先|先|正在|接下来|现在先|让我|我需要)(?:读取|查看|核对|检查|分析|整理|梳理|处理|确认|了解)", re.IGNORECASE)


def _looks_like_progress_preamble(text: str) -> bool:
    """判断一段滞留文本是否只是紧贴工具调用的“进展开场白”。

    只有 ``我来读取…`` / ``先查看…`` 这类自述句才允许改道过程通道。Markdown
    标题、列表项、代码围栏和空行同样“短且以换行结尾”，但它们**是回答正文**：
    它们后面跟着工具调用时，把整块搬进 thinking 会让标题和围栏从答案里消失。
    """
    return bool(_PROCESS_LEAD_RE.search(str(text or "")))


class TextToolStripper:
    """跨增量、即时转发的工具标签剥离器（用于直答流的纯文本路径）。

    与 ModelStreamProtocolParser 的分工：
      - DSML 显式协议 → parser 负责；
      - 其它 XML 形态（workspace_read 类）→ 本剥离器在进入 parser 前去掉，
        保证任何带符号的工具残留都不会进入 delta 正文。

    设计目标：保留真实流式体感 —— feed() 每次返回“可立即外发”的干净文本，
    只对跨增量、尚未闭合的标签尾部做最小滞留；不整段缓冲、不二次调用。
    """

    def __init__(self) -> None:
        self._pending = ""       # 已进入疑似标签的残余
        self._tag_name = ""      # 开闭对模式时的标签名
        self._pair_depth = 0
        self._short_hold = ""    # 短句（≤ _SHORT_HOLD 字符）滞留，等待下一片确认不是工具前缀
        self._short_hold_len = 60
        self._process_prefix = ""  # 工具调用前的自然语言，供过程通道消费
        self._process_candidate = ""

    def feed(self, delta: str) -> list[str]:
        text = _normalize_protocol_text(delta)
        if not text:
            return []
        if self._process_candidate:
            self._process_candidate += text
            if looks_like_tool_markup(text) or self._text_starts_with_tag(text):
                # 只把标签**之前**的自述句交给过程通道：标签本身随后仍由
                # _scan 正常消费，不能借道过程通道漏出原始 XML/DSML。
                prose = self._process_candidate
                cut = self._next_tag_start(prose)
                self._process_prefix += prose if cut is None else prose[:cut]
                self._process_candidate = ""
            elif len(self._process_candidate) <= 120:
                return []
            else:
                text = self._process_candidate
                self._process_candidate = ""
        elif _PROCESS_LEAD_RE.search(text) and not looks_like_tool_markup(text):
            self._process_candidate = text
            return []
        if not self._tag_name and self._pending:
            # 上一片断疑似标签起始，但还没有名字/属性结尾，先拼接再判断。
            text = self._pending + text
            self._pending = ""
        # 上一片滞留的短句：若新片以工具标签开始，只有“我来读取…”这类开场白
        # 才改道过程通道；Markdown 块（标题/列表/围栏/空行）必须留在正文。
        held = self._short_hold
        self._short_hold = ""
        if held:
            if (looks_like_tool_markup(text) or self._text_starts_with_tag(text)) and _looks_like_progress_preamble(held):
                # This is a model-side progress sentence (for example
                # “我来读取这份 PPT…”), not answer content.  Preserve it for
                # the caller's process/thinking channel instead of leaking it
                # into the answer or silently losing it.
                self._process_prefix += held
                held = ""
            else:
                return [held, *self._scan(text)]
        out = self._scan(text)
        if out and not self._tag_name and self._is_short_sentence(out[-1]):
            self._short_hold = out.pop()
        return out

    def drain_process(self) -> str:
        """Return and clear tool-prefix prose captured during the last feeds."""
        value = strip_tool_markup(self._process_prefix)
        self._process_prefix = ""
        return value

    def flush(self) -> list[str]:
        """流结束：若残留不是完整标签则按“疑似标签前缀”丢弃，其它原样外发。"""
        tail: list[str] = []
        if self._process_candidate:
            tail.append(self._process_candidate)
            self._process_candidate = ""
        if self._short_hold:
            tail.append(self._short_hold)
            self._short_hold = ""
        if not self._pending:
            return tail
        rest = self._pending
        self._pending = ""
        if self._tag_name or looks_like_tool_markup(rest):
            return tail
        return [*tail, rest]

    @staticmethod
    def _is_short_sentence(text: str) -> bool:
        if not text or len(text) > 60:
            return False
        return bool(re.search(r"[。！？!?；;，,：:\n]$", text))

    @staticmethod
    def _text_starts_with_tag(text: str) -> bool:
        head = text.lstrip("\n\r\t ")
        return head.startswith("<") and looks_like_tool_markup(head[:128])

    def _scan(self, text: str) -> list[str]:
        out: list[str] = []
        rest = text
        while rest:
            if self._tag_name:
                marker = f"</{self._tag_name}"
                index = rest.lower().find(marker)
                if index == -1:
                    # 尚无闭合标记：整个剩余都视为标签体，丢弃。
                    rest = ""
                    break
                # 丢弃到闭合标签之后
                after = rest[index:]
                end = after.find(">")
                if end == -1:
                    # 闭合符号被截断：保留残余等待下一片。
                    self._pending = rest
                    rest = ""
                    break
                rest = after[end + 1:]
                self._tag_name = ""
                self._pair_depth = 0
                continue
            # 定位下一个“疑似标签开始”
            start = self._next_tag_start(rest)
            if start is None:
                # Keep a split DSML prefix for the protocol parser.  The
                # stripper must not emit ``<||D``/``<||DSML||`` as answer text.
                marker = "<||DSML||"
                partial = next((i for i in range(len(rest)) if marker.startswith(rest[i:])), None)
                if partial is not None:
                    if partial:
                        out.append(rest[:partial])
                    self._pending = rest[partial:]
                    rest = ""
                    break
                out.append(rest)
                break
            if start > 0:
                out.append(rest[:start])
                rest = rest[start:]
                continue
            # DSML is parsed downstream. Pass the complete marker and body
            # through untouched so ModelStreamProtocolParser can normalize it.
            if _START_RE.match(rest):
                out.append(rest)
                break
            # rest 以标签开始：判定完整/需滞留
            result = self._consume_tag(rest)
            if result is None:
                # 无法在当前片判定（标签被截断）→ 全部滞留等下一片。
                self._pending = rest
                rest = ""
                break
            rest = result
        return out

    @staticmethod
    def _next_tag_start(text: str) -> int | None:
        """返回最靠前的工具形态标签开始位置；普通文本里的 <b> 等不会命中。"""
        candidates: list[int] = []
        for match in _PAIR_TAG_OPEN.finditer(text):
            if _tag_is_tool_like(match.group("name"), match.group("attrs")):
                candidates.append(match.start())
        for match in _SELF_CLOSE_TAG.finditer(text):
            if _tag_is_tool_like(match.group("name"), match.group("attrs")):
                candidates.append(match.start())
        # 带属性的未闭合起始
        for match in _ATTR_TOOL_SIGNAL.finditer(text):
            segment = text[match.start():]
            name_match = re.match(r"<\s*([A-Za-z_][\w.:-]*)", segment)
            if name_match is not None and _tag_is_tool_like(name_match.group(1), segment):
                candidates.append(match.start())
        # DSML is intentionally left for ModelStreamProtocolParser.  Keeping
        # the marker in the stream lets the parser reclassify the natural
        # language prefix as ``process`` instead of leaking it into answer
        # text when a provider wraps invokes in ``tool_calls``.
        return min(candidates) if candidates else None

    def _consume_tag(self, text: str) -> str | None:
        """尝试把 text（以标签开始）消费到标签结束。

        Returns:
            剩余文本；None 表示标签尚未完整（截断），需要滞留等待下一片。
        """
        # DSML：交给 parser 的职责，这里按整段 DSML 直到闭合处理。
        start_match = _START_RE.search(text)
        if start_match is not None and start_match.start() == 0:
            return self._consume_dsml(text)
        opener = re.match(r"<\s*(?P<name>[A-Za-z_][\w.:-]*)(?P<body>[^<>]*)", text)
        if opener is None:
            # 极小的 '<' 片段，滞留等待。
            return None
        name = opener.group("name")
        body = opener.group("body")
        if not _tag_is_tool_like(name, body):
            # 非工具形态（如普通强调标签），原样保留，避免误伤。
            first_gt = text.find(">")
            return text[first_gt + 1:] if first_gt != -1 else None
        # 找到标签结束：单行 '>' 或自闭合 '/>'
        end = self._find_tag_end(text)
        if end is None:
            return None
        remainder = text[end:]
        if not body.strip().endswith("/"):
            # 开闭对：可能带正文，记录名字继续扫描闭合。
            self._tag_name = name
        return remainder

    @staticmethod
    def _find_tag_end(text: str) -> int | None:
        # 简化：标签内没有嵌套 '<'，闭合为第一个 '>'。
        for index, char in enumerate(text):
            if char == ">":
                return index + 1
        return None

    @staticmethod
    def _consume_dsml(text: str) -> str | None:
        # A provider may wrap one or more invokes in a ``tool_calls`` block.
        # Consume the wrapper as one protocol unit; otherwise its closing tag
        # would leak into the answer after the inner invoke was removed.
        wrapper_end = re.search(
            r"</\|\|DSML\|\|\s*tool_calls\s*>", text,
            flags=re.IGNORECASE,
        )
        if wrapper_end is not None:
            return text[wrapper_end.end():]
        # 属性式自闭合或嵌套式直到 </||DSML||...>
        attr_end = re.search(r"/\s*>", text, flags=re.DOTALL)
        nested_end = _CLOSE_INVOKE_RE.search(text)
        if attr_end is not None:
            return text[attr_end.end():]
        if nested_end is not None:
            return text[nested_end.end():]
        return None


def strip_tool_markup(text: str) -> str:
    """尽力从文本中剥离工具调用形态；残余仅作为兜底（正常不应触发）。

    ⚠️ **绝不做 ``.strip()``**：本函数在流式路径里是**逐增量**调用的，任何"顺手的
    去空白"都会吃掉 Markdown 的结构空白——``"##"`` + ``" 总体概况"`` 会粘成
    ``"##总体概况"``，``"\\n\\n"`` 会整段消失，代码块围栏后的换行也会丢。
    因此：没有协议残留时原样返回；真的剥掉了标记时也只删除被匹配的片段。
    """
    if not text:
        return text
    cleaned = _normalize_protocol_text(text)
    original = cleaned
    # 1) DSML 块：属性式自闭合 + 嵌套式
    cleaned = re.sub(
        r"(?s)<\|\|DSML\|\|\s*(?:invoke|召唤)\b.*?(?:/>|</\|\|DSML\|\|\s*(?:invoke|召唤)\s*>)",
        "", cleaned, flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"(?s)<\|\|DSML\|\|\s*(?:参数|param|parameter)\b.*?(?:/>|</\|\|DSML\|\|\s*(?:参数|param|parameter)\s*>)",
        "", cleaned, flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"</?\|\|DSML\|\|\s*(?:tool_calls|/tool_calls)\s*>",
        "", cleaned, flags=re.IGNORECASE,
    )
    # 2) 开闭对形态（先剥对，再剥自闭合），保留同名内部正文以外内容
    for match in reversed(list(_PAIR_TAG_OPEN.finditer(cleaned))):
        if _tag_is_tool_like(match.group("name"), match.group("attrs")):
            cleaned = cleaned[: match.start()] + cleaned[match.end():]
    cleaned = _SELF_CLOSE_TAG.sub(
        lambda m: "" if _tag_is_tool_like(m.group("name"), m.group("attrs")) else m.group(0),
        cleaned,
    )
    # 没有剥掉任何东西 → 原样返回（**不要**动空白：见上面的流式约束）。
    if cleaned == original:
        return text
    return cleaned


def normalize_native_tool_calls(
    tool_calls: list[dict] | None,
) -> list[NormalizedToolCall]:
    """把 provider 原生 Function Calling 结构归一化。"""
    out: list[NormalizedToolCall] = []
    for raw in tool_calls or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = str(function.get("name") or raw.get("name") or "").strip()
        if not name:
            continue
        raw_args = function.get("arguments")
        arguments: dict[str, Any] = {}
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args or "{}")
                if isinstance(parsed, dict):
                    arguments = parsed
            except (TypeError, ValueError):
                arguments = {}
        out.append(NormalizedToolCall(
            name=name,
            arguments=arguments,
            call_id=str(raw.get("id") or ""),
            protocol="native_function_calling",
        ))
    return out


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_key_values(segment: str) -> dict[str, str]:
    """解析 ``name="x" arguments="{...}"`` 形式的属性序列（容忍单双引号）。"""
    attrs: dict[str, str] = {}
    for match in re.finditer(r'([\w.-]+)\s*=\s*("(?:\\.|[^"])*"|\'(?:\\.|[^\'])*\')', segment):
        key = str(match.group(1)).strip()
        value = _strip_quotes(match.group(2))
        if key:
            attrs[key] = value
    return attrs


def _parse_invoke_attributes(segment: str) -> NormalizedToolCall | None:
    attrs = _parse_key_values(segment)
    name = str(attrs.get("name") or "").strip()
    if not name:
        return None
    arguments: dict[str, Any] = {}
    raw_args = attrs.get("arguments") or attrs.get("参数") or ""
    if raw_args:
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                arguments = parsed
        except (TypeError, ValueError):
            arguments = {"_raw": raw_args}
    return NormalizedToolCall(name=name, arguments=arguments, protocol="dsml_attribute")


def _parse_nested_arguments(text: str) -> dict[str, Any]:
    """解析嵌套 ``<||DSML||参数 name="path">值</||DSML||参数>`` 片段。"""
    out: dict[str, Any] = {}
    pattern = re.compile(
        r"<\|\|DSML\|\|\s*(?:参数|param|parameter)\s+([^>]*)>(.*?)</\|\|DSML\|\|\s*(?:参数|param|parameter)\s*>",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        attrs = _parse_key_values(match.group(1))
        name = str(attrs.get("name") or "").strip()
        if not name:
            continue
        value: Any = match.group(2).strip()
        # 声明 string="true" 时按字符串保留；否则尽力类型化（JSON/数值/布尔）。
        if str(attrs.get("string") or attrs.get("is_string") or "").casefold() in {
            "true", "1", "yes",
        }:
            out[name] = value
        else:
            out[name] = _coerce_scalar(value)
    return out


def _coerce_scalar(value: str) -> Any:
    text = value.strip()
    if text == "":
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    return text


# ── 主解析器（增量流式）─────────────────────────────────

class ModelStreamProtocolParser:
    """增量缓冲解析器：feed(delta) → list[ParsedModelChunk]。

    使用“小窗口延迟发送”：正文暂存，确认不是协议前缀后再发出；一旦进入
    DSML 协议段，正文停止外发，标签闭合后统一归一化。
    """

    def __init__(self, *, flush_window: int = 200) -> None:
        self._flush_window = max(32, int(flush_window))
        self._text: list[str] = []           # 已收到的完整增量原文（审计/trace 用）
        self._lead_text: str = ""            # 尚未发出的普通文字
        self._protocol_text: str = ""        # 正在缓冲的协议原文
        self._in_protocol = False
        self._call_id_seq = 0

    # -- 供 trace 的原文 --
    @property
    def raw_output(self) -> str:
        return "".join(self._text)

    def _next_call_id(self) -> str:
        self._call_id_seq += 1
        return f"model-{self._call_id_seq}"

    def feed(self, delta: str) -> list[ParsedModelChunk]:
        """接收一段增量文本，返回本轮可发出的分片列表。"""
        chunks: list[ParsedModelChunk] = []
        delta = _normalize_protocol_text(delta)
        self._text.append(delta)
        if not self._in_protocol:
            # 找协议起始
            buffer = self._lead_text + delta
            idx = _START_RE.search(buffer)
            if idx is not None:
                lead = buffer[: idx.start()]
                self._lead_text = lead
                self._protocol_text = buffer[idx.start():]
                self._in_protocol = True
                chunks.extend(self._try_parse_protocol())
            else:
                self._lead_text = buffer
                chunks.extend(self._flush_safe_text())
            return chunks
        self._protocol_text += delta
        return self._try_parse_protocol()

    def _flush_safe_text(self) -> list[ParsedModelChunk]:
        """把确认安全（不可能是协议前缀）的正文发出去；尾部可能的前缀扣留。"""
        text = self._lead_text
        if not text:
            return []
        hold = 0
        for prefix in _PARTIAL_PREFIXES:
            if text.endswith(prefix):
                hold = max(hold, len(prefix))
        safe_len = len(text) - hold
        if safe_len <= 0:
            if len(text) >= self._flush_window * 2:
                # 长期无法判定（例如正文本身就是大量 '<'），按窗口强制发出
                safe_len = self._flush_window
            else:
                return []
        self._lead_text = text[safe_len:]
        return [ParsedModelChunk(answer_delta=text[:safe_len])]

    def _try_parse_protocol(self) -> list[ParsedModelChunk]:
        """按“一次一条调用”消费协议缓冲；未闭合则保持 pending。"""
        chunks: list[ParsedModelChunk] = []
        protocol = self._protocol_text
        end = self._call_end_offset(protocol)
        if end is None:
            chunks.append(ParsedModelChunk(protocol_pending=True))
            return chunks
        segment = protocol[:end]
        call, warning = self._parse_segment(segment)
        if warning:
            chunks.append(ParsedModelChunk(
                protocol_pending=False,
                warnings=[warning],
                process_delta=self._take_lead_as_process(),
            ))
        elif call is None:
            chunks.append(ParsedModelChunk(protocol_pending=True))
            return chunks
        else:
            # 工具前的自然语言转 process；DSML 原文不进入正文
            chunks.append(ParsedModelChunk(
                process_delta=self._take_lead_as_process(),
                tool_calls=[call],
            ))
        rest = protocol[end:]
        self._protocol_text = ""
        self._in_protocol = False
        if rest.strip():
            # 同一片段后面还有普通文本或下一个协议
            chunks.extend(self.feed(rest))
        return chunks

    @staticmethod
    def _call_end_offset(protocol: str) -> int | None:
        """定位协议缓冲中第一条完整调用的结束位置（无闭合返回 None）。"""
        text = protocol.strip()
        # DeepSeek may wrap one or more invokes in a tool_calls envelope.
        # Treat the envelope as a single protocol unit so its closing marker
        # cannot leak after the inner invoke is parsed.
        wrapper = re.match(r"(?s)<\|\|DSML\|\|\s*tool_calls\s*>", text)
        if wrapper:
            close = re.search(r"</\|\|DSML\|\|\s*tool_calls\s*>", text[wrapper.end():], re.IGNORECASE)
            if close:
                return wrapper.end() + close.end()
        # 属性式自闭合：<||DSML||invoke ... />
        attr = re.match(
            r"(?s)<\|\|DSML\|\|\s*(?:invoke|召唤)\b.*?/\s*>", text
        )
        if attr:
            return attr.end()
        # 嵌套参数式：<||DSML||invoke ...>...</||DSML||invoke>
        nested = re.match(
            r"(?s)<\|\|DSML\|\|\s*(?:invoke|召唤)\b.*?</\|\|DSML\|\|\s*(?:invoke|召唤)\s*>",
            text,
        )
        if nested:
            return nested.end()
        return None

    def _take_lead_as_process(self) -> str:
        text = self._lead_text
        self._lead_text = ""
        return text

    def _parse_segment(self, segment: str) -> tuple[NormalizedToolCall | None, str | None]:
        """解析“单条调用”协议片段。返回 (call, warning)。"""
        text = segment.strip()
        if not text.startswith("<||DSML||"):
            return None, "协议段缺少起始标记"
        # Unwrap ``tool_calls`` emitted by some OpenAI-compatible providers;
        # the actual invoke parser remains unchanged and returns one call at a
        # time.  Any additional invokes are handled on the next stream turn.
        wrapper = re.match(r"(?s)<\|\|DSML\|\|\s*tool_calls\s*>", text)
        if wrapper:
            inner = re.search(r"<\|\|DSML\|\|\s*(?:invoke|召唤)\b.*", text[wrapper.end():], re.IGNORECASE)
            if inner:
                text = inner.group(0).strip()
        # 嵌套参数式优先（包含内部参数标签时不应被属性分支误切）
        nested = re.match(
            r"<\|\|DSML\|\|\s*(?:invoke|召唤)\s+([^>]*)>(.*?)</\|\|DSML\|\|\s*(?:invoke|召唤)\s*>",
            text, flags=re.IGNORECASE | re.DOTALL,
        )
        if nested and nested.group(1) is not None:
            attrs = _parse_key_values(str(nested.group(1) or ""))
            name = str(attrs.get("name") or "").strip()
            if not name:
                return None, "DSML 嵌套调用缺少 name 属性"
            arguments = _parse_nested_arguments(str(nested.group(2) or ""))
            raw_args = attrs.get("arguments") or ""
            if raw_args:
                try:
                    parsed = json.loads(raw_args)
                    if isinstance(parsed, dict):
                        arguments.update(parsed)
                except (TypeError, ValueError):
                    pass
            return NormalizedToolCall(
                name=name, arguments=arguments,
                call_id=self._next_call_id(), protocol="dsml_nested",
            ), None
        # 属性式：<||DSML||invoke name=... arguments=... />
        inner = re.sub(r"^<\|\|DSML\|\|\s*(?:invoke|召唤)\s*", "", text, flags=re.IGNORECASE)
        inner = re.sub(r"/?\s*>?$", "", inner).strip()
        call = _parse_invoke_attributes(inner)
        if call is None:
            return None, "DSML 属性式调用解析失败（缺 name 或参数不完整）"
        return NormalizedToolCall(
            name=call.name, arguments=call.arguments,
            call_id=self._next_call_id(), protocol=call.protocol,
        ), None

    def finalize(self) -> list[ParsedModelChunk]:
        """流结束：冲刷剩余正文；残留未闭合协议给出 warning（可触发一次纠正重试）。"""
        chunks: list[ParsedModelChunk] = []
        if self._in_protocol:
            chunks.append(ParsedModelChunk(
                protocol_pending=False,
                warnings=["协议未闭合，已丢弃该段（可执行一次格式纠正重试）"],
                process_delta=self._take_lead_as_process(),
            ))
            self._protocol_text = ""
            self._in_protocol = False
        remaining = self._lead_text
        if remaining:
            self._lead_text = ""
            chunks.append(ParsedModelChunk(answer_delta=remaining))
        return chunks


def parse_one_shot(text: str) -> tuple[list[NormalizedToolCall], list[str]]:
    """一次性解析整段文本中的 DSML 调用（测试/重放用）。"""
    parser = ModelStreamProtocolParser()
    calls: list[NormalizedToolCall] = []
    warnings: list[str] = []
    for chunk in parser.feed(text):
        calls.extend(chunk.tool_calls)
        warnings.extend(chunk.warnings)
    for chunk in parser.finalize():
        calls.extend(chunk.tool_calls)
        warnings.extend(chunk.warnings)
    return calls, warnings


def chunk_to_events(
    chunk: ParsedModelChunk,
    *,
    on_tool=None,
) -> list[dict]:
    """把一个 ParsedModelChunk 转成 SSE 风格事件 dict 列表。

    事件类型：
      {"type": "delta", "content": ...}          —— 正文增量；
      {"type": "process", "content": ...}        —— 过程说明（工具调用前的自然语言）；
      {"type": "tool", "tool_call": {...}}       —— 归一化工具调用（on_tool 可改写/分发）；
      {"type": "warning", "content": ...}        —— 协议解析告警（供调用方做一次格式纠正重试）。

    过程/工具事件同时携带**语义过程字段**（``entry_id`` / ``kind`` / ``title`` /
    ``summary`` / ``status``，全部过 ``sanitize_process_text``）：``kind`` 由后端按
    工具名判定，前端只渲染；``tool`` 事件用稳定 ``call_id`` 作 ``entry_id``，与
    后续同一调用的完成事件合并成一行。原始 ``tool_call.arguments`` 只保留在既有
    的 ``tool_call`` 字段里（内部消费者用），绝不复制进过程展示字段。

    Chat / ReAct / Planner 的流式出口都应经过这里，共用同一个解析器。
    """
    events: list[dict] = []
    if chunk.answer_delta:
        events.append({"type": "delta", "content": chunk.answer_delta})
    if chunk.process_delta:
        events.append({
            "type": "process",
            "content": chunk.process_delta,
            # 语义过程字段：kind 由后端判定；标题是通用阶段标签，摘要复用解析出的
            # 工具前置说明（本来就是发给用户的自然语言，非模型推理链）。
            "kind": str(derive_kind(event_type="process")),
            "title": sanitize_process_text(PROCESS_ENTRY_TITLE, limit=TITLE_MAX_CHARS),
            "summary": sanitize_process_text(chunk.process_delta, limit=SUMMARY_MAX_CHARS),
            "status": "running",
        })
    for call in chunk.tool_calls:
        if on_tool is not None:
            produced = on_tool(call)
            if produced is not None:
                events.append(dict(produced))
                continue
        events.append({
            "type": "tool",
            "tool_call": {
                "name": call.name,
                "arguments": call.arguments,
                "call_id": call.call_id,
                "protocol": call.protocol,
            },
            # 语义过程字段：entry_id 用稳定 call_id（同一调用的完成事件复用同一
            # call_id，因此合并成一行）；工具名只以净化后的短文本出现。
            "tool_name": sanitize_process_text(call.name, limit=TITLE_MAX_CHARS),
            "call_id": str(call.call_id or ""),
            "entry_id": f"call:{call.call_id}" if call.call_id else "",
            "kind": str(derive_kind(tool_name=str(call.name or ""))),
            "title": sanitize_process_text(call.name or "工具调用", limit=TITLE_MAX_CHARS),
            "summary": sanitize_process_text(
                f"正在调用 {call.name}", limit=SUMMARY_MAX_CHARS
            ),
            "status": "running",
        })
    for warning in chunk.warnings:
        events.append({"type": "warning", "content": warning})
    return events


async def protocol_events_from_stream(source, *, on_tool=None):
    """包装一个产出文本增量的异步流，输出事件 dict。

    ``source``：async iterable[str]（如 provider 的增量输出）。
    本适配器保证：小窗口延迟发送、DSML 原文不进入 delta/process、工具调用前
    的自然语言作为 process 事件、协议解析告警以 warning 事件暴露（供上层
    做一次格式纠正重试）。调用方需自行决定对 "tool" 事件执行/排队。
    """
    parser = ModelStreamProtocolParser()
    async for text in source:
        for chunk in parser.feed(str(text or "")):
            for event in chunk_to_events(chunk, on_tool=on_tool):
                yield event
    for chunk in parser.finalize():
        for event in chunk_to_events(chunk, on_tool=on_tool):
            yield event
