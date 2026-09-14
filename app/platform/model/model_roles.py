"""模型档位（Model Profile）与职责角色（Model Role）。

方案目标：把"按场景选模型"升级为"**按任务职责选模型**"。

两层概念（业务代码只认角色，不认模型名）：

* **Model Profile**：``main`` / ``cheap`` / ``reasoning`` / ``vision``
  （``embedding`` 是独立本地模型，不参与聊天路由）。
  每个档位有 provider / base_url / api_key / model + 能力声明
  （超时、最大输出、是否支持工具/JSON/视觉/reasoning、最大上下文）。
* **Model Role**：``title`` / ``summary`` / ``intent_assessor`` / ``query_rewriter``
  / ``memory_extract`` / ``planner_simple`` / ``planner_complex`` / ``tool_read``
  / ``tool_write`` / ``tool_execute`` / ``direct_answer`` / ``final_summary`` /
  ``code_writer`` / ``code_reviewer`` / ``vision``。

解析优先级（高 → 低）：

1. **请求级 BYOK**（``X-LLM-API-KEY`` + 用户选择，密钥永不落库）；
2. **管理员动态角色配置**（Redis ``config:llm:role:{role}``）；
3. **管理员动态档位配置**（Redis ``config:llm:profile:{profile}``）；
4. **角色级 .env**（``LLM_ROLE_*`` + ``LLM_{PROFILE}_*``）；
5. **既有 scene 级 Redis 配置**（兼容保留，见 ``app.platform.model.llm_config``）；
6. **旧全局 .env**（``LLM_PROVIDER`` / ``DEEPSEEK_MODEL`` … 兜底）。

硬约束：

* **密钥只进内存/短期运行态**：``ResolvedModel.public_dict()`` 不含 key，
  可安全写入 Job 快照、SSE 元数据与审计；
* **角色只决定"用哪个模型"，不决定"能不能执行"**：是否存在副作用、是否需要
  审批、是否越权仍由后端确定性规则判定（Router/Capability/Approval）；
* **档位缺配置一律回退 ``main``**，保证"只配了 main 也能跑"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from app.core.config import settings

# ── 档位与角色词表（唯一权威定义）─────────────────────────────

PROFILE_MAIN = "main"
PROFILE_CHEAP = "cheap"
PROFILE_REASONING = "reasoning"
PROFILE_VISION = "vision"
#: embedding 不参与聊天路由（继续使用独立本地模型）。
PROFILE_EMBEDDING = "embedding"

CHAT_PROFILES: tuple[str, ...] = (PROFILE_MAIN, PROFILE_CHEAP, PROFILE_REASONING, PROFILE_VISION)
ALL_PROFILES: tuple[str, ...] = (*CHAT_PROFILES, PROFILE_EMBEDDING)

ROLE_TITLE = "title"
ROLE_SUMMARY = "summary"
ROLE_INTENT_ASSESSOR = "intent_assessor"
ROLE_QUERY_REWRITER = "query_rewriter"
ROLE_MEMORY_EXTRACT = "memory_extract"
ROLE_MEMORY_MERGE = "memory_merge"
ROLE_PRIVACY_CANDIDATE = "privacy_candidate"
ROLE_PLANNER_SIMPLE = "planner_simple"
ROLE_PLANNER_COMPLEX = "planner_complex"
ROLE_TOOL_READ = "tool_read"
ROLE_TOOL_WRITE = "tool_write"
ROLE_TOOL_EXECUTE = "tool_execute"
ROLE_DIRECT_ANSWER = "direct_answer"
ROLE_FINAL_SUMMARY = "final_summary"
ROLE_CODE_WRITER = "code_writer"
ROLE_CODE_REVIEWER = "code_reviewer"
ROLE_VISION = "vision"

ALL_ROLES: tuple[str, ...] = (
    ROLE_TITLE, ROLE_SUMMARY, ROLE_INTENT_ASSESSOR, ROLE_QUERY_REWRITER,
    ROLE_MEMORY_EXTRACT, ROLE_MEMORY_MERGE, ROLE_PRIVACY_CANDIDATE,
    ROLE_PLANNER_SIMPLE, ROLE_PLANNER_COMPLEX, ROLE_TOOL_READ, ROLE_TOOL_WRITE,
    ROLE_TOOL_EXECUTE, ROLE_DIRECT_ANSWER, ROLE_FINAL_SUMMARY,
    ROLE_CODE_WRITER, ROLE_CODE_REVIEWER, ROLE_VISION,
)

#: 角色 → 档位默认映射（与方案 §四 一致；可用 ``LLM_ROLE_*`` / Redis 覆盖）。
DEFAULT_ROLE_PROFILES: dict[str, str] = {
    ROLE_TITLE: PROFILE_CHEAP,
    ROLE_SUMMARY: PROFILE_CHEAP,
    ROLE_INTENT_ASSESSOR: PROFILE_CHEAP,
    ROLE_QUERY_REWRITER: PROFILE_CHEAP,
    ROLE_MEMORY_EXTRACT: PROFILE_CHEAP,
    ROLE_MEMORY_MERGE: PROFILE_CHEAP,
    ROLE_PRIVACY_CANDIDATE: PROFILE_CHEAP,
    ROLE_PLANNER_SIMPLE: PROFILE_CHEAP,
    ROLE_PLANNER_COMPLEX: PROFILE_MAIN,
    ROLE_TOOL_READ: PROFILE_CHEAP,
    ROLE_TOOL_WRITE: PROFILE_MAIN,
    ROLE_TOOL_EXECUTE: PROFILE_REASONING,
    ROLE_DIRECT_ANSWER: PROFILE_MAIN,
    ROLE_FINAL_SUMMARY: PROFILE_MAIN,
    ROLE_CODE_WRITER: PROFILE_MAIN,
    ROLE_CODE_REVIEWER: PROFILE_REASONING,
    ROLE_VISION: PROFILE_VISION,
}

#: 角色失败时的回退策略（方案 §八；"不能静默继续"）。
#: * ``none``        —— 失败即失败（调用方已有兜底，例如标题返回空）
#: * ``main``        —— 换主档位重试一次；仍失败才算失败
#: * ``original``    —— 回退到"原始输入"（查询改写失败用原查询）
ROLE_FALLBACK: dict[str, str] = {
    ROLE_TITLE: "none",
    ROLE_SUMMARY: "none",
    ROLE_INTENT_ASSESSOR: "main",
    ROLE_QUERY_REWRITER: "original",
    ROLE_MEMORY_EXTRACT: "none",
    ROLE_MEMORY_MERGE: "none",
    ROLE_PRIVACY_CANDIDATE: "main",
    ROLE_PLANNER_SIMPLE: "main",
    ROLE_PLANNER_COMPLEX: "main",
    ROLE_TOOL_READ: "main",
    ROLE_TOOL_WRITE: "main",
    ROLE_TOOL_EXECUTE: "main",
    ROLE_DIRECT_ANSWER: "none",
    ROLE_FINAL_SUMMARY: "none",
    ROLE_CODE_WRITER: "main",
    ROLE_CODE_REVIEWER: "main",
    ROLE_VISION: "main",
}

#: 档位能力声明默认值（可被 ``LLM_{PROFILE}_SUPPORTS_*`` 覆盖）。
DEFAULT_PROFILE_CAPABILITIES: dict[str, dict[str, Any]] = {
    PROFILE_MAIN: {
        "supports_tools": True, "supports_json": True, "supports_vision": False,
        "supports_reasoning": True, "timeout": 120.0, "max_tokens": 8192, "max_context": 128_000,
    },
    PROFILE_CHEAP: {
        "supports_tools": False, "supports_json": True, "supports_vision": False,
        "supports_reasoning": False, "timeout": 60.0, "max_tokens": 4096, "max_context": 32_000,
    },
    PROFILE_REASONING: {
        "supports_tools": True, "supports_json": True, "supports_vision": False,
        "supports_reasoning": True, "timeout": 180.0, "max_tokens": 16_384, "max_context": 128_000,
    },
    PROFILE_VISION: {
        "supports_tools": False, "supports_json": False, "supports_vision": True,
        "supports_reasoning": False, "timeout": 120.0, "max_tokens": 4096, "max_context": 32_000,
    },
}

# Redis key 模板（管理员动态配置；与既有 scene 配置同机制）
ROLE_CONFIG_KEY = "config:llm:role:{role}"
PROFILE_CONFIG_KEY = "config:llm:profile:{profile}"
#: 连通性探测结果（管理员页"连通性/最近错误"用；测试连接后写入）。
PROFILE_STATUS_KEY = "config:llm:profile_status:{profile}"
_CACHE_TTL = 5.0
_cache: dict[str, tuple[float, dict | None]] = {}


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """一次角色解析的结果（**不含**密钥的公开视图可直接落快照/审计）。"""

    role: str
    profile: str
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout: float
    reasoning_effort: str | None
    source: str
    capabilities: dict[str, Any] = field(default_factory=dict)
    fallback_profile: str = ""
    byok: bool = False

    @property
    def supports_tools(self) -> bool:
        return bool(self.capabilities.get("supports_tools"))

    @property
    def supports_json(self) -> bool:
        return bool(self.capabilities.get("supports_json"))

    @property
    def supports_vision(self) -> bool:
        return bool(self.capabilities.get("supports_vision"))

    @property
    def supports_reasoning(self) -> bool:
        return bool(self.capabilities.get("supports_reasoning"))

    @property
    def max_context(self) -> int:
        try:
            return int(self.capabilities.get("max_context") or 0)
        except (TypeError, ValueError):
            return 0

    def api_dict(self) -> dict[str, Any]:
        """给 LLM 客户端的完整配置（含密钥；只在内存/短期运行态流动）。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "timeout": self.timeout,
            "reasoning_effort": self.reasoning_effort,
            "source": self.source,
            "byok": self.byok,
        }

    def public_dict(self) -> dict[str, Any]:
        """可落 Job 快照 / SSE 元数据 / 审计（**绝不含密钥**）。"""
        return {
            "role": self.role,
            "profile": self.profile,
            "provider": self.provider,
            "model": self.model,
            "source": self.source,
            "byok": self.byok,
        }


