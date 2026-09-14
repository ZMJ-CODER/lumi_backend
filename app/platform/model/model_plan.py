"""ModelPlan：请求/任务级**冻结**的模型配置（方案 §七）。

问题：长任务会经过 Planner → Skill → Tool → Worker → 恢复/重试。如果管理员在
任务执行到一半时改了 Redis/.env，前半段用 A 模型、后半段用 B 模型，会带来

* 计划与执行能力不一致（例如 cheap 规划、reasoning 执行）；
* 同一任务内的用量/成本无法归因；
* 重试后行为不可复现。

做法：在**请求/Job 创建时**解析一次，得到 ``ModelPlan``：

* 记录每个角色 → 档位 → 模型名/provider/配置来源（**版本化、可审计**）；
* 密钥只在短期运行态（Redis，TTL 与任务上限对齐）出现，**绝不落库/落 SSE/落 Job 快照**；
* 任务执行期间（含重试、恢复）统一读这份计划，不再重新解析配置；
* 新任务才会拿到新配置。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from app.platform.model import model_roles

#: 任务级冻结配置的运行态键（只存**非密钥**部分 + 短期密钥副本）。
PLAN_KEY = "llm_plan:{plan_id}"
PLAN_TTL_SECONDS = 6 * 3600
PLAN_VERSION = 1


@dataclass(frozen=True, slots=True)
class ModelPlan:
    """一份冻结的模型计划（``public_dict`` 可安全落快照/审计）。"""

    plan_id: str = ""
    version: int = PLAN_VERSION
    created_at: float = 0.0
    byok: bool = False
    scene: str = ""
    #: role → {"profile", "provider", "model", "source", "byok", "timeout", "capabilities"}
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Model Capability Router 的结构化结论（**开关关闭时恒为空字典**；
    #: 形状见 app/platform/model/model_capability_router.py::MODEL_ROUTING_KEYS）。
    #: 只在**冻结时**写一次：任务执行期间这份计划不再变。
    model_routing: dict[str, Any] = field(default_factory=dict)

    def resolve(self, role: str) -> dict[str, Any]:
        """取某角色的冻结配置（缺失时回退该角色的默认档位解析结果）。"""
        name = model_roles.normalize_role(role)
        entry = self.roles.get(name)
        if entry is None:
            fallback = self.roles.get(model_roles.ROLE_DIRECT_ANSWER) or {}
            entry = dict(fallback)
            entry["frozen_missing"] = True
        return dict(entry)

    def public_dict(self) -> dict[str, Any]:
        """Job 快照 / SSE 元数据（**无密钥**，只给角色→档位→模型）。"""
        return {
            "plan_id": self.plan_id,
            "version": int(self.version),
            "created_at": self.created_at,
            "byok": self.byok,
            "scene": self.scene,
            "roles": {
                role: {
                    "profile": entry.get("profile", ""),
                    "provider": entry.get("provider", ""),
                    "model": entry.get("model", ""),
                    "source": entry.get("source", ""),
                }
                for role, entry in self.roles.items()
            },
        }

    def summary(self) -> str:
        """一行可读摘要（过程气泡/日志用；不含密钥）。"""
        roles = sorted(self.roles.items())
        return " · ".join(f"{role}={entry.get('model', '')}" for role, entry in roles if entry.get("model"))


def _runtime_key(plan_id: str) -> str:
    return PLAN_KEY.format(plan_id=plan_id)


async def build_model_plan(
    *,
    scene: str = "office",
    user_id: str | None = None,
    byok_key: str | None = None,
    roles: tuple[str, ...] | None = None,
    plan_id: str = "",
    requirements: Any = None,
) -> ModelPlan:
    """解析并冻结一份模型计划（创建任务时调用一次）。

    ``requirements``（``CapabilityRequirements``，可选）只在
    ``MODEL_CAPABILITY_ROUTER_V2`` 打开时生效：Model Capability Router 会按能力需求
    过滤候选、必要时换档，并把**结构化结论**写进计划（``plan.model_routing``）。
    关闭开关时本参数被完全忽略，行为与改造前逐字一致。
    """
    wanted = roles or model_roles.ALL_ROLES
    resolved: dict[str, dict[str, Any]] = {}
    byok = False
    for role in wanted:
        try:
            item = await model_roles.resolve_role(
                role, scene=scene, user_id=user_id, request_api_key=byok_key
            )
        except Exception as exc:  # noqa: BLE001 - 单个角色解析失败不能毁掉整个计划
            logger.warning("[model-plan] 角色 {} 解析失败: {}", role, str(exc)[:120])
            continue
        byok = byok or item.byok
        resolved[role] = {
            **item.public_dict(),
            "timeout": item.timeout,
            "capabilities": dict(item.capabilities),
        }
    plan = ModelPlan(
        plan_id=plan_id or f"plan_{uuid.uuid4().hex[:16]}",
        version=PLAN_VERSION,
        created_at=time.time(),
        byok=byok,
        scene=str(scene or ""),
        roles=resolved,
    )
    plan = await _refine_with_capability_router(
        plan, requirements=requirements, scene=scene, user_id=user_id
    )
    await _remember_runtime(plan, user_id=user_id, byok_key=byok_key)
    return plan


async def _refine_with_capability_router(
    plan: ModelPlan,
    *,
    requirements: Any,
    scene: str,
    user_id: str | None,
) -> ModelPlan:
    """开关打开时，用 Model Capability Router 的结论冻结"角色 → 实际档位/模型"。

    * 开关关闭 / 没有需求 → 原计划**原样返回**（逐字等价于改造前）；
    * 阻断结论也一并冻结（结论里的 ``blocked=True`` 由调用方负责不派发）；
    * 只改"无工具/审批依赖"的角色（见 ``refine_roles``），工具类角色不动。
    """
    if requirements is None:
        return plan
    from app.platform.model.model_capability_router import model_capability_router

    if not model_capability_router.enabled():
        return plan
    try:
        conclusion = await model_capability_router.route(
            plan=plan, requirements=requirements, scene=scene, user_id=user_id
        )
    except Exception as exc:  # noqa: BLE001 - 路由故障不能阻断任务提交
        logger.warning("[model-plan] 能力路由降级（沿用解析结果）: {}", str(exc)[:160])
        return plan
    if not conclusion.switched:
        return replace(plan, model_routing=conclusion.to_routing())
    return replace(
        plan,
        roles=model_capability_router.refine_roles(plan, conclusion),
        model_routing=conclusion.to_routing(),
    )


async def _remember_runtime(plan: ModelPlan, *, user_id: str | None, byok_key: str | None) -> None:
    """把计划写进短期运行态（Redis，TTL 6h）。

    只保存角色→档位/模型/端点与**当次请求带来的**密钥（BYOK）；服务端密钥不复制，
    由 ``model_roles`` 在调用时按优先级现取——这样"管理员换 key"不需要重建计划。
    """
    if not plan.plan_id:
        return
    try:
        from app.core.redis import get_redis

        payload = {
            "plan": plan.public_dict(),
            "timeouts": {role: entry.get("timeout") for role, entry in plan.roles.items()},
            "capabilities": {role: entry.get("capabilities") for role, entry in plan.roles.items()},
            "user_id": str(user_id or ""),
            "byok_key_present": bool(byok_key),
        }
        if plan.model_routing:
            # 能力路由结论随计划一起冻结（开关关闭时该键不存在 → 运行态载荷逐字不变）。
            payload["model_routing"] = dict(plan.model_routing)
        await get_redis().set(
            _runtime_key(plan.plan_id), json.dumps(payload, ensure_ascii=False), ex=PLAN_TTL_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - 运行态写入失败不影响任务
        logger.debug("[model-plan] 运行态写入失败（降级为不冻结密钥）: {}", str(exc)[:120])


async def load_model_plan(plan_id: str) -> ModelPlan | None:
    """读回冻结计划（Job 恢复/重试/Worker 侧使用）。"""
    target = str(plan_id or "")
    if not target:
        return None
    try:
        from app.core.redis import get_redis

        raw = await get_redis().get(_runtime_key(target))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[model-plan] 读取失败: {}", str(exc)[:120])
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    public = payload.get("plan") if isinstance(payload, dict) else None
    if not isinstance(public, dict):
        return None
    roles: dict[str, dict[str, Any]] = {}
    for role, entry in (public.get("roles") or {}).items():
        if not isinstance(entry, dict):
            continue
        roles[str(role)] = {
            **entry,
            "timeout": (payload.get("timeouts") or {}).get(role),
            "capabilities": (payload.get("capabilities") or {}).get(role) or {},
        }
    return ModelPlan(
        plan_id=str(public.get("plan_id") or target),
        version=int(public.get("version") or PLAN_VERSION),
        created_at=float(public.get("created_at") or 0.0),
        byok=bool(public.get("byok")),
        scene=str(public.get("scene") or ""),
        roles=roles,
        model_routing=dict(payload.get("model_routing") or {}),
    )


async def model_plan_llm_config(plan: ModelPlan, role: str) -> dict[str, Any]:
    """冻结计划 + 当前密钥 → 可直接传给 ``LLMClient`` 的 ``llm_config``。

    这样执行链（Planner/Skill/Tool/Worker）只要拿到 ``plan_id``，就会：
    用**冻结的模型/端点**、**当前服务端密钥**（或请求 BYOK 密钥）发请求，
    因此"中途改配置"不会影响正在跑的任务，而"换 key"仍然立刻生效。
    """
    entry = plan.resolve(role)
    if not entry:
        return {}
    cfg: dict[str, Any] = {
        "provider": entry.get("provider", ""),
        "model": entry.get("model", ""),
        "base_url": entry.get("base_url", ""),
        "timeout": entry.get("timeout") or 120.0,
        "source": f"model_plan:{plan.plan_id}",
        "byok": bool(entry.get("byok")),
    }
    api_key = ""
    if not cfg["byok"]:
        # 服务端密钥按档位现取（管理员轮换 key 后无需重建计划）。
        try:
            resolved = await model_roles.resolve_role(entry.get("role") or role)
            api_key = resolved.api_key
            if not cfg["model"]:
                cfg["model"] = resolved.model
            if not cfg["base_url"]:
                cfg["base_url"] = resolved.base_url
        except Exception as exc:  # noqa: BLE001
            logger.debug("[model-plan] 现取服务端密钥失败: {}", str(exc)[:120])
    if api_key:
        cfg["api_key"] = api_key
    return cfg


def plan_from_routing(routing: dict | None) -> dict[str, Any]:
    """从 Job ``routing`` 里取出公开计划（无密钥），供前端诊断展示。"""
    if not isinstance(routing, dict):
        return {}
    plan = routing.get("model_plan")
    return plan if isinstance(plan, dict) else {}


def profile_user_label(profile: str) -> str:
    """档位 → 面向普通用户的三档显示（不暴露内部命名与模型名）。

    前端 ``PROFILE_USER_LABELS`` 的同义词表（服务端唯一的"人话"映射），
    Model Capability Router 的 process 文案也用它，避免两处措辞漂移。
    """
    name = model_roles.normalize_profile(profile)
    if name == model_roles.PROFILE_CHEAP:
        return "快速模型"
    if name == model_roles.PROFILE_REASONING:
        return "深度模型"
    if name == model_roles.PROFILE_VISION:
        return "视觉模型"
    return "标准模型"


def role_label(role: str) -> str:
    """面向普通用户的三档显示（不暴露内部细节与密钥）。"""
    return profile_user_label(model_roles.role_profile(role))


def default_plan_roles() -> tuple[str, ...]:
    """默认冻结哪些角色：一次任务里可能用到的全部（保证一致性）。"""
    return model_roles.ALL_ROLES


__all__ = [
    "PLAN_KEY",
    "PLAN_TTL_SECONDS",
    "PLAN_VERSION",
    "ModelPlan",
    "build_model_plan",
    "default_plan_roles",
    "load_model_plan",
    "model_plan_llm_config",
    "plan_from_routing",
    "profile_user_label",
    "role_label",
]
