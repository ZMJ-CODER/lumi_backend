"""统一工作区读取域（后端侧 ``workspace_navigator`` 的实现）。

模型只看到 **一个** 工作区读取工具::

    workspace_navigator(action="list" | "search" | "read" | "scan", ...)

它的内部结构不是“上帝函数”，而是：::

    工具协议入口（execute_tool_call）
        ↓
    WorkspaceNavigatorService（action 校验 / 参数校验 / 工作区注入 / 路由 / 信封）
        ↓
    Electron MCP（workspace_list / workspace_search / workspace_content_extract…）
    + 内部处理器 NavigatorListHandler / NavigatorSearchHandler / NavigatorReadHandler
      / NavigatorScanHandler（代码骨架，服务端 code_structure 纯函数解析）

设计约束（与产品方案一致）：

* **工作区授权不来自模型**：``workspace_id`` 一律由 ``execute_tool_call`` 用
  ``authorized_workspace_id`` 注入；模型传的 workspace_id 被忽略，绝不作为授权依据。
* **list 不读正文**：只返回条目元数据（名称/相对路径/类型/大小/修改时间/扩展名/
  是否被忽略），并强制 depth / 条目数 / 扫描时间上限。默认由 Electron 过滤隐藏项与
  node_modules/构建/缓存目录（`include_ignored=true` 才返回并保留 `ignored` 标记）。
* **search 不返回整份文档**：只返回命中文件路径、命中位置（页码/段落/行号/Sheet）、
  少量上下文与文件格式；内容搜索复用 Electron 侧按格式选择的解析器
  （PPTX/DOCX/XLSX/PDF/图片 OCR/编码识别文本），绝不把所有文件当 UTF-8 读。
  `query` 原样透传：Electron 把 `| * [ ( ^ $ .` 等当分隔符做子串/中文 2-gram 命中
  （等价 OR，按相关度排序），后端不拆词、不当正则。
* **read 保持原子**：一次只读一个文件；目录路径直接拒绝并返回
  ``WORKSPACE_PATH_NOT_DIRECTORY`` + ``suggested_action="list"``；分页用 ``cursor``。
  **普通读取不弹确认**；单次返回上限 4000 字符（``READ_MAX_CHARS``，是分页粒度
  而不是安全上限），超出用 cursor 继续。凭据/密钥/PII 由 ``redact_sensitive_text``
  自动脱敏（保留键名、遮蔽值），返回 ``meta.sensitive/sensitivity/redacted/
  redaction_count``；续页每一页都重新脱敏，**敏感原文不会借 cursor 继续暴露**。
* **search 是定位而不是取正文**：命中上下文固定 320 字符（``CONTEXT_SNIPPET``），
  该长度**不暴露给模型**（需要完整内容应当 read）；命中片段同样先脱敏再返回。
* **cursor 托管**：Electron 的 cursor 是自包含 base64（list/search 为 `nav1`、
  read 为 `v1`），后端把它存进自己的 TTL 游标表并在续页**原样回传**（不包装、
  不重编码）；Electron 没给 cursor 时才退回后端 offset 切片。续页沿用首次请求的
  `include_ignored` / `search_mode`，与 Electron 的 cursor 绑定校验一致。
* **统一信封**：所有动作返回同一结构（status/action/summary/data/has_more/cursor/
  meta/error），错误码稳定，失败尽量带 ``error.suggested_action`` 帮模型自我修正。

返回值不抛异常：任何内部错误都收敛为 ``status="error"`` 的信封，由调用方
（executor / orchestrator）决定如何反馈给模型。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

from app.contracts.operations import TRASH_DIRNAME, RevisionRules, is_trash_path

# ── 动作与参数契约 ──────────────────────────────────────────
ACTION_LIST = "list"
ACTION_SEARCH = "search"
ACTION_READ = "read"
ACTION_SCAN = "scan"
ACTIONS: tuple[str, ...] = (ACTION_LIST, ACTION_SEARCH, ACTION_READ, ACTION_SCAN)

SEARCH_MODE_AUTO = "auto"
SEARCH_MODE_FILENAME = "filename"
SEARCH_MODE_CONTENT = "content"
SEARCH_MODES: tuple[str, ...] = (SEARCH_MODE_AUTO, SEARCH_MODE_FILENAME, SEARCH_MODE_CONTENT)

# 状态值：ok（完整）/ partial（有下一页）/ empty（无结果）/ error（失败）
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"

# ── 稳定错误码（模型与前端联调共用）──────────────────────────
INVALID_ACTION = "INVALID_ACTION"
INVALID_PARAMS = "INVALID_PARAMS"
WORKSPACE_NOT_BOUND = "WORKSPACE_NOT_BOUND"
WORKSPACE_NOT_REGISTERED = "WORKSPACE_NOT_REGISTERED"
WORKSPACE_DEVICE_OFFLINE = "WORKSPACE_DEVICE_OFFLINE"
WORKSPACE_PATH_NOT_FOUND = "WORKSPACE_PATH_NOT_FOUND"
WORKSPACE_PATH_NOT_DIRECTORY = "WORKSPACE_PATH_NOT_DIRECTORY"
WORKSPACE_UNSUPPORTED_FORMAT = "WORKSPACE_UNSUPPORTED_FORMAT"
WORKSPACE_READ_FAILED = "WORKSPACE_READ_FAILED"
SEARCH_TIMEOUT = "SEARCH_TIMEOUT"
CURSOR_EXPIRED = "CURSOR_EXPIRED"
#: 回收站路径：普通工作区操作（list/read/search/scan/write/edit/move/delete）一律拒止。
TRASH_PATH_FORBIDDEN = "TRASH_PATH_FORBIDDEN"

# 失败时给模型的自我修正提示（不是通用重试）
_ERROR_SUGGESTIONS: dict[str, str] = {
    INVALID_ACTION: "action 只能是 list/search/read/scan；请重新选择动作。",
    INVALID_PARAMS: "请按工具 schema 补全或修正参数后重试一次。",
    WORKSPACE_NOT_BOUND: "本轮没有绑定工作区；不要编造文件内容，直接说明无法读取。",
    WORKSPACE_NOT_REGISTERED: "工作区未注册或没有可路由的桌面连接；请如实说明后停止读取。",
    WORKSPACE_DEVICE_OFFLINE: "托管该工作区的桌面当前离线；请如实说明并停止读取，不要重试。",
    WORKSPACE_PATH_NOT_FOUND: "路径不存在；建议先 action=list 或 action=search 重新定位文件。",
    WORKSPACE_PATH_NOT_DIRECTORY: "该路径是目录，不能读取正文；先 action=list 浏览该目录，再对具体文件 read。",
    WORKSPACE_UNSUPPORTED_FORMAT: "该格式不是文本/可解析文档；如确需处理请说明，不要按文本硬读。",
    WORKSPACE_READ_FAILED: "读取失败；可换一个候选文件，或先 search 缩小范围。",
    SEARCH_TIMEOUT: "搜索超时；请缩小 search_path 或改用更具体的关键词。",
    CURSOR_EXPIRED: "游标已过期；请重新发起本次 list/search/read，不要复用旧 cursor。",
    TRASH_PATH_FORBIDDEN: "回收站 .lumi_trash 不是普通目录：内容只能用恢复/清理接口访问。",
}

# Electron 侧可能返回的“目录被当文件读”信号
_DIRECTORY_ERROR_CODES = frozenset({
    "WORKSPACE_PATH_NOT_DIRECTORY", "WORKSPACE_PATH_IS_DIRECTORY",
    "EISDIR", "IS_A_DIRECTORY", "DIRECTORY_NOT_FILE",
})
# Electron 侧可能返回的“文件不存在”信号
_NOT_FOUND_ERROR_CODES = frozenset({
    "WORKSPACE_PATH_NOT_FOUND", "ENOENT", "FILE_NOT_FOUND", "NOT_FOUND",
})
# Electron 侧透出的连接/注册错误（其余一律折叠为 WORKSPACE_READ_FAILED）
_PASSTHROUGH_ERROR_CODES = frozenset({
    WORKSPACE_NOT_BOUND, WORKSPACE_NOT_REGISTERED, WORKSPACE_DEVICE_OFFLINE,
    "WORKSPACE_ROOT_MISSING",
})

# ── 参数钳制（后端是最终裁决者，模型给的值只是建议）─────────
DEFAULT_DEPTH = 1
MAX_DEPTH = 4
DEFAULT_MAX_RESULTS = 50
MAX_MAX_RESULTS = 200
DEFAULT_MAX_CHARS = 12000
MIN_MAX_CHARS = 500
MAX_MAX_CHARS = 50000
DEFAULT_TIMEOUT_SECONDS = 20.0
MAX_TIMEOUT_SECONDS = 60.0
DEFAULT_LIST_MAX_ENTRIES = 200
DEFAULT_SEARCH_MAX_RESULTS = 30
# 搜索命中上下文长度：**后端固定，不暴露给模型**。search 的职责是"定位"而不是
# 让模型调整上下文预算；需要完整内容时应该 read。Electron 只接受 80–600。
CONTEXT_SNIPPET = 320
MIN_CONTEXT_SNIPPET = 80
MAX_CONTEXT_SNIPPET = 600
# 单次向 Electron 请求的最大条目数（Electron 侧上限：list 1000 / search 200）。
MAX_ELECTRON_LIST_ENTRIES = 1000
MAX_ELECTRON_SEARCH_RESULTS = 200
# Electron 的 workspace_search 只接受 80–600 的上下文预算。
MIN_SEARCH_CONTEXT_CHARS = 80
MAX_SEARCH_CONTEXT_CHARS = 600

# 默认跳过的目录（前端仍会做一次同样的过滤，这里只是后端兜底与提示）
DEFAULT_SKIP_DIRS = (".git", "node_modules", "__pycache__", ".venv", "dist", "build", ".cache")

# 纯文本类扩展名（可直接按编码识别读取；其余交给格式解析器）
_TEXT_EXTS = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".ini", ".cfg", ".conf", ".toml", ".env", ".xml", ".html", ".htm",
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".svelte",
    ".java", ".kt", ".go", ".rs", ".rb", ".php", ".cs", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".m", ".mm", ".swift", ".scala", ".sh", ".bash", ".zsh", ".ps1", ".bat",
    ".cmd", ".sql", ".graphql", ".proto", ".dockerfile", ".gitignore", ".editorconfig",
})
# 需要专用解析器的二进制/复合文档格式
_PARSED_EXTS = frozenset({
    ".pptx", ".ppt", ".docx", ".doc", ".xlsx", ".xls", ".pdf", ".rtf",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif",
    ".eml", ".ics", ".odt", ".ods", ".odp",
})
# 明确不支持的正文格式（压缩包/可执行/音视频等）
_UNSUPPORTED_EXTS = frozenset({
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".exe", ".dll", ".so",
    ".dylib", ".bin", ".iso", ".dmg", ".msi", ".class", ".jar", ".war", ".pyc",
    ".mp3", ".wav", ".flac", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ttf",
    ".otf", ".woff", ".woff2", ".db", ".sqlite", ".sqlite3", ".pkl", ".npy", ".pt",
})

# 敏感路径标记：命中时后端会做敏感检测、脱敏并标注，读本身不弹确认。
SENSITIVE_PATH_MARKERS = (
    ".env", ".pem", ".key", "id_rsa", "credentials", "secrets", ".npmrc",
    "token", "password", "aws", "gcloud", "kubeconfig",
)
# 单次**每页**返回上限（分页粒度，**不是**可读取总量上限）：一次 read 默认返回
# 一页约 4000 字符，文件更长时用 cursor 继续，直到把整份读完。不要把 4000 理解成
# "这个文件最多只能读 4000 字符"——那是分页，不是截断。
READ_MAX_CHARS = 4000
# 一次 read 调用内部最多连续拉取的页数（防止把整本资料一次灌进上下文）。
# 用户/Agent 显式要求"读完整份/继续读完"时按页循环拉取，直到内容真正结束；
# 达到这个页数仍未读完时，必须如实返回 has_more=true + cursor，不能谎报读完。
MAX_READ_PAGES_PER_CALL = 8
# Internal complete-read requests (for an explicit "全文/整份/通读" intent) may
# consume several page batches in one atomic tool execution. This remains a
# bounded safety valve; a request that reaches it still returns has_more/cursor.
MAX_READ_PAGES_PER_REQUEST = 128
# ``action=scan`` 的骨架必须覆盖**整份文件**才有意义（"只扫了前 4000 字符"的骨架
# 会让模型以为文件里就这些符号），所以扫描时主动跟游标续读；同时给字符/页数预算
# 兜底，预算耗尽就如实标 partial，不假装扫完了。
MAX_SCAN_CHARS = 200_000
MAX_SCAN_PAGES = 24
# 敏感内容的脱敏类别（与 Electron 的 meta.sensitivity 取值对齐）。
SENSITIVITY_CREDENTIAL = "CREDENTIAL"
SENSITIVITY_KEY_MATERIAL = "KEY_MATERIAL"
SENSITIVITY_PII = "PII"
SENSITIVITY_UNKNOWN = "UNKNOWN"
_REDACTION_MASK = "[REDACTED:{category}]"
# 敏感级别强弱：取最高级别作为本次结果的整体标注。
_SENSITIVITY_RANK = {
    SENSITIVITY_CREDENTIAL: 3,
    SENSITIVITY_KEY_MATERIAL: 3,
    SENSITIVITY_PII: 2,
    SENSITIVITY_UNKNOWN: 1,
}
# 路径 → 敏感级别的兜底判定（Electron 的 meta.sensitive/sensitivity 优先）。
_SENSITIVITY_PATH_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (SENSITIVITY_KEY_MATERIAL, (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", "id_rsa", "id_ed25519")),
    (SENSITIVITY_CREDENTIAL, (
        ".env", ".npmrc", ".netrc", ".pgpass", "credentials", "secrets", "secret",
        "token", "password", "passwd", ".htpasswd", "kubeconfig", ".aws/", ".gcloud/",
    )),
    (SENSITIVITY_PII, ("id_card", "身份证", "passport", "护照", "bank_card", "银行卡", "phone_list")),
)

# 脱敏规则：命中即替换为 [REDACTED:CATEGORY]，保留键名与结构，不删整行。
# 顺序有意义——先处理“键=值”这类带键名的赋值，再处理裸 token/密钥，最后 PII。
_REDACTION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 键值型凭据：PASSWORD=xxx / "api_key": "xxx" / token: xxx / 密码：xxx
    (
        SENSITIVITY_CREDENTIAL,
        re.compile(
            r"(?i)([A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|"
            r"access[_-]?key|private[_-]?key|client[_-]?secret|auth|credential)[A-Za-z0-9_.\-]*"
            r"|密码|口令|密钥|凭证)"
            r"(\s*[:=]\s*)"
            r"(\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|`[^`\n]{1,200}`|[^\s,;|}\]\n]{4,200})"
        ),
    ),
    (
        SENSITIVITY_CREDENTIAL,
        re.compile(
            r"(?i)([A-Za-z0-9_.\-]*(?:密码|口令|密钥|凭证))"
            r"(\s*[:：=]\s*)"
            r"(\"[^\"\n]{1,200}\"|'[^'\n]{1,200}'|[^\s,;|}\]\n]{4,200})"
        ),
    ),
    # PEM / OpenSSH 私钥块
    (
        SENSITIVITY_KEY_MATERIAL,
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]{0,20000}?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    # 带前缀的 token / API key
    (
        SENSITIVITY_CREDENTIAL,
        re.compile(r"\b(?:sk|pk|rk|ghp|gho|ghu|ghs|glpat|xox[baprs]|AKIA|ASIA|AIza)[A-Za-z0-9_\-]{12,}\b"),
    ),
    (
        SENSITIVITY_CREDENTIAL,
        re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}"),
    ),
    # JWT
    (
        SENSITIVITY_CREDENTIAL,
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    ),
    # URL 内嵌凭据 scheme://user:pass@host
    (
        SENSITIVITY_CREDENTIAL,
        re.compile(r"://[^\s:/@]{1,64}:[^\s:/@]{4,128}@"),
    ),
    # PII：邮箱 / 手机号 / 中国身份证号 / 银行卡号
    (SENSITIVITY_PII, re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,255}\.[A-Za-z]{2,10}\b")),
    (SENSITIVITY_PII, re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (SENSITIVITY_PII, re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    (SENSITIVITY_PII, re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
)

_EXT_TO_FORMAT = {
    ".pptx": "pptx", ".ppt": "ppt", ".docx": "docx", ".doc": "doc",
    ".pdf": "pdf", ".xlsx": "xlsx", ".xls": "xls", ".csv": "csv", ".tsv": "tsv",
    ".md": "markdown", ".markdown": "markdown", ".txt": "text", ".log": "text",
    ".json": "json", ".jsonl": "jsonl", ".yaml": "yaml", ".yml": "yaml",
    ".xml": "xml", ".html": "html", ".htm": "html", ".zip": "archive",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".gif": "image", ".bmp": "image", ".tiff": "image", ".tif": "image",
    ".eml": "email", ".ics": "calendar", ".rtf": "rtf",
}

_CURSOR_TTL_SECONDS = 900

# Electron 内部工具候选（按优先级挑选；模型永远看不到这些名字）
_LIST_TOOLS = ("workspace_list", "workspace_catalog")
_SEARCH_TOOLS = ("workspace_search",)
_READ_TOOLS = ("workspace_content_extract", "workspace_read_structured", "workspace_read")
_ROUTE_TOOLS = ("workspace_stat", "workspace_list", "workspace_catalog")


def clean_path(value: Any) -> str:
    """规范化模型给出的工作区相对路径（不接受绝对路径/父目录逃逸）。"""
    raw = str(value or "").strip().replace("\\", "/")
    raw = raw.split("\x00", 1)[0]
    while raw.startswith("./"):
        raw = raw[2:]
    raw = raw.strip("/")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def is_root_path(value: Any) -> bool:
    """判断路径是否等价于工作区根目录（空 / . / ./ / / 都算）。"""
    raw = str(value or "").strip().replace("\\", "/")
    return raw in {"", ".", "./", "/", ".\\"}


def extension_of(path: str) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return ""
    return ("." + name.rsplit(".", 1)[-1]).casefold()


def format_for(path: str) -> str:
    lowered = str(path or "").casefold()
    for ext, fmt in _EXT_TO_FORMAT.items():
        if lowered.endswith(ext):
            return fmt
    ext = extension_of(path)
    if ext in _TEXT_EXTS or ext in _PARSED_EXTS:
        return "text"
    if ext in _UNSUPPORTED_EXTS:
        return "binary"
    return "unknown"


def _is_readable_format(path: str) -> bool:
    ext = extension_of(path)
    if not ext:
        # 无扩展名（Makefile / Dockerfile / LICENSE…）按文本处理
        return True
    return ext in _TEXT_EXTS or ext in _PARSED_EXTS


def has_known_extension(path: str) -> bool:
    """路径是否带一个后端已知的扩展名（用于判断是否需要目录/文件探测）。"""
    ext = extension_of(path)
    return bool(ext) and (ext in _TEXT_EXTS or ext in _PARSED_EXTS or ext in _UNSUPPORTED_EXTS)


def is_sensitive_path(path: str) -> bool:
    """路径是否疑似凭据/密钥文件（用于触发敏感检测流程）。"""
    lowered = str(path or "").casefold()
    return any(marker in lowered for marker in SENSITIVE_PATH_MARKERS)


def sensitivity_of(path: str, *, hint: str = "") -> str:
    """按路径（以及 Electron 给出的 hint）判定敏感类别。"""
    hinted = str(hint or "").strip().upper()
    if hinted in _SENSITIVITY_RANK:
        return hinted
    lowered = str(path or "").casefold()
    for category, markers in _SENSITIVITY_PATH_RULES:
        if any(marker in lowered for marker in markers):
            return category
    if is_sensitive_path(path):
        return SENSITIVITY_UNKNOWN
    return ""


@dataclass(slots=True)
class RedactionResult:
    """一次脱敏的结果：正文、类别、命中数与是否发生过替换。"""

    text: str
    redacted: bool = False
    count: int = 0
    categories: tuple[str, ...] = ()
    sections: list[dict] = field(default_factory=list)

    @property
    def sensitivity(self) -> str:
        if not self.categories:
            return SENSITIVITY_UNKNOWN
        return max(self.categories, key=lambda item: _SENSITIVITY_RANK.get(item, 0))

    def meta(self) -> dict:
        return {
            "redacted": self.redacted,
            "redaction_count": self.count,
            "sensitivity": self.sensitivity if self.redacted else "",
        }


def redact_sensitive_text(text: str) -> RedactionResult:
    """按规则把凭据/密钥/PII 替换为 ``[REDACTED:CATEGORY]``。

    设计取向：**保留键名与结构，只遮蔽值**。这样模型仍然知道"这里有一个数据库
    密码字段"，但拿不到原文；出现次数用于透明化（``redaction_count``）。
    """
    raw = str(text or "")
    if not raw:
        return RedactionResult(text=raw)
    findings: dict[str, int] = {}

    def apply_rule(value: str, rule, category: str) -> str:
        def repl(match: "re.Match[str]") -> str:
            findings[category] = findings.get(category, 0) + 1
            if match.re.groups >= 3:
                # 键值型规则：保留 键名 + 分隔符，遮蔽值。
                body = match.group(3)
                if len(body) >= 2 and body[0] in "\"'`" and body[-1] == body[0]:
                    masked = body[0] + _REDACTION_MASK.format(category=category) + body[-1]
                else:
                    masked = _REDACTION_MASK.format(category=category)
                return f"{match.group(1)}{match.group(2)}{masked}"
            return _REDACTION_MASK.format(category=category)

        return rule.sub(repl, value)

    result = raw
    for category, rule in _REDACTION_RULES:
        try:
            result = apply_rule(result, rule, category)
        except re.error:  # pragma: no cover - 规则写错时不影响读取
            continue
    count = sum(findings.values())
    return RedactionResult(
        text=result,
        redacted=count > 0,
        count=count,
        categories=tuple(sorted(findings)),
    )


def redact_sections(sections: list[dict]) -> RedactionResult:
    """对 read 的正文分片逐段脱敏，返回替换后的分片与汇总统计。"""
    cleaned: list[dict] = []
    findings: dict[str, int] = {}
    total = 0
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        result = redact_sensitive_text(str(section.get("text") or ""))
        total += result.count
        for category in result.categories:
            findings[category] = findings.get(category, 0) + 1
        cleaned.append({**section, "text": result.text})
    return RedactionResult(
        text="",
        redacted=total > 0,
        count=total,
        categories=tuple(sorted(findings)),
        sections=cleaned,
    )


def normalize_ignored_flag(args: dict) -> bool:
    """``include_ignored``（``include_hidden`` 为旧别名）：是否返回被忽略条目。

    默认 False：隐藏项与 node_modules/构建/缓存目录由 Electron 过滤掉，
    只把 ``meta.filtered_ignored`` 的数量报回来，避免目录列表撑爆上下文。
    """
    for key in ("include_ignored", "include_hidden"):
        value = (args or {}).get(key)
        if isinstance(value, bool):
            return value
        if value not in (None, ""):
            return str(value).strip().casefold() in {"1", "true", "yes", "on"}
    return False


def parse_full_read_flag(args: dict) -> bool:
    """read 是否"按页读到内容结束"（默认 True）。

    读取就是读取：默认不受固定页数限制，一次调用内部按页循环拉取，直到内容真正
    结束或达到 ``MAX_READ_PAGES_PER_CALL``。达到页数上限时仍然返回
    ``has_more=true`` + ``cursor``，调用方可以继续，**绝不谎报"已读完"**。
    """
    for key in ("read_full", "full"):
        value = (args or {}).get(key)
        if isinstance(value, bool):
            return value
        if value not in (None, ""):
            return str(value).strip().casefold() in {"1", "true", "yes", "on"}
    return True


def _looks_like_path(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "/" in text or "\\" in text:
        return True
    return extension_of(text) != ""


@dataclass(slots=True)
class NavigatorError(Exception):
    """结构化失败：携带稳定错误码与可选自我修正动作。"""

    code: str
    message: str
    suggested_action: str = ""
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志
        return f"{self.code}: {self.message}"


def _suggestion_for(code: str) -> str:
    return _ERROR_SUGGESTIONS.get(code, "请如实说明失败原因，不要编造文件内容。")


def build_error(
    status: str,
    action: str,
    code: str,
    message: str,
    *,
    summary: str = "",
    suggested_action: str = "",
    data: dict | None = None,
    meta: dict | None = None,
) -> dict:
    """统一的错误信封（``error`` 内带 code/message/suggested_action）。"""
    return {
        "status": status,
        "action": action,
        "summary": summary or message,
        "data": data if data is not None else {},
        "has_more": False,
        "cursor": None,
        "meta": dict(meta or {}),
        "error": {
            "code": code,
            "message": message,
            "suggested_action": suggested_action or _suggestion_for(code),
        },
    }


def clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return max(minimum, min(maximum, number))


class _CursorStore:
    """进程内游标表（TTL，超时即 CURSOR_EXPIRED）。

    游标只承载“同一次 list/search/read 的后续分页”，并且绑定发起它的
    workspace_id；跨工作区复用同一个 cursor 一律判为过期。
    """

    def __init__(self) -> None:
        self._items: dict[str, dict] = {}

    def put(self, state: dict) -> str:
        self.prune()
        cursor = uuid.uuid4().hex
        self._items[cursor] = {"created_at": time.time(), **state}
        return cursor

    def get(self, cursor: str, *, workspace_id: str = "") -> dict | None:
        self.prune()
        state = self._items.get(str(cursor or ""))
        if not state:
            return None
        bound = str(state.get("workspace_id") or "")
        if workspace_id and bound and bound != workspace_id:
            return None
        return state

    def prune(self) -> None:
        now = time.time()
        for key, state in list(self._items.items()):
            if now - float(state.get("created_at") or 0) > _CURSOR_TTL_SECONDS:
                self._items.pop(key, None)

    def clear(self) -> None:
        self._items.clear()


# 全局游标表：service 实例每次调用都会重建，游标必须跨实例存活。
CURSOR_STORE = _CursorStore()


class WorkspaceNavigatorService:
    """一次 ``workspace_navigator`` 调用的编排者（授权 → 路由 → 处理器 → 信封）。"""

    def __init__(
        self,
        *,
        user_id: str,
        workspace_id: str,
        conversation_id: str = "",
        user_role: str = "user",
        device_id: str = "",
        server_name: str = "",
        resolve_route: Callable[[], dict] | None = None,
        list_tools: Callable[[str], Any] | None = None,
        call_tool: Callable[..., Any] | None = None,
        request: str = "",
        timeout_s: float | None = None,
        list_max_entries: int | None = None,
        search_max_results: int | None = None,
    ) -> None:
        self.user_id = str(user_id or "")
        self.workspace_id = str(workspace_id or "").strip()
        self.conversation_id = str(conversation_id or "")
        self.user_role = str(user_role or "user")
        self.device_id = str(device_id or "")
        self._server_name = str(server_name or "")
        self.request = str(request or "")
        self._resolve_route = resolve_route
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._timeout_s = max(1.0, min(MAX_TIMEOUT_SECONDS, float(timeout_s or DEFAULT_TIMEOUT_SECONDS)))
        self._list_max_entries = clamp_int(
            list_max_entries, default=DEFAULT_LIST_MAX_ENTRIES, minimum=1, maximum=MAX_MAX_RESULTS
        )
        self._search_max_results = clamp_int(
            search_max_results, default=DEFAULT_SEARCH_MAX_RESULTS, minimum=1, maximum=MAX_MAX_RESULTS
        )
        self._route_cache: dict | None = None
        self._advertised: list[dict] | None = None

    # ── 对外入口 ──────────────────────────────────────────────

    async def execute(self, action: str, params: dict | None = None) -> dict:
        """执行一次导航动作；任何异常都收敛为错误信封，绝不抛出。"""
        args = dict(params or {})
        # 模型传入的 workspace_id 一律丢弃：授权只认服务端注入值。
        args.pop("workspace_id", None)
        normalized_action = str(action or args.get("action") or "").strip().casefold()
        if normalized_action not in ACTIONS:
            return build_error(
                STATUS_ERROR, normalized_action or ACTION_LIST, INVALID_ACTION,
                f"不支持的动作：{normalized_action or '（空）'}；只支持 list/search/read/scan。",
            )
        try:
            bound = self._require_bound()
            if bound is not None:
                self._observe(normalized_action, args, bound)
                return bound
            if normalized_action == ACTION_LIST:
                payload = await NavigatorListHandler(self).run(args)
            elif normalized_action == ACTION_SEARCH:
                payload = await NavigatorSearchHandler(self).run(args)
            elif normalized_action == ACTION_SCAN:
                payload = await NavigatorScanHandler(self).run(args)
            else:
                payload = await NavigatorReadHandler(self).run(args)
            self._observe(normalized_action, args, payload)
            return payload
        except NavigatorError as exc:
            payload = build_error(
                STATUS_ERROR, normalized_action, exc.code, exc.message,
                suggested_action=exc.suggested_action, data=exc.data,
            )
            self._observe(normalized_action, args, payload)
            return payload
        except asyncio.TimeoutError:
            payload = build_error(
                STATUS_ERROR, normalized_action, SEARCH_TIMEOUT,
                f"工作区 {normalized_action} 超时（{self._timeout_s:.0f}s）。",
            )
            self._observe(normalized_action, args, payload)
            return payload
        except Exception as exc:  # noqa: BLE001 - 统一收敛，不把内部异常抛给模型
            logger.warning(
                "workspace_navigator {} 失败: {}", normalized_action, str(exc)[:200]
            )
            payload = build_error(
                STATUS_ERROR, normalized_action, WORKSPACE_READ_FAILED,
                f"工作区读取失败：{str(exc)[:160]}",
            )
            self._observe(normalized_action, args, payload)
            return payload

    def _observe(self, action: str, args: dict, payload: dict) -> None:
        """审计日志：只记录动作/目标/结果数量/错误码，绝不记录文件正文。"""
        target = ""
        if action == ACTION_SEARCH:
            target = f"query={str(args.get('query') or '')[:120]}"
        elif args.get("path"):
            target = f"path={clean_path(args.get('path'))[:200]}"
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        counts = {
            key: len(data.get(key) or [])
            for key in ("entries", "matches", "sections")
            if isinstance(data.get(key), list)
        }
        logger.info(
            "workspace_navigator action={} {} status={} counts={} has_more={} error={}",
            action,
            target or "path=.",
            str(payload.get("status") or ""),
            counts or {},
            bool(payload.get("has_more")),
            str((payload.get("error") or {}).get("code") or "") or "-",
        )

    # ── 授权与路由 ────────────────────────────────────────────

    def _require_bound(self) -> dict | None:
        if not self.workspace_id:
            return build_error(
                STATUS_ERROR, ACTION_LIST, WORKSPACE_NOT_BOUND,
                "当前会话没有绑定工作区，无法访问本地文件。",
            )
        route = self.route()
        if str(route.get("status_code") or "") == "WORKSPACE_READY" and route.get("server_name"):
            return None
        code = str(route.get("status_code") or WORKSPACE_NOT_REGISTERED)
        if code not in {WORKSPACE_NOT_BOUND, WORKSPACE_NOT_REGISTERED, WORKSPACE_DEVICE_OFFLINE}:
            code = WORKSPACE_NOT_REGISTERED
        return build_error(STATUS_ERROR, ACTION_LIST, code, self._status_message(code))

    def route(self) -> dict:
        if self._route_cache is not None and self._server_name:
            return self._route_cache
        if self._resolve_route is not None:
            result = self._resolve_route() or {}
        else:
            from app.services.workspace_context import resolve_workspace_desktop

            try:
                result = resolve_workspace_desktop(self.user_id, self.workspace_id)
            except Exception as exc:  # noqa: BLE001 - 路由解析失败按未注册处理
                logger.warning("workspace_navigator 解析桌面路由失败: {}", str(exc)[:160])
                result = {"status_code": WORKSPACE_NOT_REGISTERED, "server_name": ""}
        route = dict(result) if isinstance(result, dict) else {}
        if self._server_name and not route.get("server_name"):
            # 调用方已显式给出 server_name（例如从 WorkspaceContext 带入）
            route = {**route, "server_name": self._server_name, "status_code": "WORKSPACE_READY"}
        if not route.get("device_id") and self.device_id:
            route["device_id"] = self.device_id
        self._route_cache = route
        return route

    @property
    def server_name(self) -> str:
        return str(self.route().get("server_name") or "")

    async def advertised_tools(self) -> list[dict]:
        """已连接的 Electron MCP 工具清单（用于选择内部原子能力）。"""
        if self._advertised is not None:
            return self._advertised
        server_name = self.server_name
        if not server_name:
            self._advertised = []
            return self._advertised
        try:
            if self._list_tools is not None:
                listed = await self._list_tools(server_name)
            else:
                from app.agents.mcp.manager import list_tools

                listed = await list_tools(server_name)
        except Exception as exc:  # noqa: BLE001 - 发现失败视为无可用工具
            logger.warning("workspace_navigator 列出桌面工具失败: {}", str(exc)[:160])
            listed = []
        self._advertised = [item for item in (listed or []) if isinstance(item, dict)]
        return self._advertised

    async def pick_tool(self, candidates: tuple[str, ...]) -> str:
        names = {str(item.get("name") or "") for item in await self.advertised_tools()}
        for candidate in candidates:
            if candidate in names:
                return candidate
        return ""

    def resolve_error(self, payload: Any, *, default: str = WORKSPACE_READ_FAILED) -> tuple[str, str] | None:
        """把 Electron 返回的失败信号映射为稳定错误码；成功返回 None。"""
        if not isinstance(payload, dict):
            return None
        status = str(payload.get("status") or "").strip().casefold()
        if status and status not in {"failed", "error", "cancelled", "timeout"}:
            return None
        raw = ""
        for candidate in (
            payload.get("error_code"),
            (payload.get("error") or {}).get("code") if isinstance(payload.get("error"), dict) else None,
            payload.get("error") if isinstance(payload.get("error"), str) else None,
            (payload.get("meta") or {}).get("error_code") if isinstance(payload.get("meta"), dict) else None,
        ):
            if candidate not in (None, ""):
                raw = str(candidate)
                break
        upper = raw.strip().upper()
        if upper in _DIRECTORY_ERROR_CODES:
            return WORKSPACE_PATH_NOT_DIRECTORY, "目标是目录，不能作为文件读取。"
        if upper in _NOT_FOUND_ERROR_CODES:
            return WORKSPACE_PATH_NOT_FOUND, "工作区内没有该路径。"
        if upper in _PASSTHROUGH_ERROR_CODES:
            return upper, self._status_message(upper)
        if upper == SEARCH_TIMEOUT:
            return SEARCH_TIMEOUT, "搜索超时。"
        if status == "timeout":
            return SEARCH_TIMEOUT, "搜索超时。"
        return default, str(payload.get("message") or payload.get("error") or "工作区读取失败。")[:200]

    async def call(self, tool: str, args: dict) -> dict:
        """调用 Electron 原子工具（带超时），并转换为可读失败信息。"""
        if not tool:
            raise NavigatorError(
                WORKSPACE_NOT_REGISTERED, "当前客户端未提供所需的工作区原子能力。"
            )
        server_name = self.server_name
        if not server_name:
            raise NavigatorError(WORKSPACE_NOT_REGISTERED, "没有可路由的桌面连接。")
        timeout = self._timeout_s
        try:
            if self._call_tool is not None:
                result = await asyncio.wait_for(
                    self._call_tool(server_name, tool, dict(args)), timeout=timeout
                )
            else:
                from app.agents.mcp.manager import call_tool

                result = await asyncio.wait_for(
                    call_tool(
                        server_name,
                        tool,
                        dict(args),
                        task_id=self.conversation_id or None,
                        user_id=self.user_id,
                        device_id=str(self.route().get("device_id") or ""),
                        workspace_id=self.workspace_id,
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError as exc:
            raise NavigatorError(
                SEARCH_TIMEOUT, f"{tool} 调用超时（{timeout:.0f}s）。"
            ) from exc
        except NavigatorError:
            raise
        except Exception as exc:  # noqa: BLE001 - MCP 异常按读取失败处理
            logger.warning("workspace_navigator 调用 {} 失败: {}", tool, str(exc)[:160])
            raise NavigatorError(
                WORKSPACE_READ_FAILED, f"{tool} 调用失败：{str(exc)[:160]}"
            ) from exc
        if not isinstance(result, dict):
            raise NavigatorError(WORKSPACE_READ_FAILED, f"{tool} 未返回结构化结果。")
        failure = self.resolve_error(result)
        if failure is not None:
            code, message = failure
            raise NavigatorError(code, message)
        return result

    @staticmethod
    def data_of(payload: Any) -> Any:
        """取统一信封中的 data；没有 data 时回退整个 payload。"""
        if not isinstance(payload, dict):
            return payload
        data = payload.get("data")
        return data if data is not None else payload

    @staticmethod
    def _status_message(code: str) -> str:
        try:
            from app.services.workspace_context import describe_status

            return describe_status(code, workspace_id="")
        except Exception:  # noqa: BLE001 - 文案降级不影响状态码
            return "工作区当前不可用。"

    # ── 信封与游标工具 ────────────────────────────────────────

    def envelope(
        self,
        action: str,
        *,
        status: str = STATUS_OK,
        summary: str = "",
        data: Any = None,
        has_more: bool = False,
        cursor: str | None = None,
        meta: dict | None = None,
    ) -> dict:
        return {
            "status": status,
            "action": action,
            "summary": summary,
            "data": data if data is not None else {},
            "has_more": bool(has_more),
            "cursor": cursor,
            "meta": dict(meta or {}),
            "error": None,
        }

    def base_meta(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "workspace_version": None,
            "server_name": self.server_name,
            "limits": {
                "depth": MAX_DEPTH,
                "max_results": MAX_MAX_RESULTS,
                "max_chars": MAX_MAX_CHARS,
                "timeout_seconds": self._timeout_s,
            },
            "skipped_dirs": list(DEFAULT_SKIP_DIRS),
        }

    def put_cursor(self, action: str, payload: dict) -> str:
        return CURSOR_STORE.put({"action": action, "workspace_id": self.workspace_id, **payload})

    def take_cursor(self, action: str, cursor: str) -> dict:
        state = CURSOR_STORE.get(cursor, workspace_id=self.workspace_id)
        if not state:
            raise NavigatorError(CURSOR_EXPIRED, "该 cursor 已过期或不属于当前工作区。")
        if str(state.get("action") or "") != action:
            raise NavigatorError(
                CURSOR_EXPIRED,
                f"该 cursor 属于 {state.get('action')} 动作，不能用于 {action}。",
            )
        return state


class NavigatorListHandler:
    """``action=list``：目录枚举（元数据，不读正文）。"""

    def __init__(self, service: WorkspaceNavigatorService) -> None:
        self.service = service

    async def run(self, args: dict) -> dict:
        service = self.service
        raw_path = args.get("path")
        cursor = str(args.get("cursor") or "").strip()
        state: dict = service.take_cursor(ACTION_LIST, cursor) if cursor else {}
        # 续页的 path / depth / 过滤语义都以首次请求为准（调用方只需回传 cursor），
        # 这样也保证 Electron 侧绑定的 include_ignored 不会被换掉。
        if state and "path" in state:
            path = str(state.get("path") or "")
        else:
            path = "" if is_root_path(raw_path) else clean_path(raw_path)
            if not is_root_path(raw_path) and not path:
                return build_error(
                    STATUS_ERROR, ACTION_LIST, INVALID_PARAMS,
                    "path 必须是工作区内的相对路径。",
                    suggested_action="去掉绝对路径或 ../，改用相对路径重新 list。",
                )
        depth = clamp_int(
            state.get("depth") if state else args.get("depth"),
            default=DEFAULT_DEPTH,
            minimum=1,
            maximum=MAX_DEPTH,
        )
        if is_trash_path(path):
            # 回收站不是普通目录：不允许显式列它（内容只能经恢复/清理接口处理）。
            return build_error(
                STATUS_ERROR, ACTION_LIST, TRASH_PATH_FORBIDDEN,
                f"{path} 位于回收站 {TRASH_DIRNAME}/ 内，不能用 list 浏览。",
                suggested_action="普通浏览请换一个目录；回收站内容请用恢复/清理接口。",
            )
        max_results = clamp_int(
            args.get("max_results"),
            default=service._list_max_entries,
            minimum=1,
            maximum=MAX_MAX_RESULTS,
        )
        include_ignored = (
            bool(state.get("include_ignored"))
            if state and "include_ignored" in state
            else normalize_ignored_flag(args)
        )
        electron_cursor = str(state.get("electron_cursor") or "") if state else ""
        if state and not args.get("max_results"):
            # 续页默认沿用首次请求的页大小，保持分页边界稳定。
            max_results = clamp_int(
                state.get("page_size"),
                default=service._list_max_entries,
                minimum=1,
                maximum=MAX_MAX_RESULTS,
            )

        page, electron_cursor, has_more, meta_extra = await fetch_page(
            service,
            action=ACTION_LIST,
            electron_cursor=electron_cursor,
            fetch=lambda ec: self._fetch_entries(
                path,
                depth=depth,
                max_entries=max(
                    max_results,
                    min(MAX_ELECTRON_LIST_ENTRIES, service._list_max_entries),
                ),
                include_ignored=include_ignored,
                cursor=ec,
            ),
            data_key="entries",
            offset=int(state.get("offset") or 0),
            size=max_results,
        )

        next_cursor = None
        if has_more:
            next_cursor = service.put_cursor(ACTION_LIST, {
                "path": path,
                "depth": depth,
                "page_size": max_results,
                "include_ignored": include_ignored,
                "electron_cursor": electron_cursor,
                "offset": (0 if electron_cursor else int(state.get("offset") or 0) + len(page)),
            })
        meta = {
            **service.base_meta(),
            "path": path or ".",
            "depth": depth,
            "include_ignored": include_ignored,
            "returned": len(page),
            **meta_extra,
        }
        if has_more:
            summary = f"目录 {path or '.'} 本页返回 {len(page)} 个条目，还有更多（可用 cursor 继续）"
        else:
            summary = f"目录 {path or '.'} 返回 {len(page)} 个条目"
            if meta.get("total_entries") is not None:
                summary = f"目录 {path or '.'} 共 {meta['total_entries']} 个条目（本页 {len(page)} 个）"
            elif meta.get("filtered_ignored"):
                summary += f"（已过滤 {meta['filtered_ignored']} 个被忽略条目）"
        return service.envelope(
            ACTION_LIST,
            status=STATUS_PARTIAL if has_more else (STATUS_OK if page else STATUS_EMPTY),
            summary=summary,
            data={"entries": page},
            has_more=has_more,
            cursor=next_cursor,
            meta=meta,
        )

    async def _fetch_entries(
        self,
        path: str,
        *,
        depth: int,
        max_entries: int,
        include_ignored: bool,
        cursor: str,
    ) -> dict:
        service = self.service
        tool = await service.pick_tool(_LIST_TOOLS)
        if not tool:
            raise NavigatorError(
                WORKSPACE_NOT_REGISTERED, "当前客户端未提供目录枚举能力（workspace_list）。"
            )
        if tool == "workspace_catalog":
            # workspace_catalog 是概览（无分页 cursor）：只做字段级回退。
            payload = await service.call(tool, {"workspace_id": service.workspace_id})
            data = service.data_of(payload)
            return {"items": normalize_entries(data), "has_more": False, "cursor": ""}
        call_args: dict = {
            "workspace_id": service.workspace_id,
            "path": path,
            "depth": depth,
            # Electron 优先读 max_entries，max_results 作为别名。
            "max_entries": max_entries,
            "max_results": max_entries,
            "include_ignored": include_ignored,
        }
        if cursor:
            # Electron 的 nav1 cursor 是自包含 base64：后端只做托管，续页原样回传，
            # 不包装、不重编码。
            call_args["cursor"] = cursor
        payload = await service.call(tool, call_args)
        data = service.data_of(payload)
        items = normalize_entries(data)
        # 回收站默认不出现在 list 结果里：.lumi_trash 是删除内容的落点，
        # 普通浏览看到它只会诱导模型去读"已经删掉的东西"。
        hidden = [item for item in items if is_trash_path(str(item.get("path") or ""))]
        if hidden:
            items = [item for item in items if not is_trash_path(str(item.get("path") or ""))]
        meta = payload_meta(payload)
        if hidden:
            meta = {**meta, "trash_hidden": len(hidden)}
        return {
            "items": items,
            "has_more": payload_has_more(payload),
            "cursor": payload_cursor(payload),
            "meta": meta,
        }


class NavigatorSearchHandler:
    """``action=search``：文件名 / 内容检索（只返回命中位置与少量上下文）。"""

    def __init__(self, service: WorkspaceNavigatorService) -> None:
        self.service = service

    async def run(self, args: dict) -> dict:
        service = self.service
        cursor = str(args.get("cursor") or "").strip()
        # 续页的 query / mode / 范围都以首次请求为准：调用方只需回传 cursor。
        state: dict = service.take_cursor(ACTION_SEARCH, cursor) if cursor else {}
        query = str(state.get("query") or args.get("query") or "").strip()
        if not query:
            return build_error(
                STATUS_ERROR, ACTION_SEARCH, INVALID_PARAMS,
                "search 必须提供 query。",
                suggested_action="补充 query（关键词或文件名）后重试。",
            )
        mode = str(state.get("search_mode") or args.get("search_mode") or SEARCH_MODE_AUTO).strip().casefold()
        if mode not in SEARCH_MODES:
            return build_error(
                STATUS_ERROR, ACTION_SEARCH, INVALID_PARAMS,
                f"search_mode 只能是 {', '.join(SEARCH_MODES)}。",
            )
        search_path = str(
            state.get("search_path")
            if state.get("search_path") is not None
            else clean_path(args.get("search_path") or args.get("path"))
        )
        if is_trash_path(search_path):
            # 回收站内容不参与检索：否则"已删除"的文件会被 search 重新带回上下文。
            return build_error(
                STATUS_ERROR, ACTION_SEARCH, TRASH_PATH_FORBIDDEN,
                f"检索范围 {search_path} 位于回收站 {TRASH_DIRNAME}/ 内。",
                suggested_action="换一个检索范围；回收站内容请用恢复/清理接口处理。",
            )
        max_results = clamp_int(
            args.get("max_results"),
            default=service._search_max_results,
            minimum=1,
            maximum=MAX_MAX_RESULTS,
        )
        electron_cursor = ""
        if state:
            electron_cursor = str(state.get("electron_cursor") or "")
            if not args.get("max_results"):
                # 续页默认沿用首次请求的页大小，否则分页边界会漂移。
                max_results = clamp_int(
                    state.get("page_size"),
                    default=service._search_max_results,
                    minimum=1,
                    maximum=MAX_MAX_RESULTS,
                )

        page, electron_cursor, has_more, meta_extra = await fetch_page(
            service,
            action=ACTION_SEARCH,
            electron_cursor=electron_cursor,
            fetch=lambda ec: self._search(
                query,
                mode=mode,
                search_path=search_path,
                max_results=max(
                    max_results,
                    min(MAX_ELECTRON_SEARCH_RESULTS, service._search_max_results),
                ),
                cursor=ec,
            ),
            data_key="matches",
            offset=int(state.get("offset") or 0),
            size=max_results,
        )
        # 命中结果里落入回收站的条目同样要过滤（客户端可能把 .lumi_trash 也检索出来）。
        trash_hits = [item for item in page if is_trash_path(str(item.get("path") or ""))]
        if trash_hits:
            page = [item for item in page if not is_trash_path(str(item.get("path") or ""))]

        next_cursor = None
        if has_more:
            next_cursor = service.put_cursor(ACTION_SEARCH, {
                "query": query,
                "search_mode": mode,
                "search_path": search_path,
                "page_size": max_results,
                "electron_cursor": electron_cursor,
                "offset": (0 if electron_cursor else int(state.get("offset") or 0) + len(page)),
            })
        meta = {
            **service.base_meta(),
            "query": query,
            "search_mode": mode,
            "search_path": search_path or ".",
            "returned": len(page),
            **({"trash_hidden": len(trash_hits)} if trash_hits else {}),
            **meta_extra,
        }
        if any(item.get("sensitive") for item in page):
            meta["sensitive"] = True
        redacted_count = sum(int(item.get("redaction_count") or 0) for item in page)
        if redacted_count:
            meta["redacted"] = True
            meta["redaction_count"] = redacted_count
        summary = f"「{query}」命中 {len(page)} 处"
        if search_path:
            summary += f"（范围 {search_path}）"
        if redacted_count:
            summary += f"；已自动脱敏 {redacted_count} 处敏感片段"
        elif meta.get("sensitive"):
            summary += "；结果含凭据类文件，注意不要外传"
        if has_more:
            summary += "，还有更多（可用 cursor 继续）"
        return service.envelope(
            ACTION_SEARCH,
            status=STATUS_PARTIAL if has_more else (STATUS_OK if page else STATUS_EMPTY),
            summary=summary,
            data={"matches": page},
            has_more=has_more,
            cursor=next_cursor,
            meta=meta,
        )

    async def _search(
        self,
        query: str,
        *,
        mode: str,
        search_path: str,
        max_results: int | None = None,
        cursor: str = "",
    ) -> dict:
        service = self.service
        tool = await service.pick_tool(_SEARCH_TOOLS)
        if not tool:
            raise NavigatorError(
                WORKSPACE_NOT_REGISTERED, "当前客户端未提供搜索能力（workspace_search）。"
            )
        limit = max_results or max(
            service._search_max_results, min(MAX_ELECTRON_SEARCH_RESULTS, service._search_max_results)
        )
        # query 原样透传：Electron 的 tokenizer 把 | * [ ( ^ $ . 当分隔符并做
        # 子串/中文 2-gram 命中（等价 OR，按 score 排序），后端不再拆词。
        # auto 交给 Electron 自己做“先文件名/路径、再内容”的组合检索。
        return await self._call_search(tool, query, search_path, mode, limit, cursor)

    async def _call_search(
        self,
        tool: str,
        query: str,
        search_path: str,
        search_mode: str,
        max_results: int,
        cursor: str = "",
    ) -> dict:
        service = self.service
        args: dict = {
            "workspace_id": service.workspace_id,
            "query": query,
            "max_results": max_results,
        }
        if search_path:
            args["search_path"] = search_path
        if search_mode and search_mode != SEARCH_MODE_AUTO:
            args["search_mode"] = search_mode
        if cursor:
            # Electron 的 nav1 cursor 原样回传（自包含 base64）。
            args["cursor"] = cursor
        payload = await service.call(tool, args)
        data = service.data_of(payload)
        return {
            "items": normalize_matches(data),
            "has_more": payload_has_more(payload),
            "cursor": payload_cursor(payload),
            "meta": payload_meta(payload),
        }


class NavigatorReadHandler:
    """``action=read``：单文件原子读取（复用 WorkspaceReader 内部解析器）。"""

    def __init__(self, service: WorkspaceNavigatorService) -> None:
        self.service = service

    async def run(self, args: dict) -> dict:
        service = self.service
        raw_path = args.get("path")
        path = clean_path(raw_path)
        if not path:
            return build_error(
                STATUS_ERROR, ACTION_READ, INVALID_PARAMS,
                "read 必须提供单个文件的 path。",
                suggested_action="先用 action=list 或 search 定位文件，再用相对路径 read。",
            )
        cursor = str(args.get("cursor") or "").strip()
        if is_trash_path(path):
            # 回收站只能通过 restore/purge 接口访问：读取一律拒止（含索引文件），
            # 否则"删掉的东西"会以另一种方式重新出现在模型上下文里。
            return build_error(
                STATUS_ERROR, ACTION_READ, TRASH_PATH_FORBIDDEN,
                f"{path} 位于回收站 {TRASH_DIRNAME}/ 内，不能用 read 访问。",
                suggested_action="如需恢复请用回收站恢复接口；普通读取请换一个路径。",
                data={"path": path},
                meta={**service.base_meta(), "path": path},
            )
        max_chars = clamp_int(
            args.get("max_chars"),
            default=READ_MAX_CHARS,
            minimum=MIN_MAX_CHARS,
            maximum=MAX_MAX_CHARS,
        )
        # 每页的返回粒度（**不是**这个文件能读多少的上限）：4000 字符/页，
        # 文件更长就按页继续，直到内容真正结束。
        max_chars = min(max_chars, READ_MAX_CHARS)
        full_read = parse_full_read_flag(args)
        read_to_end = bool(args.get("read_to_end"))
        if cursor:
            # 续读：path 与 cursor 一并给出（Electron 也支持仅 cursor 兜底）。
            # 续读的每一页都会重新脱敏，敏感原文不会借 cursor 继续暴露。
            # 续读仍然是"按页"的：给一页 + 下一页 cursor，除非显式要求读到结束。
            if not full_read:
                payload = await self._read_via_reader(
                    request="", path=path, cursor=cursor, max_chars=max_chars
                )
                return self._from_reader(payload, path=path, cursor_in=cursor)
            payload = await self._read_until_end(
                path=path, cursor=cursor, request="", max_chars=max_chars,
                max_pages=MAX_READ_PAGES_PER_REQUEST if read_to_end else MAX_READ_PAGES_PER_CALL,
            )
            return self._from_reader(
                payload, path=path, cursor_in=cursor, full_read=True
            )

        if not _is_readable_format(path):
            return build_error(
                STATUS_ERROR, ACTION_READ, WORKSPACE_UNSUPPORTED_FORMAT,
                f"不支持的正文格式：{extension_of(path) or '（无扩展名）'}。",
                data={"path": path, "format": format_for(path)},
                meta={**service.base_meta(), "path": path},
            )

        # 带已知扩展名的路径（文件）与无扩展名的路径（可能是目录）都可以先探测：
        # 探测同时给出“目录 / 文件 / 不存在”三种答案，比读失败后再猜更准确。
        kind, probe_code = await self._path_kind(path)
        if kind == "directory" or probe_code == WORKSPACE_PATH_NOT_DIRECTORY:
            return build_error(
                STATUS_ERROR, ACTION_READ, WORKSPACE_PATH_NOT_DIRECTORY,
                f"{path} 是目录，不能作为文件读取。",
                suggested_action="list",
                data={"path": path, "kind": "directory"},
                meta={**service.base_meta(), "path": path},
            )

        # 路径不存在：不进入内容提取，直接给出可自我修正的错误（建议先 list/search）。
        if kind == "missing":
            return build_error(
                STATUS_ERROR, ACTION_READ, WORKSPACE_PATH_NOT_FOUND,
                f"工作区内没有 {path}。",
                data={"path": path, "sections": []},
                meta={**service.base_meta(), "path": path},
            )

        request = str(args.get("request") or service.request or f"读取文件 {path}")
        # 行区间精读：给了 start_line/end_line 就**只取这一段**（配合 scan 返回的骨架行号），
        # 而不是从头顺序读到目标位置。切片在服务端做，语义稳定、可测。
        want_start = clamp_int(args.get("start_line"), default=0, minimum=0, maximum=10_000_000)
        want_end = clamp_int(args.get("end_line"), default=0, minimum=0, maximum=10_000_000)
        if want_start or want_end:
            window = await self._read_line_window(
                path=path, request=request, start_line=want_start, end_line=want_end
            )
            if window is not None:
                return window
        if full_read:
            payload = await self._read_until_end(
                path=path, cursor="", request=request, max_chars=max_chars,
                max_pages=MAX_READ_PAGES_PER_REQUEST if read_to_end else MAX_READ_PAGES_PER_CALL,
            )
        else:
            payload = await self._read_via_reader(
                request=request, path=path, cursor="", max_chars=max_chars
            )
        # 元数据探测只用于提前拒绝对目录的读取；真正的裁决仍看读取结果：
        # 客户端把目录当文件读时同样要给出 WORKSPACE_PATH_NOT_DIRECTORY，
        # 而不是让模型看到一条泛化的读取失败。
        if str(payload.get("status") or "") == "failed" and not payload.get("content"):
            code = str((payload.get("meta") or {}).get("error_code") or "").upper()
            if code in _DIRECTORY_ERROR_CODES:
                return build_error(
                    STATUS_ERROR, ACTION_READ, WORKSPACE_PATH_NOT_DIRECTORY,
                    f"{path} 是目录，不能作为文件读取。",
                    suggested_action="list",
                    data={"path": path, "kind": "directory"},
                    meta={**service.base_meta(), "path": path},
                )
        return self._from_reader(payload, path=path, cursor_in="", full_read=full_read)

    async def _read_line_window(
        self, *, path: str, request: str, start_line: int, end_line: int
    ) -> dict | None:
        """按行区间读取（返回 None 表示应回退到常规按页读取）。

        先取整篇正文（客户端解析器），再在服务端切出 ``start_line..end_line``。
        这样"读到某个函数体"是一次调用，而不是从第 1 行翻页翻过去。
        """
        service = self.service
        from app.services.code_structure import slice_lines

        payload = await self._read_via_reader(
            request=request, path=path, cursor="", max_chars=READ_MAX_CHARS
        )
        reader_status = str(payload.get("status") or "")
        sections = [item for item in (payload.get("content") or []) if isinstance(item, dict)]
        if reader_status == "failed" and not sections:
            return None  # 让常规路径给出统一错误
        full_text = "\n".join(str(item.get("text") or "") for item in sections)
        if not full_text:
            return None
        window = slice_lines(
            full_text, start_line=start_line or 1, end_line=end_line or 0
        )
        reader_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        if not window.get("ok"):
            return build_error(
                STATUS_ERROR, ACTION_READ, INVALID_PARAMS,
                str(window.get("reason") or "行区间不合法"),
                data={"path": path, "total_lines": window.get("total_lines")},
                meta={**service.base_meta(), "path": path},
            )
        detection = sensitivity_of(path, hint=str(reader_meta.get("sensitivity") or ""))
        item = {
            "source": path,
            "location": f"第 {window['start_line']}-{window['end_line']} 行",
            "title": f"{path}（行区间）",
            "text": str(window.get("text") or ""),
        }
        redaction = redact_sections([item])
        if redaction.redacted:
            item = redaction.sections[0] if redaction.sections else item
        meta = {
            **service.base_meta(),
            "path": path,
            "format": reader_meta.get("format") or format_for(path),
            "parser": reader_meta.get("parser"),
            "sections_returned": 1,
            "char_count": len(str(item.get("text") or "")),
            "total_lines": int(window.get("total_lines") or 0),
            "start_line": int(window.get("start_line") or 0),
            "end_line": int(window.get("end_line") or 0),
            "line_window": True,
            "truncated": bool(window.get("truncated")),
            "sensitive": bool(detection) or redaction.redacted,
            "sensitivity": redaction.sensitivity if redaction.redacted else (detection or ""),
            "redacted": redaction.redacted,
            "redaction_count": redaction.count,
            "workspace_version": reader_meta.get("workspace_version"),
            # 行区间读取仍然基于**整篇原文**：因此可以给出整文件版本，供 edit/write
            # 作为 expected_revision 使用（"先 scan 骨架 → 再读区间 → 再编辑"闭环）。
            "revision": RevisionRules.for_file(full_text),
        }
        summary = (
            f"已读取 {path} 第 {window['start_line']}-{window['end_line']} 行"
            f"（共 {window['total_lines']} 行；revision {meta['revision']}）"
        )
        if window.get("truncated"):
            summary += "；该区间之后还有内容，需要时用新的 start_line 继续"
        if redaction.redacted:
            summary += f"；已自动脱敏 {redaction.count} 处敏感内容"
        return service.envelope(
            ACTION_READ,
            status=STATUS_PARTIAL if window.get("truncated") else STATUS_OK,
            summary=summary,
            data={"path": path, "sections": [item]},
            meta=meta,
        )

    async def _read_until_end(
        self, *, path: str, cursor: str, request: str, max_chars: int,
        max_pages: int = MAX_READ_PAGES_PER_CALL,
    ) -> dict:
        """按页连续读取，直到内容结束或达到单次页数上限。

        关键语义（与产品要求一致）：

        * **单次返回粒度**（每页 4000 字符）与**文件可读取总量**彻底分开：这里不再
          因为固定页数提前结束，而是按页拉取直到解析器说"没有更多"；
        * 达到 ``MAX_READ_PAGES_PER_CALL`` 仍未读完时，返回 ``partial`` +
          ``has_more=true`` + 可继续的 ``cursor``，并把 ``page_budget_exhausted``
          写进 meta —— 让调用方知道是"本次预算用完"，而不是"文件读完了"；
        * 页与页之间按 ``(source, location)`` 去重，避免分页边界重复灌入。
        """
        sections: list[dict] = []
        seen: set[tuple[str, str]] = set()
        pages = 0
        next_cursor = cursor
        has_more = False
        summary = ""
        reader_meta: dict = {}
        first_status = "success"
        page_limit = max(1, int(max_pages or MAX_READ_PAGES_PER_CALL))
        while pages < page_limit:
            payload = await self._read_via_reader(
                request=request if pages == 0 else "",
                path=path,
                cursor=next_cursor,
                max_chars=max_chars,
            )
            status = str(payload.get("status") or "")
            if status == "failed" and not payload.get("content"):
                if pages == 0:
                    return payload
                break
            first_status = status or first_status
            summary = str(payload.get("summary") or summary)
            if isinstance(payload.get("meta"), dict):
                reader_meta = {**reader_meta, **payload["meta"]}
            for item in payload.get("content") or []:
                if not isinstance(item, dict):
                    continue
                key = (str(item.get("source") or ""), str(item.get("location") or ""))
                if key in seen:
                    continue
                seen.add(key)
                sections.append(item)
            pages += 1
            next_cursor = str(payload.get("cursor") or "")
            has_more = bool(payload.get("has_more"))
            if not has_more or not next_cursor:
                has_more = False
                break
        aggregated = {
            "status": "partial" if has_more else ("success" if sections else first_status),
            "summary": summary,
            "content": sections,
            "has_more": has_more,
            "cursor": next_cursor if has_more else None,
            "meta": {
                **reader_meta,
                "pages_read": pages,
                "page_budget_exhausted": bool(has_more and pages >= page_limit),
                "page_limit": page_limit,
            },
        }
        return aggregated

    async def _path_kind(self, path: str) -> tuple[str, str]:
        """用 Electron 的元数据能力判断 path 是文件/目录/缺失（best effort）。

        返回 ``(kind, error_code)``；``kind`` 为 file/directory/missing/unknown，
        ``error_code`` 保留客户端给出的判定信号（例如 EISDIR），供调用方决定
        是否直接给出 ``WORKSPACE_PATH_NOT_DIRECTORY``。
        """
        service = self.service
        tool = await service.pick_tool(_ROUTE_TOOLS)
        if not tool:
            return "unknown", ""
        try:
            if tool == "workspace_stat":
                payload = await service.call(
                    tool, {"workspace_id": service.workspace_id, "path": path}
                )
            elif tool == "workspace_list":
                payload = await service.call(
                    tool, {"workspace_id": service.workspace_id, "path": path, "depth": 1}
                )
            else:
                payload = await service.call(tool, {"workspace_id": service.workspace_id})
        except NavigatorError as exc:
            if exc.code in {WORKSPACE_PATH_NOT_DIRECTORY, WORKSPACE_PATH_NOT_FOUND}:
                return "unknown", exc.code
            return "unknown", ""
        data = service.data_of(payload)
        target = path.casefold()
        if isinstance(data, dict):
            # workspace_stat 的返回是"单个对象"（kind/size/mtime/type），不是条目
            # 列表；必须按对象形态处理，否则会把成功结果误判为路径不存在。
            stat_kind = _stat_kind(data)
            if stat_kind:
                return stat_kind, ""
            entries = normalize_entries(data)
            for item in entries:
                if str(item.get("path") or "").casefold() == target:
                    return ("directory" if item.get("kind") == "directory" else "file"), ""
            if not entries:
                return "missing", ""
            return "unknown", ""
        if isinstance(data, list):
            entries = normalize_entries({"entries": data})
            for item in entries:
                if str(item.get("path") or "").casefold() == target:
                    return ("directory" if item.get("kind") == "directory" else "file"), ""
            return "unknown", ""
        return "unknown", ""

    async def _read_via_reader(
        self, *, request: str, path: str, cursor: str, max_chars: int
    ) -> dict:
        service = self.service
        from app.services.workspace_reader import WorkspaceReader

        reader = WorkspaceReader(
            user_id=service.user_id,
            user_role=service.user_role,
            workspace_id=service.workspace_id,
            conversation_id=service.conversation_id,
        )
        return await reader.read(request, path=path, cursor=cursor, max_chars=max_chars)

    def _from_reader(
        self, payload: dict, *, path: str, cursor_in: str, full_read: bool = False
    ) -> dict:
        service = self.service
        content = [item for item in (payload.get("content") or []) if isinstance(item, dict)]
        error_code = str((payload.get("meta") or {}).get("error_code") or "")
        reader_status = str(payload.get("status") or "")
        if reader_status == "failed" and not content:
            code = error_code or WORKSPACE_READ_FAILED
            if code in _DIRECTORY_ERROR_CODES:
                code = WORKSPACE_PATH_NOT_DIRECTORY
            elif code in _NOT_FOUND_ERROR_CODES:
                code = WORKSPACE_PATH_NOT_FOUND
            suggestion = "list" if code == WORKSPACE_PATH_NOT_DIRECTORY else ""
            return build_error(
                STATUS_ERROR, ACTION_READ, code,
                str(payload.get("summary") or "读取失败。"),
                suggested_action=suggestion,
                data={"path": path, "sections": []},
                meta={**service.base_meta(), "path": path},
            )
        sections = normalize_sections(content)
        reader_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        format_value = reader_meta.get("format") or format_for(path)
        # 版本：基于**整篇原文**（脱敏前的 sections）。只有"整篇读完"才给出，
        # 分页中间页算出来的哈希没有意义（会诱导调用方拿半截内容的版本去写）。
        raw_text = "\n".join(str(item.get("text") or "") for item in sections)
        content_revision = RevisionRules.for_file(raw_text)
        # 敏感检测 → 自动脱敏：普通读取免确认，但凭据/密钥/PII 一律先脱敏，
        # 原文不进入模型上下文（不依赖文件名是否可疑）。
        detection = sensitivity_of(path, hint=str(reader_meta.get("sensitivity") or ""))
        redaction = redact_sections(sections)
        if redaction.redacted:
            sections = redaction.sections
        sensitive = bool(detection) or bool(reader_meta.get("sensitive")) or redaction.redacted
        meta = {
            **service.base_meta(),
            "path": path,
            "format": format_value,
            "parser": reader_meta.get("parser"),
            "sections_returned": len(sections),
            "char_count": sum(len(str(item.get("text") or "")) for item in sections),
            "continued_from_cursor": bool(cursor_in),
            "workspace_version": reader_meta.get("workspace_version"),
            "sensitive": sensitive,
            "sensitivity": redaction.sensitivity if redaction.redacted else (detection or ""),
            "redacted": redaction.redacted,
            "redaction_count": redaction.count,
            # 语义澄清：这是"每页返回的粒度"，不是"该文件最多能读多少"。
            # 文件更长时 has_more=true + cursor 可以一直继续，直到读完整份。
            "page_chars": READ_MAX_CHARS,
            "max_pages_per_call": MAX_READ_PAGES_PER_CALL,
            "page_limit": int(reader_meta.get("page_limit") or MAX_READ_PAGES_PER_CALL),
            "pages_read": int(reader_meta.get("pages_read") or 1),
            "page_budget_exhausted": bool(reader_meta.get("page_budget_exhausted")),
            "read_full_requested": bool(full_read),
        }
        has_more = bool(payload.get("has_more"))
        if not has_more and sections:
            # 编辑/写入的版本前置条件：这里给出**当前版本**，调用方原样作为
            # expected_revision 传回即可（同一个算法，见 workspace_revision.py）。
            meta["revision"] = content_revision
            meta["expected_revision_hint"] = (
                "写入/编辑该文件时把 revision 作为 expected_revision 传回"
            )
        summary = str(payload.get("summary") or f"已读取 {path}")
        if has_more:
            summary += (
                f"；本次已按页读取 {meta['pages_read']} 页"
                f"（{meta['page_chars']} 字符/页），文件尚未读完，"
                "可用 cursor 继续读取直到结束"
            )
        else:
            summary += "；已读到文件结尾"
            if meta.get("revision"):
                summary += f"（revision {meta['revision']}，写入/编辑时原样作为 expected_revision）"
        if redaction.redacted:
            summary += (
                f"；已自动脱敏 {redaction.count} 处敏感内容"
                f"（{meta['sensitivity']}），原文未进入上下文，需要原文请让用户单独授权"
            )
        elif sensitive:
            summary += "；该文件疑似凭据/密钥，注意不要外传原文"
        if not sections and not has_more:
            return service.envelope(
                ACTION_READ,
                status=STATUS_EMPTY,
                summary=str(payload.get("summary") or f"{path} 没有可读正文。"),
                data={"path": path, "sections": []},
                meta=meta,
            )
        return service.envelope(
            ACTION_READ,
            status=STATUS_PARTIAL if has_more else STATUS_OK,
            summary=summary,
            data={"path": path, "sections": sections},
            has_more=has_more,
            cursor=str(payload.get("cursor") or "") or None,
            meta=meta,
        )


class NavigatorScanHandler:
    """``action=scan``：**代码骨架**扫描（类/函数/导入 + 行号区间）。

    与 ``read`` 的分工：``read`` 给正文（按页），``scan`` 给**结构**——文件体不返回，
    因此几百行的代码文件也只占很小上下文。拿到 ``line/end_line`` 后可用
    ``action=read&start_line=&end_line=`` 精确读某一段，不必从头顺序读。

    解析完全在服务端做（``app/services/code_structure.py``，纯函数）：即使客户端只提供
    原始正文，也能给出骨架；Python 走 ``ast``，其余语言走保守正则。
    """

    def __init__(self, service: WorkspaceNavigatorService) -> None:
        self.service = service

    async def run(self, args: dict) -> dict:
        service = self.service
        path = clean_path(args.get("path"))
        if not path:
            return build_error(
                STATUS_ERROR, ACTION_SCAN, INVALID_PARAMS,
                "scan 必须提供单个文件的 path。",
                suggested_action="先用 action=list 或 search 定位文件，再对代码文件 scan。",
            )
        if not _is_readable_format(path):
            return build_error(
                STATUS_ERROR, ACTION_SCAN, WORKSPACE_UNSUPPORTED_FORMAT,
                f"不支持扫描的格式：{extension_of(path) or '（无扩展名）'}。",
                data={"path": path, "format": format_for(path)},
                meta={**service.base_meta(), "path": path},
            )
        if is_trash_path(path):
            # 回收站内容不参与结构扫描（与 list/read/search 同一策略）。
            return build_error(
                STATUS_ERROR, ACTION_SCAN, TRASH_PATH_FORBIDDEN,
                f"{path} 位于回收站 {TRASH_DIRNAME}/ 内，不能扫描。",
                suggested_action="换一个路径；回收站内容请用恢复/清理接口处理。",
                data={"path": path, "symbols": []},
                meta={**service.base_meta(), "path": path},
            )
        kind = str(args.get("kind") or "").strip().casefold()
        if kind and kind not in {"class", "function", "method", "import"}:
            return build_error(
                STATUS_ERROR, ACTION_SCAN, INVALID_PARAMS,
                f"kind 只能是 class/function/method/import，收到：{kind}",
            )
        max_symbols = clamp_int(args.get("max_symbols"), default=200, minimum=1, maximum=400)
        want_start = clamp_int(args.get("start_line"), default=0, minimum=0, maximum=10_000_000)
        want_end = clamp_int(args.get("end_line"), default=0, minimum=0, maximum=10_000_000)

        # 取正文：客户端解析器给整篇原文，**切片在服务端做**（纯函数、可测、不依赖客户端）。
        payload = await self._fetch(path=path)
        reader_status = str(payload.get("status") or "")
        sections = [item for item in (payload.get("content") or []) if isinstance(item, dict)]
        # ``WorkspaceReader`` 只在**完全没有正文**时才 failed/empty（见 workspace_reader）：
        # 那就是失败，不能拿空正文扫出一个"空骨架"冒充成功。
        if reader_status in {"failed", "empty"}:
            code = str((payload.get("meta") or {}).get("error_code") or WORKSPACE_READ_FAILED)
            if code in _DIRECTORY_ERROR_CODES:
                code = WORKSPACE_PATH_NOT_DIRECTORY
            elif code in _NOT_FOUND_ERROR_CODES:
                code = WORKSPACE_PATH_NOT_FOUND
            elif reader_status == "empty":
                code = WORKSPACE_PATH_NOT_FOUND
            return build_error(
                STATUS_ERROR, ACTION_SCAN, code,
                str(payload.get("summary") or "扫描失败。"),
                data={"path": path, "symbols": []},
                meta={**service.base_meta(), "path": path},
            )
        full_text = "\n".join(str(item.get("text") or "") for item in sections)
        reader_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        from app.services.code_structure import scan_text, slice_lines

        text = full_text
        sliced = False
        total_lines = len(full_text.splitlines())
        if want_start or want_end:
            window = slice_lines(
                full_text,
                start_line=want_start or 1,
                end_line=want_end or (want_start + 2000),
            )
            text = str(window.get("text") or "")
            sliced = True
        skeleton = scan_text(text, path=path, max_symbols=max_symbols)
        # 骨架不完整的两种情形都要标出来：只扫了窗口（sliced）、或文件太长没抓完
        # （预算耗尽）。否则模型会把局部骨架当成全文。
        budget_exhausted = bool(reader_meta.get("budget_exhausted"))
        skeleton["partial"] = bool(sliced or budget_exhausted)
        if sliced:
            skeleton["partial_from_line"] = int(want_start or 1)
        if budget_exhausted:
            skeleton.setdefault("notes", []).append(
                f"文件较长，本次只扫描了前 {len(full_text)} 个字符（骨架可能不完整）；"
                "可用 action=read 配合 line 区间分段精读"
            )
        # 敏感文件：骨架里的签名/文档串同样可能夹带凭据，标注出来让调用方注意。
        detection = sensitivity_of(path, hint=str(reader_meta.get("sensitivity") or ""))
        if detection:
            skeleton["sensitive"] = True
            skeleton["sensitivity"] = str(detection)
        if kind:
            skeleton["symbols"] = [
                item for item in skeleton.get("symbols") or []
                if str(item.get("kind") or "") == kind
            ]
        if not bool(args.get("include_imports", True)):
            skeleton.pop("imports", None)
        find_name = str(args.get("find") or "").strip()
        found = None
        if find_name:
            from app.services.code_structure import find_symbol

            found = find_symbol(skeleton, find_name)
            if found is None:
                # 没找到就明确说"没找到"，而不是给空骨架让人以为文件是空的。
                skeleton.setdefault("notes", []).append(f"未找到名为 {find_name} 的符号")
        stats = skeleton.get("stats") or {}
        meta = {
            **service.base_meta(),
            "path": path,
            "language": skeleton.get("language"),
            "parser": "ast" if skeleton.get("language") == "python" else "regex",
            "symbols_returned": len(skeleton.get("symbols") or []),
            "total_lines": total_lines,
            "scanned_chars": len(full_text),
            "pages_read": int(reader_meta.get("pages_read") or 1),
            "budget_exhausted": budget_exhausted,
            "partial": bool(skeleton.get("partial")),
            "sensitive": bool(detection),
            "sensitivity": str(detection or ""),
            "workspace_version": reader_meta.get("workspace_version"),
            "sliced": sliced,
            "start_line": int(want_start or 0) or None,
            "end_line": int(want_end or 0) or None,
        }
        summary = (
            f"已扫描 {path}（{skeleton.get('language')}）："
            f"{stats.get('classes', 0)} 个类、{stats.get('functions', 0)} 个函数、"
            f"{stats.get('methods', 0)} 个方法、{stats.get('imports', 0)} 个导入，"
            f"共 {total_lines} 行；未返回函数体，可用 action=read 配合 line 区间精读。"
        )
        if skeleton.get("truncated"):
            summary += "；符号过多已截断"
        if skeleton.get("notes"):
            summary += "；" + "；".join(str(item) for item in skeleton["notes"][:3])
        data = {
            "path": path,
            "language": skeleton.get("language"),
            "symbols": skeleton.get("symbols") or [],
            "imports": skeleton.get("imports") or [],
            "stats": stats,
            "truncated": bool(skeleton.get("truncated")),
            "notes": skeleton.get("notes") or [],
            "partial": bool(skeleton.get("partial")),
            "found": found,
        }
        return service.envelope(
            ACTION_SCAN,
            status=STATUS_OK if skeleton.get("ok") else STATUS_PARTIAL,
            summary=summary,
            data=data,
            meta=meta,
        )

    async def _fetch(self, *, path: str) -> dict:
        """取整篇正文（切片由服务端纯函数完成，客户端只需给原文）。

        ``WorkspaceReader`` 是**按页**返回的（每页 ``READ_MAX_CHARS``）：只读第一页
        会让大文件的骨架缺掉后半段符号，还会让模型以为"文件里就这些"。所以这里主动
        跟游标续读，直到读完或触到字符/页数预算；预算耗尽时把 ``budget_exhausted``
        放进 meta，由调用方标成 partial。
        """
        service = self.service
        from app.services.workspace_reader import WorkspaceReader

        reader = WorkspaceReader(
            user_id=service.user_id,
            user_role=service.user_role,
            workspace_id=service.workspace_id,
            conversation_id=service.conversation_id,
        )
        payload = await reader.read("", path=path, cursor="", max_chars=READ_MAX_CHARS)
        if str(payload.get("status") or "") in {"failed", "empty"}:
            return payload
        sections = [item for item in (payload.get("content") or []) if isinstance(item, dict)]
        chars = sum(len(str(item.get("text") or "")) for item in sections)
        pages = 1
        cursor = str(payload.get("cursor") or "")
        has_more = bool(payload.get("has_more"))
        budget_exhausted = False
        seen: set[str] = set()
        while has_more and cursor and pages < MAX_SCAN_PAGES and chars < MAX_SCAN_CHARS:
            if cursor in seen:  # 游标不前进 → 停下，别死循环
                budget_exhausted = True
                break
            seen.add(cursor)
            try:
                page = await reader.read("", path=path, cursor=cursor, max_chars=READ_MAX_CHARS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scan 续读失败（{}）：{}", path, str(exc)[:160])
                budget_exhausted = True
                break
            new_sections = [item for item in (page.get("content") or []) if isinstance(item, dict)]
            if not new_sections and not page.get("has_more"):
                break
            sections.extend(new_sections)
            chars += sum(len(str(item.get("text") or "")) for item in new_sections)
            pages += 1
            has_more = bool(page.get("has_more"))
            cursor = str(page.get("cursor") or "")
            if str(page.get("status") or "") == "failed":
                break
        # 只要还剩 has_more，就说明这份文件**没抓完**（页数/字符预算用完，或游标
        # 不再前进）：如实标 partial，不假装骨架是全文。
        if has_more:
            budget_exhausted = True
        meta = dict(payload.get("meta") or {})
        meta["pages_read"] = pages
        meta["scanned_chars"] = chars
        meta["budget_exhausted"] = budget_exhausted
        return {**payload, "content": sections, "has_more": has_more, "cursor": cursor, "meta": meta}


# ── 归一化工具（Electron 返回结构 → 稳定模型契约）──────────────
def payload_has_more(payload: Any) -> bool:
    """Electron 侧是否还有下一页（先看信封，再看 meta）。"""
    if not isinstance(payload, dict):
        return False
    if isinstance(payload.get("has_more"), bool):
        return bool(payload["has_more"])
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("has_more"), bool):
        return bool(meta["has_more"])
    return False


def payload_cursor(payload: Any) -> str:
    """取 Electron 自包含 cursor（nav1 / v1 原样字符串）。"""
    if not isinstance(payload, dict):
        return ""
    for source in (payload, payload.get("meta")):
        if isinstance(source, dict) and source.get("cursor"):
            return str(source["cursor"])
    return ""


def payload_meta(payload: Any) -> dict:
    """合并 Electron 信封 meta 与 data.meta（保留 filtered_ignored / sensitive）。"""
    if not isinstance(payload, dict):
        return {}
    merged: dict = {}
    for source in (payload.get("meta"), (payload.get("data") or {}).get("meta")
                   if isinstance(payload.get("data"), dict) else None):
        if isinstance(source, dict):
            merged.update(source)
    out: dict = {}
    if merged.get("filtered_ignored") is not None:
        try:
            out["filtered_ignored"] = int(merged["filtered_ignored"])
        except (TypeError, ValueError):
            pass
    if merged.get("total_entries") is not None:
        try:
            out["total_entries"] = int(merged["total_entries"])
        except (TypeError, ValueError):
            pass
    if merged.get("sensitive") is not None:
        out["sensitive"] = bool(merged["sensitive"])
    if merged.get("truncated") is not None:
        out["truncated"] = bool(merged["truncated"])
    return out


async def fetch_page(
    service: "WorkspaceNavigatorService",
    *,
    action: str,
    electron_cursor: str,
    fetch: Callable[[str], Any],
    data_key: str,
    offset: int,
    size: int,
) -> tuple[list, str, bool, dict]:
    """取一页结果，并托管 Electron 的 cursor。

    返回 ``(items, electron_cursor, has_more, meta_extra)``：

    * Electron 给了 cursor 时**优先用 Electron 翻页**（后端只托管字符串，
      原样回传，不包装/不重编码），不会出现“只看到首页”；
    * Electron 没给 cursor 时退回后端 offset 切片（同一页内继续切）;
    * Electron 的 cursor 用尽但仍有剩余时，继续用后端 offset 切完余量。
    """
    result = await fetch(electron_cursor)
    items = list(result.get("items") or []) if isinstance(result, dict) else list(result or [])
    next_cursor = str((result or {}).get("cursor") or "") if isinstance(result, dict) else ""
    has_more = bool((result or {}).get("has_more")) if isinstance(result, dict) else False
    meta_extra = dict((result or {}).get("meta") or {}) if isinstance(result, dict) else {}

    start = max(0, int(offset or 0))
    if start >= len(items):
        items = []
    else:
        items = items[start:]
    page = list(items[: max(1, size)])
    remaining = len(items) - len(page)
    page_cursor = next_cursor if remaining <= 0 else ""
    return page, page_cursor, bool(has_more or remaining > 0), meta_extra


def _paginate(items: list, *, offset: int, size: int) -> tuple[list, bool]:
    start = max(0, int(offset or 0))
    page = list(items[start:start + max(1, size)])
    return page, start + len(page) < len(items)


def normalize_entries(data: Any) -> list[dict]:
    """把 workspace_list / workspace_catalog 的返回归一成目录条目。"""
    if isinstance(data, dict):
        for key in ("entries", "top_level", "items", "files", "children", "nodes"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return []
    entries: list[dict] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = clean_path(item.get("path") or item.get("name") or item.get("relative_path"))
        if not path or path.casefold() in seen:
            continue
        seen.add(path.casefold())
        raw_kind = str(item.get("type") or item.get("kind") or "").strip().casefold()
        is_dir = raw_kind in {"dir", "directory", "folder"} or bool(
            item.get("is_directory") or item.get("isDirectory") or item.get("is_dir")
        )
        size = item.get("size")
        entries.append({
            "name": str(item.get("name") or path.rsplit("/", 1)[-1]),
            "path": path,
            "kind": "directory" if is_dir else "file",
            "size": int(size) if isinstance(size, (int, float)) else None,
            "modified_at": item.get("modified_at") or item.get("mtime") or item.get("modified"),
            "ext": "" if is_dir else extension_of(path),
            "ignored": bool(item.get("ignored") or item.get("is_ignored")),
        })
    return entries


def _stat_kind(data: dict) -> str:
    """解析 workspace_stat / 单对象元数据返回：返回 file/directory/''。

    这类返回没有 ``entries`` 列表，只有 ``kind``/``type``/``is_directory`` 等字段；
    必须单独识别，否则会被当成"空目录条目"从而误判为路径不存在。
    """
    if not isinstance(data, dict):
        return ""
    if isinstance(data.get("entries"), list) or isinstance(data.get("items"), list):
        return ""
    raw_kind = str(data.get("kind") or data.get("type") or "").strip().casefold()
    if raw_kind in {"dir", "directory", "folder"}:
        return "directory"
    if raw_kind in {"file", "regular", "f"}:
        return "file"
    for key in ("is_directory", "isDirectory", "is_dir", "directory"):
        if isinstance(data.get(key), bool):
            return "directory" if data[key] else "file"
    if isinstance(data.get("size"), (int, float)):
        return "file"
    return ""


def normalize_matches(data: Any) -> list[dict]:
    """把 workspace_search 的返回归一成“命中位置 + 少量上下文”。"""
    if isinstance(data, dict):
        for key in ("matches", "results", "hits", "items", "files"):
            value = data.get(key)
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        return []
    matches: list[dict] = []
    for item in data:
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        path = clean_path(item.get("path") or item.get("file") or item.get("file_path") or item.get("name"))
        if not path:
            continue
        location = (
            item.get("location") or item.get("position")
            or _compose_location(item)
        )
        context = str(
            item.get("context") or item.get("snippet") or item.get("text") or item.get("preview") or ""
        ).strip()
        path_sensitive = is_sensitive_path(path) or bool(item.get("sensitive"))
        # 搜索的职责是定位：命中上下文固定长度；无论文件名是否可疑，都先跑
        # 脱敏，凭据/PII 原文不回灌模型。
        redaction = redact_sensitive_text(context)
        context = redaction.text
        matches.append({
            "path": path,
            "location": str(location or "")[:160],
            "page": item.get("page"),
            "paragraph": item.get("paragraph") or item.get("para"),
            "line": item.get("line") or item.get("line_number"),
            "sheet": item.get("sheet") or item.get("sheet_name"),
            "match_type": str(
                item.get("match_type") or item.get("type") or item.get("mode") or "content"
            )[:32],
            "context": context[:CONTEXT_SNIPPET],
            "format": str(item.get("format") or format_for(path)),
            # 凭据类路径：Electron 会逐条标记，后端再兜一层保证不丢信号。
            "sensitive": bool(path_sensitive or redaction.redacted),
            "redacted": redaction.redacted,
            "redaction_count": redaction.count,
        })
    return matches


def _compose_location(item: dict) -> str:
    parts: list[str] = []
    if item.get("sheet"):
        parts.append(str(item["sheet"]))
    if item.get("page"):
        parts.append(f"page-{item['page']}")
    if item.get("paragraph"):
        parts.append(f"paragraph-{item['paragraph']}")
    if item.get("line"):
        parts.append(f"line-{item['line']}")
    return " ".join(parts)


def normalize_sections(content: list[dict]) -> list[dict]:
    """统一 read 正文分片：source/location/title/text（与旧契约一致）。"""
    sections: list[dict] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        sections.append({
            "source": str(item.get("source") or "")[:300],
            "location": str(item.get("location") or "")[:120],
            "title": str(item.get("title") or "")[:200],
            "text": str(item.get("text") or ""),
        })
    return sections


def payload_to_text(payload: dict, *, limit: int = 20000) -> str:
    """把 navigator 信封渲染为可注入模型的上下文文本（无正文返回空串）。"""
    if not isinstance(payload, dict):
        return ""
    action = str(payload.get("action") or "")
    status = str(payload.get("status") or "")
    lines: list[str] = []
    summary = str(payload.get("summary") or "")
    if summary:
        lines.append(f"[{action} 摘要] {summary}")
    if status == STATUS_ERROR:
        error = payload.get("error") or {}
        lines.append(
            f"[读取失败 {error.get('code')}] {error.get('message')}"
            f"（建议：{error.get('suggested_action')}）"
        )
        return "\n".join(lines)[:limit]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    for item in data.get("entries") or []:
        if isinstance(item, dict):
            lines.append(
                f"- {item.get('path')}（{'目录' if item.get('kind') == 'directory' else '文件'}"
                f"{'，' + str(item.get('size')) + ' 字节' if item.get('size') else ''}）"
            )
    for item in data.get("matches") or []:
        if isinstance(item, dict):
            lines.append(f"- {item.get('path')} @ {item.get('location')}：{item.get('context')}")
    for item in data.get("sections") or []:
        if isinstance(item, dict):
            header = f"[{item.get('source')} · {item.get('location')}]"
            title = str(item.get("title") or "")
            if title:
                header += f" {title}"
            lines.append(header)
            lines.append(str(item.get("text") or ""))
    if not lines:
        return ""
    if payload.get("has_more"):
        lines.append(f"（还有更多结果，可用 cursor 继续：{payload.get('cursor')}）")
    return "\n".join(lines)[:limit]


MODEL_TEXT_MAX_CHARS = 4000


def handoff_text(payload: Any, *, limit: int = 120000) -> str:
    """把 navigator 信封渲染为**交给下游模型节点**的正文证据。

    为什么不直接把整个信封当 JSON 交给下游：信封里有 ``limits``、``skipped_dirs``、
    ``server_name``、``parser`` 等实现噪音，正文埋在里面，模型容易"看不到内容"并
    在回答里声明自己看不到文档。这里只保留可读事实：

    * read → ``===== 文件 · 位置 · 标题 =====`` + 正文分段；
    * search → 命中路径 + 位置 + 片段；
    * list → 条目清单；
    * error → 错误码 + 建议（让下游能诚实说明失败原因）。

    与 ``model_text`` 的区别：``model_text`` 是**回灌给决策模型**的工具结果（有界、
    紧凑，默认 4000 字符）；``handoff_text`` 是**证据传递**，预算由调用方给
    （默认 120000），不因为"工具结果要短"而把正文截掉。
    """
    if isinstance(payload, str):
        return payload[:limit]
    if not isinstance(payload, dict):
        return str(payload or "")[:limit]
    # 信封可能被包在**执行信封** {"call_id": ..., "data": <navigator 信封>} 里，
    # 也可能本身就是 navigator 信封。判据用执行信封独有的 call_id —— 两种形态都
    # 带 status，只看 status 会把真正的 payload 剥掉或解析错层。
    inner = payload.get("data")
    envelope = (
        inner
        if isinstance(inner, dict) and "call_id" in payload and "status" in inner
        else payload
    )
    if not isinstance(envelope, dict):
        return str(payload)[:limit]
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    action = str(envelope.get("action") or "")
    status = str(envelope.get("status") or "")
    lines: list[str] = []
    summary = str(envelope.get("summary") or "")
    if summary:
        lines.append(f"[{action or 'read'} 摘要] {summary}")
    if status == STATUS_ERROR:
        error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
        lines.append(f"[读取失败 {error.get('code') or ''}] {error.get('message') or ''}".strip())
        if error.get("suggested_action"):
            lines.append(f"下一步建议：{error['suggested_action']}")
        return "\n".join(lines)[:limit]
    for item in data.get("sections") or []:
        if not isinstance(item, dict):
            continue
        header = f"===== {item.get('source') or data.get('path') or ''}"
        if item.get("location"):
            header += f" · {item['location']}"
        if item.get("title"):
            header += f" · {item['title']}"
        lines.append(header + " =====")
        lines.append(str(item.get("text") or ""))
    for item in data.get("entries") or []:
        if isinstance(item, dict):
            kind = "目录" if item.get("kind") == "directory" else "文件"
            lines.append(f"- {item.get('path')}（{kind}）")
    for item in data.get("matches") or []:
        if isinstance(item, dict):
            lines.append(
                f"- {item.get('path')} @ {item.get('location')}：{item.get('context')}"
            )
    if payload.get("has_more") or envelope.get("has_more"):
        lines.append(
            "（本次未读完，还有更多内容；cursor 可继续："
            f"{payload.get('cursor') or envelope.get('cursor')}）"
        )
    if not lines:
        return ""
    return "\n".join(lines)[:limit]


def model_text(payload: dict, *, limit: int = MODEL_TEXT_MAX_CHARS) -> str:
    """把 navigator 信封渲染成模型可读文本（紧凑、有界、绝不含二进制/整份文档）。

    与 ``payload_to_text`` 的区别：``payload_to_text`` 供内部注入上下文使用
    （信息更全），``model_text`` 供工具结果回灌模型使用，必须自己带预算，
    不能依赖下游的通用投影（那会先截断 JSON 再渲染，导致模型读到半截结构）。
    """
    if not isinstance(payload, dict):
        return ""
    action = str(payload.get("action") or "")
    status = str(payload.get("status") or "")
    budget = max(200, int(limit or MODEL_TEXT_MAX_CHARS))
    # 给尾部的 has_more/cursor 提示预留空间：模型必须看得到继续读取的办法，
    # 否则它只能在被截断的正文里猜。
    content_budget = max(120, budget - 120)
    lines: list[str] = [f"[workspace_navigator/{action}] status={status}"]
    summary = str(payload.get("summary") or "")
    if summary:
        lines.append(summary)
    if status == STATUS_ERROR:
        error = payload.get("error") or {}
        lines.append(f"error.code={error.get('code')}")
        if error.get("message"):
            lines.append(str(error["message"]))
        if error.get("suggested_action"):
            lines.append(f"下一步建议：{error['suggested_action']}")
        return "\n".join(lines)[:budget]
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    used = sum(len(line) for line in lines)
    for item in data.get("entries") or []:
        if not isinstance(item, dict):
            continue
        kind = "目录" if item.get("kind") == "directory" else "文件"
        size = f" {item['size']}B" if item.get("size") else ""
        modified = f" mtime={item['modified_at']}" if item.get("modified_at") else ""
        line = f"- {item.get('path')} [{kind}{size}{modified}]"
        if used + len(line) > content_budget:
            lines.append("…（条目较多，可用 cursor 继续）")
            break
        lines.append(line)
        used += len(line)
    for item in data.get("matches") or []:
        if not isinstance(item, dict):
            continue
        line = (
            f"- {item.get('path')} @ {item.get('location')}"
            f" ({item.get('match_type')}/{item.get('format')})：{item.get('context')}"
        )
        if used + len(line) > content_budget:
            lines.append("…（命中较多，可用 cursor 继续）")
            break
        lines.append(line)
        used += len(line)
    for item in data.get("sections") or []:
        if not isinstance(item, dict):
            continue
        header = f"[{item.get('source')} · {item.get('location')}]"
        title = str(item.get("title") or "")
        if title:
            header += f" {title}"
        text = str(item.get("text") or "")
        remaining = content_budget - used - len(header)
        if remaining <= 0:
            lines.append("…（正文较长，可用 cursor 继续读取）")
            break
        lines.append(header)
        lines.append(text[:remaining])
        used += len(header) + min(len(text), remaining)
        if len(text) > remaining:
            lines.append("…（本段已截断，可用 cursor 继续）")
            break
    # scan：骨架必须逐行给出（类/函数/方法 + 行区间），否则模型只看到一个
    # 摘要数字，等于白扫；这里直接复用 code_structure 的纯函数渲染。
    if action == ACTION_SCAN:
        from app.services.code_structure import render_skeleton_lines

        skeleton_lines = render_skeleton_lines(
            data.get("symbols"),
            imports=data.get("imports"),
            stats=data.get("stats"),
            max_lines=200,
        )
        for line in skeleton_lines:
            if used + len(line) > content_budget:
                lines.append("…（骨架较长，已截断；可用 kind/find/行区间缩小范围）")
                break
            lines.append(line)
            used += len(line)
        found = data.get("found")
        if isinstance(found, dict):
            lines.append(
                f"命中：{found.get('name')} [{found.get('kind')}] "
                f"L{found.get('line')}-{found.get('end_line')}；"
                "可用 action=read 配合 start_line/end_line 精读"
            )
    if len(lines) <= 1:
        lines.append("（没有结果）")
    if payload.get("has_more"):
        lines.append(f"has_more=true，继续读取请复用 cursor={payload.get('cursor')}")
    return "\n".join(lines)[:budget]


def success_or_error(payload: dict) -> bool:
    """信封是否包含可用结果（供 orchestrator 判断是否需要降级说明）。"""
    return str(payload.get("status") or "") in {STATUS_OK, STATUS_PARTIAL, STATUS_EMPTY}


__all__ = [
    "ACTIONS",
    "ACTION_LIST",
    "ACTION_READ",
    "ACTION_SEARCH",
    "CURSOR_EXPIRED",
    "CURSOR_STORE",
    "DEFAULT_MAX_CHARS",
    "INVALID_ACTION",
    "INVALID_PARAMS",
    "MAX_DEPTH",
    "MAX_ELECTRON_LIST_ENTRIES",
    "MAX_ELECTRON_SEARCH_RESULTS",
    "MAX_MAX_CHARS",
    "MAX_MAX_RESULTS",
    "MAX_READ_PAGES_PER_CALL",
    "MAX_READ_PAGES_PER_REQUEST",
    "MODEL_TEXT_MAX_CHARS",
    "NavigatorError",
    "CONTEXT_SNIPPET",
    "READ_MAX_CHARS",
    "RedactionResult",
    "redact_sections",
    "redact_sensitive_text",
    "sensitivity_of",
    "NavigatorListHandler",
    "NavigatorReadHandler",
    "NavigatorSearchHandler",
    "SEARCH_MODES",
    "SEARCH_MODE_AUTO",
    "SEARCH_MODE_CONTENT",
    "SEARCH_MODE_FILENAME",
    "SEARCH_TIMEOUT",
    "SENSITIVITY_CREDENTIAL",
    "SENSITIVITY_KEY_MATERIAL",
    "SENSITIVITY_PII",
    "SENSITIVITY_UNKNOWN",
    "SENSITIVE_PATH_MARKERS",
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_PARTIAL",
    "WORKSPACE_DEVICE_OFFLINE",
    "WORKSPACE_NOT_BOUND",
    "WORKSPACE_NOT_REGISTERED",
    "WORKSPACE_PATH_NOT_DIRECTORY",
    "WORKSPACE_PATH_NOT_FOUND",
    "WORKSPACE_READ_FAILED",
    "WORKSPACE_UNSUPPORTED_FORMAT",
    "WorkspaceNavigatorService",
    "build_error",
    "clamp_int",
    "clean_path",
    "extension_of",
    "fetch_page",
    "format_for",
    "handoff_text",
    "has_known_extension",
    "is_root_path",
    "is_sensitive_path",
    "model_text",
    "normalize_entries",
    "normalize_ignored_flag",
    "normalize_matches",
    "normalize_sections",
    "parse_full_read_flag",
    "payload_cursor",
    "payload_has_more",
    "payload_meta",
    "payload_to_text",
    "success_or_error",
]