def normalize_profile(value: str | None, *, default: str = PROFILE_MAIN) -> str:
    text = str(value or "").strip().casefold()
    return text if text in ALL_PROFILES else default


def normalize_role(value: str | None) -> str:
    return str(value or "").strip().casefold()


def is_chat_role(role: str | None) -> bool:
    return normalize_role(role) in ALL_ROLES


def profile_capabilities(profile: str) -> dict[str, Any]:
    """档位能力：内置默认 + ``LLM_{PROFILE}_SUPPORTS_*`` 覆盖（**同步层**）。

    管理员动态覆盖（Redis）在 :func:`resolve_role` 里合并，见
    :func:`merge_capability_overrides`。
    """
    name = normalize_profile(profile)
    caps = dict(DEFAULT_PROFILE_CAPABILITIES.get(name) or DEFAULT_PROFILE_CAPABILITIES[PROFILE_MAIN])
    prefix = f"LLM_{name.upper()}_"
    for key in ("supports_tools", "supports_json", "supports_vision", "supports_reasoning"):
        raw = getattr(settings, f"{prefix}{key.upper()}", None)
        if raw is not None:
            caps[key] = bool(raw)
    for key in ("timeout", "max_tokens", "max_context"):
        raw = getattr(settings, f"{prefix}{key.upper()}", None)
        if raw not in (None, "", 0):
            try:
                caps[key] = int(float(raw)) if key != "timeout" else float(raw)
            except (TypeError, ValueError):
                continue
    return caps


