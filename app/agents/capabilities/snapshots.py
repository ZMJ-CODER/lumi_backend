"""阶段 2：把插件/能力/策略快照写进 Job（``routing["*_snapshot"]``）。

为什么进 Job 而不是只放全局注册表："这次任务当时用的是哪个 Provider 版本、哪台设备、
哪个契约版本"是**审计事实**，刷新/重试/回滚之后都要能解释。全局状态只能回答"现在有
什么"，回答不了"当时用了什么"。

写入位置遵循既有约定：``routing`` 只存路由/策略/审计摘要（与
``route_snapshot`` 的 MANAGED_KEYS 不冲突），不放过程日志与正文。
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from lumi_contracts.plugins import PluginSnapshot, PolicyRef

#: routing 里的快照键（前端 run_view 与审计读这三个）。
ROUTING_PLUGIN_SNAPSHOT = "plugin_snapshot"
ROUTING_CAPABILITY_SNAPSHOT = "capability_snapshot"
ROUTING_POLICY_SNAPSHOT = "policy_snapshot"

#: 快照规模上限（避免把 routing 撑大；容量按"同时活跃能力数"预算）。
MAX_SNAPSHOT_ROWS = 64


def policy_refs(*, approval_mode: str = "", execution_mode: str = "", policy_id: str = "") -> list[PolicyRef]:
    """由既有策略事实生成策略快照（阶段 5 的 Policy Pack 在此登记版本）。

    ``policy_id`` 由 ``select_policy_id`` 决定并写入 routing：快照必须能回答"当时用的
    是哪个策略包、什么版本、能否切云端"，否则回滚/复盘时无从解释行为差异。
    """
    rows: list[PolicyRef] = []
    if policy_id:
        try:
            from app.agents.capabilities.policy_packs import policy_packs

            pack = policy_packs.get(policy_id)
            rows.append(
                PolicyRef(
                    id=str(policy_id),
                    version=pack.version if pack is not None else "",
                    source="builtin" if pack is not None else "unknown",
                )
            )
        except Exception:  # noqa: BLE001 - 快照失败不影响任务
            rows.append(PolicyRef(id=str(policy_id), source="unknown"))
    if approval_mode:
        rows.append(PolicyRef(id=str(approval_mode), version="1.0.0", source="builtin"))
    if execution_mode:
        rows.append(PolicyRef(id=str(execution_mode), version="1.0.0", source="builtin"))
    return rows


def build_job_snapshots(
    *,
    approval_mode: str = "",
    execution_mode: str = "",
    policy_id: str = "",
    leases: list[Any] | None = None,
    capabilities: list[Any] | None = None,
) -> dict[str, Any]:
    """构造三个快照（缺数据时给出空结构，不写半截字段）。"""
    snapshot = PluginSnapshot.from_leases(
        leases=list(leases or []),
        capabilities=list(capabilities or []),
        policies=policy_refs(
            approval_mode=approval_mode, execution_mode=execution_mode, policy_id=policy_id
        ),
    )
    return {
        ROUTING_PLUGIN_SNAPSHOT: snapshot.to_snapshot(),
        ROUTING_CAPABILITY_SNAPSHOT: snapshot.capability_snapshot()[:MAX_SNAPSHOT_ROWS],
        ROUTING_POLICY_SNAPSHOT: snapshot.policy_snapshot(),
    }


def attach_job_snapshots(
    routing: dict[str, Any],
    *,
    approval_mode: str = "",
    execution_mode: str = "",
    policy_id: str = "",
    capability_broker: Any = None,
) -> dict[str, Any]:
    """把当前活跃能力租约写进 ``routing``（原地更新并返回）。

    ``capability_broker`` 缺省用进程内共享 Broker；取快照失败只记日志，绝不阻断提交
    （快照是审计信息，不是执行前提）。
    """
    try:
        if capability_broker is None:
            from app.agents.capabilities.broker import capability_broker as shared

            capability_broker = shared
        leases = capability_broker.leases.snapshot()
        catalog = list(capability_broker.catalog.all())
        routing.update(
            build_job_snapshots(
                approval_mode=approval_mode,
                execution_mode=execution_mode,
                policy_id=policy_id,
                leases=leases,
                capabilities=catalog,
            )
        )
    except Exception as exc:  # noqa: BLE001 - 审计快照失败不影响任务
        logger.warning("[capability] 快照写入失败（降级）: {}", str(exc)[:160])
    return routing


__all__ = [
    "MAX_SNAPSHOT_ROWS",
    "ROUTING_CAPABILITY_SNAPSHOT",
    "ROUTING_PLUGIN_SNAPSHOT",
    "ROUTING_POLICY_SNAPSHOT",
    "attach_job_snapshots",
    "build_job_snapshots",
    "policy_refs",
]
