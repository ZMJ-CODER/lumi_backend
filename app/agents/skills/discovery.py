"""工具四层发现：L0 注册资产、L1 域索引、L2 动态检索、L3 收窄。"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Iterable
import json
import yaml

from app.agents.skills.capability import ToolCapability


_LEGACY_L0_DEFAULTS = {
    "general": ("infra", ["用户明确要求该工具所声明的能力"], ["请求不属于该工具声明的能力范围"]),
    "office": ("document", ["用户明确要求处理办公内容"], ["请求不涉及办公内容或需要外部写操作"]),
    "filesystem": ("document", ["用户明确授权访问指定文件或目录"], ["路径未授权或请求扩大访问范围"]),
    "shell": ("system", ["用户明确要求执行受控脚本或命令"], ["命令会绕过沙箱、权限或安全确认"]),
    "process": ("system", ["用户明确要求查看或管理指定进程"], ["未指定目标或操作需要绕过权限确认"]),
    "system": ("system", ["用户明确询问系统信息或确定性计算"], ["请求要求访问敏感环境变量或未授权数据"]),
    "network": ("research", ["用户明确要求访问公开网络或已授权知识库"], ["请求访问私有项目、凭据或未授权内网"]),
    "devtools": ("development", ["用户明确授权对指定项目执行开发辅助操作"], ["未绑定项目范围或请求执行破坏性写操作"]),
    "desktop": ("desktop", ["用户明确要求通过已绑定客户端执行桌面操作"], ["没有绑定客户端或请求访问服务器资源"]),
    "orchestration": ("orchestration", ["编排任务明确要求汇总或推进节点"], ["请求绕过任务状态和执行策略"]),
}


def _load_domain_policy() -> dict:
    """加载 L1 域描述；文件缺失或非法时返回空，不能阻断能力发现。"""
    try:
        from app.core.config import settings

        path = _resolve_policy_path(getattr(settings, "AGENT_TOOL_DOMAIN_POLICY_PATH", "config/agent_policies/tool_domains.yaml"))
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        domains = payload.get("domains", {}) if isinstance(payload, dict) else {}
        return domains if isinstance(domains, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def _load_registry_policy() -> dict:
    """加载 L0 工具注册策略；相对路径相对项目根解析。"""
    try:
        from app.core.config import settings

        path = _resolve_policy_path(getattr(settings, "AGENT_TOOL_REGISTRY_POLICY_PATH", "config/agent_policies/tool_registry.yaml"))
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def base_tool_names() -> set[str]:
    """读取规范基础工具名单。"""
    try:
        from app.core.config import settings

        path = _resolve_policy_path(getattr(settings, "AGENT_BASE_TOOLS_POLICY_PATH", "config/agent_policies/base_tools.yaml"))
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tools = payload.get("tools", {}) if isinstance(payload, dict) else {}
        return {str(name).strip() for name in tools if str(name).strip()} if isinstance(tools, dict) else set()
    except (OSError, ValueError, yaml.YAMLError):
        return set()


def _resolve_policy_path(value: object) -> Path:
    """策略路径不依赖当前工作目录，支持 Docker 与 IDE 两种启动方式。"""
    path = Path(str(value))
    if path.is_absolute():
        return path
    # discovery.py 位于 app/agents/skills，下两级的项目根是 parents[3]；
    # 镜像中通常为 /app，IDE 中为仓库根，均不依赖进程当前目录。
    return Path(__file__).resolve().parents[3] / path


@dataclass(frozen=True)
class DomainGroup:
    """L1 域索引单元；只保存描述和能力名，不复制完整 schema。"""

    name: str
    description: str
    tools: tuple[str, ...]
    keywords: tuple[str, ...] = ()


@dataclass
class ToolDiscoverySession:
    """L2 会话级发现缓存，避免同一会话重复展开 schema。"""

    loaded_tools: dict[str, ToolCapability] = field(default_factory=dict)
    discovered_domains: set[str] = field(default_factory=set)
    policy_version: str = ""

    def add(self, capabilities: Iterable[ToolCapability]) -> None:
        for capability in capabilities:
            self.loaded_tools[capability.name] = capability

    async def load(self, user_id: str, conversation_id: str) -> None:
        """从 Redis 恢复会话发现元数据；Redis 不可用时保持空缓存。"""
        if not user_id or not conversation_id:
            return
        try:
            from app.core.redis import get_redis

            raw = await get_redis().get(_cache_key(user_id, conversation_id))
            payload = json.loads(raw) if raw else {}
            if not isinstance(payload, dict):
                return
            current = _policy_version()
            if str(payload.get("policy_version") or "") != current:
                return
            values = payload.get("tools") or []
            self.add(ToolCapability.model_validate(item) for item in values if isinstance(item, dict))
            self.discovered_domains.update(str(item) for item in payload.get("domains") or [] if str(item))
            self.policy_version = current
        except Exception:
            return

    async def save(self, user_id: str, conversation_id: str) -> None:
        """保存可回放的工具元数据，不落用户原文和工具结果。"""
        if not user_id or not conversation_id or not self.loaded_tools:
            return
        try:
            from app.core.config import settings
            from app.core.redis import get_redis

            self.policy_version = _policy_version()
            payload = {
                "policy_version": self.policy_version,
                "domains": sorted(self.discovered_domains),
                "tools": [item.model_dump(mode="json") for item in self.loaded_tools.values() if item.status == "stable"],
            }
            await get_redis().set(
                _cache_key(user_id, conversation_id),
                json.dumps(payload, ensure_ascii=False),
                ex=max(60, int(getattr(settings, "TOOL_DISCOVERY_CACHE_TTL_SECONDS", 1800))),
            )
        except Exception:
            return


def _cache_key(user_id: str, conversation_id: str) -> str:
    return f"skill:discovery:{user_id}:{conversation_id}"


def _policy_version() -> str:
    try:
        from app.core.config import settings

        path = _resolve_policy_path(getattr(settings, "AGENT_TOOL_DOMAIN_POLICY_PATH", "config/agent_policies/tool_domains.yaml"))
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str(payload.get("version") or "unknown") if isinstance(payload, dict) else "unknown"
    except Exception:
        return "unknown"


def apply_skill_allowlist(
    capabilities: Iterable[ToolCapability],
    allowed_tools: Iterable[str] | None,
) -> list[ToolCapability]:
    """执行 L3 Skill 最终过滤；空白名单表示不增加额外限制。"""
    items = list(capabilities)
    if allowed_tools is None:
        return items
    names = {str(name).strip() for name in allowed_tools if str(name).strip()}
    return [item for item in items if item.name in names]


def apply_workflow_skill_scope(
    capabilities: Iterable[ToolCapability],
    skill: object,
) -> list[ToolCapability]:
    """按 Workflow Skill 声明的 allowed_tools 执行 L3 最终收窄。"""
    allowed = getattr(skill, "allowed_tools", None)
    return apply_skill_allowlist(capabilities, allowed)


def enrich_tool_capability(capability: ToolCapability) -> ToolCapability:
    """用 L0 集中策略补齐历史工具缺失的治理元数据。"""
    policy = _load_registry_policy()
    defaults = policy.get("defaults") if isinstance(policy.get("defaults"), dict) else {}
    category = str(capability.category or "general")
    domain = str(capability.domain or category)
    builtin_domain, builtin_use_when, builtin_do_not_use_when = _LEGACY_L0_DEFAULTS.get(
        category, _LEGACY_L0_DEFAULTS["general"]
    )
    values = defaults.get(category) or defaults.get(domain) or {}
    if not isinstance(values, dict):
        values = {}
    updates = {
        "domain": capability.domain or str(values.get("domain") or builtin_domain),
        "use_when": capability.use_when or list(values.get("use_when") or builtin_use_when),
        "do_not_use_when": capability.do_not_use_when or list(values.get("do_not_use_when") or builtin_do_not_use_when),
    }
    overrides = policy.get("tools") if isinstance(policy.get("tools"), dict) else {}
    tool_values = overrides.get(capability.name) if isinstance(overrides.get(capability.name), dict) else {}
    for key in ("domain", "use_when", "do_not_use_when"):
        if not getattr(capability, key) and tool_values.get(key):
            updates[key] = tool_values[key]
    return capability.model_copy(update=updates)


def _terms(value: str) -> set[str]:
    text = str(value or "").casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text))
    han = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    terms.update(han[i : i + 2] for i in range(max(0, len(han) - 1)))
    return {item for item in terms if item}


def validate_tool_entry(capability: ToolCapability) -> list[str]:
    """L0 准入校验；返回问题列表，空列表表示通过。"""
    errors: list[str] = []
    if not capability.name:
        errors.append("缺少工具名")
    schema = capability.parameters
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        errors.append("parameters 必须是 object schema")
    if not capability.description.strip():
        errors.append("缺少 description")
    if not capability.use_when:
        errors.append("缺少 use_when")
    if not capability.do_not_use_when:
        errors.append("缺少 do_not_use_when")
    if capability.action_type not in {"read", "write"}:
        errors.append("action_type 必须为 read/write")
    if capability.idempotency_type not in {"natural_key", "explicit_key", "non_idempotent"}:
        errors.append("idempotency_type 无效")
    if not capability.domain:
        errors.append("缺少 domain")
    return errors


def build_domain_groups(capabilities: Iterable[ToolCapability]) -> dict[str, DomainGroup]:
    """按根资源/域聚合 L1 索引。"""
    grouped: dict[str, list[ToolCapability]] = {}
    for capability in capabilities:
        domain = str(capability.domain or capability.category or "general")
        grouped.setdefault(domain, []).append(capability)
    policy = _load_domain_policy()
    return {
        domain: DomainGroup(
            name=domain,
            description=str((policy.get(domain) or {}).get("description") or f"{domain} 域工具；仅处理该域声明的资源。"),
            tools=tuple(sorted(item.name for item in items)),
            keywords=tuple(sorted({tag for item in items for tag in item.intent_tags} | set((policy.get(domain) or {}).get("keywords") or []))),
        )
        for domain, items in grouped.items()
    }


def search_domains(query: str, groups: dict[str, DomainGroup], *, limit: int = 3) -> list[DomainGroup]:
    terms = _terms(query)
    ranked = []
    for group in groups.values():
        score = len(terms & _terms(group.name + " " + group.description + " " + " ".join(group.keywords)))
        score += sum(1 for tool in group.tools if terms & _terms(tool))
        if score:
            ranked.append((score, group.name, group))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked[: max(1, limit)]]


def search_tools(
    query: str,
    capabilities: Iterable[ToolCapability],
    *,
    limit: int = 5,
    allowed_tools: set[str] | None = None,
) -> list[ToolCapability]:
    """L2 两级检索：先定位域，再在域内展开少量完整能力。"""
    items = [enrich_tool_capability(item) for item in capabilities if allowed_tools is None or item.name in allowed_tools]
    groups = build_domain_groups(items)
    domains = search_domains(query, groups, limit=3)
    domain_names = {item.name for item in domains}
    if not domain_names:
        # No domain evidence means no discovery hit; do not turn L2 into a
        # generic catalogue. Callers may fall back to their existing selector.
        return []
    terms = _terms(query)
    ranked: list[tuple[int, str, ToolCapability]] = []
    for item in items:
        if item.domain not in domain_names or item.status in {"deprecated", "disabled"}:
            continue
        searchable = " ".join([
            item.name, item.description, item.domain, item.resource,
            *item.intent_tags, *item.use_when,
        ])
        score = len(terms & _terms(searchable))
        if item.domain in domain_names:
            score += 2
        if item.deprecated_by:
            score -= 100
        if score > 0:
            ranked.append((score, item.name, item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    return [row[2] for row in ranked[: max(1, limit)]]


def record_discovery(
    query: str,
    *,
    groups: dict[str, DomainGroup],
    results: Iterable[ToolCapability],
    scene: str = "",
    user_id: str = "",
    job_id: str = "",
    session: ToolDiscoverySession | None = None,
) -> dict:
    """记录一次 L1/L2 发现；只落匿名查询指纹和能力元数据。"""
    found = list(results)
    query_hash = hashlib.sha256(str(query or "").encode("utf-8")).hexdigest()[:16]
    payload = {
        "query_hash": query_hash,
        "scene": scene,
        "domain_count": len(groups),
        "matched_domains": sorted({item.domain for item in found if item.domain}),
        "shortlist": [
            {"name": item.name, "domain": item.domain, "version": item.version}
            for item in found[:15]
        ],
        "loaded_count": len(session.loaded_tools) if session else len(found),
    }
    try:
        from app.monitoring.context import MonitorContext
        from app.monitoring.logger import monitor_logger

        monitor_logger.info(
            "工具动态发现完成",
            event_type="tool_discovery",
            category="tool_selection",
            code="TOOL_DISCOVERY",
            context=MonitorContext(
                job_id=job_id or None,
                execution_id=job_id or None,
                user_id=user_id or None,
                component="skill_discovery",
            ),
            metadata=payload,
        )
    except Exception:
        pass
    return payload