#: 管理端（前端配置页）使用的字段名 ↔ 内部能力字段名。
#: 前端用 ``timeout_ms`` / ``max_output_tokens`` / ``max_context_tokens``，
#: 内部用秒 / token 数；这里做唯一一次换算，避免两套命名各自漂移。
CAPABILITY_API_TO_INTERNAL: dict[str, str] = {
    "timeout_ms": "timeout",
    "max_output_tokens": "max_tokens",
    "max_context_tokens": "max_context",
}
CAPABILITY_KEYS: tuple[str, ...] = (
    "timeout_ms", "max_output_tokens", "supports_tools", "supports_json",
    "supports_vision", "supports_reasoning", "max_context_tokens",
)


def capabilities_to_api(caps: dict[str, Any] | None) -> dict[str, Any]:
    """内部能力 → 前端字段名（``timeout`` 秒 → ``timeout_ms`` 毫秒）。"""
    source = dict(caps or {})
    out: dict[str, Any] = {}
    try:
        out["timeout_ms"] = int(float(source.get("timeout") or 0) * 1000) or None
    except (TypeError, ValueError):
        out["timeout_ms"] = None
    out["max_output_tokens"] = source.get("max_tokens")
    out["max_context_tokens"] = source.get("max_context")
    for key in ("supports_tools", "supports_json", "supports_vision", "supports_reasoning"):
        out[key] = source.get(key)
    return {key: value for key, value in out.items() if value is not None}


def capabilities_from_api(values: dict[str, Any] | None) -> dict[str, Any]:
    """前端字段名 → 内部能力（只取白名单键，非法值忽略）。"""
    source = dict(values or {})
    out: dict[str, Any] = {}
    for api_key, internal_key in CAPABILITY_API_TO_INTERNAL.items():
        if api_key not in source:
            continue
        raw = source[api_key]
        if raw in (None, ""):
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError):
            continue
        if internal_key == "timeout":
            # 前端毫秒 → 内部秒；上限夹在 5s..900s，避免误填把请求卡死或秒断。
            seconds = max(5.0, min(900.0, number / 1000.0))
            out[internal_key] = seconds
        else:
            out[internal_key] = int(max(0, number))
    for key in ("supports_tools", "supports_json", "supports_vision", "supports_reasoning"):
        if key in source and source[key] is not None:
            out[key] = bool(source[key])
    return out


def merge_capability_overrides(profile: str, overrides: dict[str, Any] | None) -> dict[str, Any]:
    """把动态覆盖（Redis）合并进档位能力（``None`` 值不覆盖）。"""
    caps = profile_capabilities(profile)
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        caps[key] = value
    return caps


def _profile_env(profile: str) -> dict[str, Any]:
    """档位 .env 配置：``LLM_{PROFILE}_*``（空值表示"继承 main / 沿用旧配置"）。"""
    name = normalize_profile(profile)
    prefix = f"LLM_{name.upper()}_"
    return {
        "provider": str(getattr(settings, f"{prefix}PROVIDER", "") or "").strip(),
        "base_url": str(getattr(settings, f"{prefix}BASE_URL", "") or "").strip(),
        "api_key": str(getattr(settings, f"{prefix}API_KEY", "") or "").strip(),
        "model": str(getattr(settings, f"{prefix}MODEL", "") or "").strip(),
        "reasoning_effort": str(getattr(settings, f"{prefix}REASONING_EFFORT", "") or "").strip(),
    }


def legacy_env_for(provider: str | None = None) -> dict[str, Any]:
    """旧全局 .env 兜底（与 ``llm_config._env_fallback`` 同语义，避免两套默认值）。"""
    chosen = str(provider or settings.LLM_PROVIDER or "deepseek").strip().casefold()
    if chosen == "qwen":
        return {
            "provider": "qwen",
            "base_url": settings.QWEN_BASE_URL,
            "api_key": settings.QWEN_API_KEY,
            "model": settings.QWEN_MODEL,
            "timeout": 120.0,
            "reasoning_effort": None,
        }
    return {
        "provider": "deepseek",
        "base_url": settings.DEEPSEEK_BASE_URL,
        "api_key": settings.DEEPSEEK_API_KEY,
        "model": settings.DEEPSEEK_MODEL,
        "timeout": 120.0,
        "reasoning_effort": None,
    }


def _cheap_env() -> dict[str, Any]:
    """cheap 档位的"就地兜底"：用项目已有的低成本模型配置。

    这样即使没配 ``LLM_CHEAP_*``，切到 cheap 角色也会落到
    ``DS_FLASH_MODEL`` / ``QWEN_TURBO_MODEL``，而不是悄悄用主模型烧钱。
    """
    flash_model = str(getattr(settings, "DS_FLASH_MODEL", "") or "").strip()
    if flash_model:
        return {
            "provider": "deepseek",
            "base_url": str(getattr(settings, "DS_FLASH_BASE_URL", "") or settings.DEEPSEEK_BASE_URL),
            "api_key": str(getattr(settings, "DS_FLASH_API_KEY", "") or settings.DEEPSEEK_API_KEY),
            "model": flash_model,
            "timeout": 60.0,
            "reasoning_effort": None,
        }
    return {
        "provider": "qwen",
        "base_url": settings.QWEN_BASE_URL,
        "api_key": settings.QWEN_API_KEY,
        "model": str(getattr(settings, "QWEN_TURBO_MODEL", "") or settings.QWEN_MODEL),
        "timeout": 60.0,
        "reasoning_effort": None,
    }


def _reasoning_env() -> dict[str, Any]:
    model = str(getattr(settings, "CHAT_THINK_MODEL", "") or "").strip()
    if model:
        return {
            "provider": str(getattr(settings, "LLM_PROVIDER", "") or "deepseek"),
            "base_url": str(getattr(settings, "CHAT_THINK_BASE_URL", "") or settings.DEEPSEEK_BASE_URL),
            "api_key": str(getattr(settings, "CHAT_THINK_API_KEY", "") or settings.DEEPSEEK_API_KEY),
            "model": model,
            "timeout": 180.0,
            "reasoning_effort": str(getattr(settings, "AGENT_LLM_REASONING_EFFORT", "") or "") or None,
        }
    return legacy_env_for(PROFILE_MAIN)


def _vision_env() -> dict[str, Any]:
    # 视觉默认走本机 Qwen-VL（Ollama / 千问兼容端点），provider 固定 qwen：
    # 文本主模型的 provider 与视觉模型无关，不能因为 LLM_PROVIDER=deepseek
    # 就把 vision 档位标成 deepseek。
    provider = "qwen" if "qwen" in str(settings.VL_MODEL or "").casefold() or "11434" in str(settings.VL_BASE_URL or "") else str(settings.LLM_PROVIDER or "qwen")
    return {
        "provider": provider,
        "base_url": settings.VL_BASE_URL,
        "api_key": settings.VL_API_KEY,
        "model": settings.VL_MODEL,
        "timeout": 120.0,
        "reasoning_effort": None,
    }


def _default_env_for(profile: str) -> dict[str, Any]:
    name = normalize_profile(profile)
    if name == PROFILE_CHEAP:
        return _cheap_env()
    if name == PROFILE_REASONING:
        return _reasoning_env()
    if name == PROFILE_VISION:
        return _vision_env()
    return legacy_env_for()


async def _read_role_config(key: str) -> dict | None:
    """带进程内缓存的 Redis 读取（与 ``llm_config`` 同机制，故障静默回落）。"""
    now = time.monotonic()
    cached = _cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    value: dict | None = None
    try:
        from app.core.redis import get_redis

        raw = await get_redis().get(key)
        if raw:
            parsed = json.loads(raw)
            value = parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 - 配置源故障不影响主流程
        logger.debug("[model-role] 读取动态配置失败，回落默认: {}", str(exc)[:120])
    _cache[key] = (now + _CACHE_TTL, value)
    return value


def invalidate_role_cache(role: str | None = None, profile: str | None = None) -> None:
    """管理员改配置后立即失效（无需重启进程）。"""
    if role:
        _cache.pop(ROLE_CONFIG_KEY.format(role=normalize_role(role)), None)
    if profile:
        _cache.pop(PROFILE_CONFIG_KEY.format(profile=normalize_profile(profile)), None)
    if not role and not profile:
        _cache.clear()


def role_profile(role: str) -> str:
    """角色 → 档位（``LLM_ROLE_*`` 覆盖默认映射）。"""
    name = normalize_role(role)
    configured = str(getattr(settings, f"LLM_ROLE_{name.upper()}", "") or "").strip()
    if configured:
        return normalize_profile(configured, default=DEFAULT_ROLE_PROFILES.get(name, PROFILE_MAIN))
    return DEFAULT_ROLE_PROFILES.get(name, PROFILE_MAIN)


async def resolve_role(
    role: str,
    *,
    scene: str | None = None,
    user_id: str | None = None,
    request_api_key: str | None = None,
) -> ResolvedModel:
    """解析一个逻辑角色最终使用的模型（优先级见模块 docstring）。

    ``request_api_key`` 只提供凭据，**不改变**模型/端点选择：BYOK 的模型选择
    来自用户级配置（``get_llm_config(user_id=...)``），避免"一个 key 悄悄换了模型"。
    """
    name = normalize_role(role) or ROLE_DIRECT_ANSWER
    # 0) 视觉角色永远走 vision 档位（图片请求不能落到纯文本模型）。
    profile = PROFILE_VISION if name == ROLE_VISION else role_profile(name)

    from app.platform.model.llm_config import get_llm_config

    # 1) 请求级 / 用户级 BYOK（含 scene 级 Redis 兼容链）优先：整份配置照用。
    user_cfg: dict[str, Any] = {}
    try:
        if user_id:
            from app.services.user_llm_config import get_user_llm_config

            raw_user = await get_user_llm_config(user_id)
            if isinstance(raw_user, dict) and raw_user.get("byok"):
                user_cfg = raw_user
        if user_cfg:
            resolved_user = await get_llm_config(scene=scene, user_id=user_id)
            if resolved_user:
                return _from_mapping(
                    name, profile, resolved_user,
                    source="byok" if request_api_key else str(resolved_user.get("source") or "user"),
                    api_key=request_api_key or str(resolved_user.get("api_key") or ""),
                    byok=True,
                )
    except Exception as exc:  # noqa: BLE001 - BYOK 读取失败不能阻断
        logger.debug("[model-role] BYOK 解析失败，回落服务端配置: {}", str(exc)[:120])

    # 2) 管理员动态角色配置 → 3) 动态档位配置 → 4/5/6) .env 与既有链
    role_cfg = await _read_role_config(ROLE_CONFIG_KEY.format(role=name))
    profile_cfg = await _read_role_config(PROFILE_CONFIG_KEY.format(profile=profile))

    env_cfg = _profile_env(profile)
    defaults = _default_env_for(profile)
    merged: dict[str, Any] = {}
    for layer in (defaults, env_cfg, profile_cfg or {}, role_cfg or {}):
        for key, value in layer.items():
            if value in (None, ""):
                continue
            merged[key] = value
    if not str(merged.get("model") or "").strip():
        # 档位没配出模型：整体回退 main（"只配了 main 也能跑"），并记录回退来源。
        merged = {
            **_default_env_for(PROFILE_MAIN),
            **{k: v for k, v in merged.items() if v not in (None, "")},
            "profile_fallback": PROFILE_MAIN,
        }
    # 动态角色配置可以直接把角色指向另一个档位（前端"角色 → 档位"页）。
    target_profile = normalize_profile(
        str((role_cfg or {}).get("profile") or "") or profile,
        default=profile,
    )
    if target_profile != profile:
        merged = {
            **{k: v for k, v in _default_env_for(target_profile).items() if v not in (None, "")},
            **(await _read_role_config(PROFILE_CONFIG_KEY.format(profile=target_profile)) or {}),
            "profile_fallback": profile,
        }
        if not str(merged.get("model") or "").strip():
            merged = {**_default_env_for(PROFILE_MAIN), "profile_fallback": PROFILE_MAIN}
        profile = target_profile
    source = "admin" if (role_cfg or profile_cfg) else ("env" if any(env_cfg.values()) else "default")
    return _from_mapping(
        name,
        profile,
        merged,
        source=source,
        api_key=request_api_key or str(merged.get("api_key") or ""),
        byok=False,
    )


def _from_mapping(
    role: str,
    profile: str,
    cfg: dict[str, Any],
    *,
    source: str,
    api_key: str,
    byok: bool,
) -> ResolvedModel:
    # 能力覆盖可能来自两处：``capabilities`` 子对象（调用方展开）或档位配置里的
    # 顶层能力键（管理员动态覆盖就是这么存的）。
    overrides: dict[str, Any] = {}
    inline = cfg.get("capabilities")
    if isinstance(inline, dict):
        overrides.update(inline)
    for key in ("timeout", "max_tokens", "max_context", "supports_tools",
                "supports_json", "supports_vision", "supports_reasoning"):
        if cfg.get(key) is not None:
            overrides[key] = cfg[key]
    caps = merge_capability_overrides(profile, overrides)
    timeout = overrides.get("timeout") or cfg.get("timeout")
    try:
        timeout_value = float(timeout) if timeout not in (None, "") else float(caps.get("timeout") or 120.0)
    except (TypeError, ValueError):
        timeout_value = float(caps.get("timeout") or 120.0)
    return ResolvedModel(
        role=role,
        profile=normalize_profile(profile),
        provider=str(cfg.get("provider") or settings.LLM_PROVIDER or ""),
        model=str(cfg.get("model") or ""),
        base_url=str(cfg.get("base_url") or "").rstrip("/"),
        api_key=str(api_key or cfg.get("api_key") or ""),
        timeout=timeout_value,
        reasoning_effort=cfg.get("reasoning_effort") or None,
        source=str(source or "env"),
        capabilities=caps,
        fallback_profile=str(cfg.get("profile_fallback") or ""),
        byok=bool(byok),
    )


def role_fallback_policy(role: str) -> str:
    return ROLE_FALLBACK.get(normalize_role(role), "none")


async def role_capabilities(role: str) -> dict[str, Any]:
    resolved = await resolve_role(role)
    return dict(resolved.capabilities)


async def set_role_profile(role: str, profile: str | None) -> None:
    """管理员动态配置：某角色固定到某档位（``None`` = 清除，回落 .env）。"""
    name = normalize_role(role)
    if name not in ALL_ROLES:
        raise ValueError(f"未知模型角色：{role}")
    from app.core.redis import get_redis

    key = ROLE_CONFIG_KEY.format(role=name)
    redis = get_redis()
    if profile in (None, ""):
        await redis.delete(key)
    else:
        await redis.set(key, json.dumps({"profile": normalize_profile(profile)}, ensure_ascii=False))
    invalidate_role_cache(role=name)


async def set_profile_config(profile: str, cfg: dict[str, Any] | None) -> None:
    """管理员动态配置：覆盖某档位的 provider/base_url/api_key/model + 能力（``None`` = 清除）。

    接受**前端字段名**（``timeout_ms`` / ``max_output_tokens`` / ``max_context_tokens``
    / ``supports_*``）与内部名（``timeout`` / ``max_tokens`` / ``max_context``）两种写法。
    ``api_key`` 留空表示"不修改"（前端永不回填明文，只提交被改动的字段）。
    """
    name = normalize_profile(profile)
    if name not in ALL_PROFILES:
        raise ValueError(f"未知模型档位：{profile}")
    from app.core.redis import get_redis

    key = PROFILE_CONFIG_KEY.format(profile=name)
    redis = get_redis()
    if not cfg:
        await redis.delete(key)
        invalidate_role_cache(profile=name)
        return

    # 只改部分字段时合并既有动态配置（避免把上次改的 key 覆盖掉）。
    existing = await _read_role_config(key) or {}
    safe: dict[str, Any] = dict(existing)
    for name_key in ("provider", "base_url", "api_key", "model", "reasoning_effort"):
        value = cfg.get(name_key)
        if value not in (None, ""):
            safe[name_key] = value
    # 移除显式置空的能力项（前端"清空该字段"语义）
    for api_key, internal_key in CAPABILITY_API_TO_INTERNAL.items():
        if api_key in cfg and cfg.get(api_key) in ("", None):
            safe.pop(internal_key, None)
    for flag in ("supports_tools", "supports_json", "supports_vision", "supports_reasoning"):
        if flag in cfg and cfg.get(flag) is None:
            safe.pop(flag, None)
    safe.update(capabilities_from_api(cfg))
    capability_overrides = {
        key_name: safe[key_name]
        for key_name in ("timeout", "max_tokens", "max_context", "supports_tools",
                         "supports_json", "supports_vision", "supports_reasoning")
        if safe.get(key_name) is not None
    }
    payload = {
        k: v for k, v in safe.items()
        if k in {"provider", "base_url", "api_key", "model", "reasoning_effort"}
        or k in capability_overrides
    }
    await redis.set(key, json.dumps(payload, ensure_ascii=False))
    invalidate_role_cache(profile=name)


async def set_profile_status(profile: str, status: dict[str, Any]) -> None:
    """记录档位最近一次连通性探测结果（管理员页展示用；不含密钥）。"""
    name = normalize_profile(profile)
    try:
        from app.core.redis import get_redis

        await get_redis().set(
            PROFILE_STATUS_KEY.format(profile=name),
            json.dumps(dict(status or {}), ensure_ascii=False),
            ex=24 * 3600,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[model-role] 连通性状态写入失败: {}", str(exc)[:120])


async def profile_status(profile: str) -> dict[str, Any]:
    name = normalize_profile(profile)
    value = await _read_role_config(PROFILE_STATUS_KEY.format(profile=name))
    return dict(value or {})


def _mask_key(value: str) -> str:
    """API Key 脱敏（只保留末 4 位；管理员页永不回填明文）。"""
    raw = str(value or "")
    if not raw:
        return ""
    if len(raw) <= 4:
        return "••••"
    return f"{'•' * min(12, len(raw) - 4)}{raw[-4:]}"


def _host_only(url: str) -> str:
    """只给 host（管理员页需要区分网关，但不该铺开完整 URL 与查询串）。"""
    from urllib.parse import urlsplit

    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        return parsed.netloc or ""
    except ValueError:
        return ""


async def role_config_view() -> dict[str, Any]:
    """当前生效的档位与角色视图（**前端管理页契约**）。

    形状（与 ``src/services/modelPlan.js::normalizeProfileConfig`` 对齐）：

    * ``profiles``：**数组**，每项 ``{profile, provider, model, base_url, api_key_masked,
      has_api_key, api_key_last4, connectivity, last_error, config_source, capabilities{...},
      role_overrides}``；能力字段用 ``timeout_ms`` / ``max_output_tokens`` /
      ``max_context_tokens``；
    * ``roles``：``{role: profile}``（前端直接当作"角色覆盖表"用）；
    * **不含任何明文密钥**（只给脱敏文本 / 是否已配置 / 末 4 位）。
    """
    role_overrides: dict[str, str] = {}
    roles: dict[str, Any] = {}
    for role in ALL_ROLES:
        resolved = await resolve_role(role)
        role_overrides[role] = resolved.profile
        item = resolved.public_dict()
        item["fallback"] = role_fallback_policy(role)
        item["model_name"] = resolved.model
        roles[role] = item

    profiles: list[dict[str, Any]] = []
    for profile in ALL_PROFILES:
        if profile == PROFILE_EMBEDDING:
            continue
        role_cfg = await _read_role_config(PROFILE_CONFIG_KEY.format(profile=profile)) or {}
        env_cfg = _profile_env(profile)
        defaults = _default_env_for(profile)
        api_key = str(role_cfg.get("api_key") or env_cfg.get("api_key") or defaults.get("api_key") or "")
        status = await profile_status(profile)
        profiles.append({
            "profile": profile,
            "provider": str(role_cfg.get("provider") or env_cfg.get("provider") or defaults.get("provider") or ""),
            "model": str(role_cfg.get("model") or env_cfg.get("model") or defaults.get("model") or ""),
            "base_url": str(role_cfg.get("base_url") or env_cfg.get("base_url") or defaults.get("base_url") or ""),
            "has_api_key": bool(api_key),
            "api_key_masked": _mask_key(api_key),
            "api_key_last4": api_key[-4:] if len(api_key) > 4 else "",
            "connectivity": str(status.get("status") or "unknown"),
            "last_error": str(status.get("error") or ""),
            "latency_ms": status.get("latency_ms"),
            "config_source": "admin" if role_cfg else ("env" if any(env_cfg.values()) else "default"),
            "capabilities": capabilities_to_api(merge_capability_overrides(profile, role_cfg)),
            "role_overrides": dict(role_overrides),
        })
    return {"profiles": profiles, "roles": role_overrides, "role_details": roles}


__all__ = [
    "ALL_PROFILES",
    "ALL_ROLES",
    "CAPABILITY_API_TO_INTERNAL",
    "CAPABILITY_KEYS",
    "CHAT_PROFILES",
    "DEFAULT_PROFILE_CAPABILITIES",
    "DEFAULT_ROLE_PROFILES",
    "PROFILE_CHEAP",
    "PROFILE_EMBEDDING",
    "PROFILE_MAIN",
    "PROFILE_REASONING",
    "PROFILE_VISION",
    "PROFILE_CONFIG_KEY",
    "PROFILE_STATUS_KEY",
    "ROLE_CONFIG_KEY",
    "ROLE_CODE_REVIEWER",
    "ROLE_CODE_WRITER",
    "ROLE_DIRECT_ANSWER",
    "ROLE_FALLBACK",
    "ROLE_FINAL_SUMMARY",
    "ROLE_INTENT_ASSESSOR",
    "ROLE_MEMORY_EXTRACT",
    "ROLE_MEMORY_MERGE",
    "ROLE_PLANNER_COMPLEX",
    "ROLE_PLANNER_SIMPLE",
    "ROLE_PRIVACY_CANDIDATE",
    "ROLE_QUERY_REWRITER",
    "ROLE_SUMMARY",
    "ROLE_TITLE",
    "ROLE_TOOL_EXECUTE",
    "ROLE_TOOL_READ",
    "ROLE_TOOL_WRITE",
    "ROLE_VISION",
    "ResolvedModel",
    "capabilities_from_api",
    "capabilities_to_api",
    "invalidate_role_cache",
    "is_chat_role",
    "legacy_env_for",
    "merge_capability_overrides",
    "normalize_profile",
    "normalize_role",
    "profile_capabilities",
    "profile_status",
    "resolve_role",
    "role_capabilities",
    "role_config_view",
    "role_fallback_policy",
    "role_profile",
    "set_profile_config",
    "set_profile_status",
    "set_role_profile",
]
