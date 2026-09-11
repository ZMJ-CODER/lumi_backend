"""能力/插件共享词表与严格语义（阶段 0 冻结，后端与内核同源）。

**为什么单独一个文件**：``local_only`` / ``cloud`` / ``hybrid`` 是**安全语义**而不是
描述性标签。把它们写成散落在各处的字符串，等于没有约束；这里把词表、默认值、
兼容规则与"是否允许路由"的判定放在唯一一处，任何调用方都必须经过
:func:`deployment_allows`，不允许自己写 if-else。

三条硬规则（方案第五节）：

* ``local_only``：只能在客户端（用户设备）执行。客户端不可用时**直接结构化失败**，
  **绝不**降级到云端——"本地工作区读取"悄悄上传到云端是数据泄露，不是降级。
* ``cloud``：只能在服务端执行。不允许被路由到客户端 Provider（客户端不能伪造一个
  "服务端"能力来窃取服务端密钥或越权）。
* ``hybrid``：服务端与客户端都允许，但**必须在策略显式允许之后**才能切换位置；
  默认按调用方声明的位置执行，切换要留下 ``routed_by_policy=True`` 的痕迹。
"""

from __future__ import annotations

from enum import StrEnum


class PluginKind(StrEnum):
    """插件类型。未知类型**默认不能执行**（见 ``Extension Handler`` 阶段）。"""

    SKILL_PLUGIN = "skill_plugin"
    CAPABILITY_PROVIDER = "capability_provider"
    POLICY_PACK = "policy_pack"
    VIEW_PLUGIN = "view_plugin"
    EXTENSION_HANDLER = "extension_handler"


#: 本阶段**允许激活**的插件类型。未知 kind 一律拒绝（生产默认关闭扩展）。
ACTIVATABLE_PLUGIN_KINDS: frozenset[str] = frozenset(
    {
        PluginKind.SKILL_PLUGIN.value,
        PluginKind.CAPABILITY_PROVIDER.value,
        PluginKind.POLICY_PACK.value,
        PluginKind.VIEW_PLUGIN.value,
    }
)

#: 只在开发者模式注册、且需要安全审计后才允许激活的类型。
DEVELOPER_ONLY_PLUGIN_KINDS: frozenset[str] = frozenset(
    {PluginKind.EXTENSION_HANDLER.value}
)


class Deployment(StrEnum):
    """插件/Provider 的运行位置。"""

    SERVER = "server"
    CLIENT = "client"
    #: 受限 Worker（官方签名插件：进程内不可信，放独立 Worker/容器）。
    WORKER = "worker"
    #: 仅开发者模式的本地插件（生产禁止任意路径加载）。
    LOCAL_DEV = "local_dev"


class CapabilityStatus(StrEnum):
    """一次能力调用在**前端过程气泡**里的展示状态。

    与 ``ExecutionStatus`` 的关系：后者是执行语义（success/failed/…），本枚举是
    能力**路由与等待**语义（等 Provider、等审批、被拒止）。两者都是稳定词表，
    投影时把执行状态映射进来，前端只渲染。
    """

    IDLE = "idle"
    REQUESTED = "requested"
    WAITING_PROVIDER = "waiting_provider"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


class DataLocality(StrEnum):
    """数据本地性：决定一次能力调用**允许在哪里执行**。"""

    LOCAL_ONLY = "local_only"
    CLOUD = "cloud"
    HYBRID = "hybrid"


class IsolationLevel(StrEnum):
    """执行隔离强度。服务端不得对第三方插件用任意 ``importlib`` 进 API 进程。"""

    IN_PROCESS = "in_process"      # 仅内置插件
    RESTRICTED_WORKER = "restricted_worker"  # 官方签名插件
    SANDBOXED = "sandboxed"        # 第三方插件：独立 Worker/容器
    CLIENT_DEVICE = "client_device"  # 客户端 Provider（本机执行 + 本地拒止）


class TrustLevel(StrEnum):
    """信任级别（签名与来源决定，不由插件自述决定）。"""

    BUILTIN = "builtin"
    OFFICIAL = "official"          # 官方签名
    THIRD_PARTY = "third_party"
    LOCAL_DEV = "local_dev"        # 仅开发者模式


class SideEffectKind(StrEnum):
    """副作用类别：决定是否需要审批、能否重放。"""

    NONE = "none"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    NETWORK = "network"
    EXTERNAL = "external"          # 对外发送/发布等不可逆外部效果


#: 需要人工审批的副作用类别（服务端审批 + 客户端本机确认）。
APPROVAL_REQUIRED_SIDE_EFFECTS: frozenset[str] = frozenset(
    {
        SideEffectKind.WRITE.value,
        SideEffectKind.DELETE.value,
        SideEffectKind.EXECUTE.value,
        SideEffectKind.EXTERNAL.value,
    }
)


class ProviderHealth(StrEnum):
    """Provider 健康状态（租约心跳维护）。

    ``OFFLINE`` 是"租约还在但客户端已断开"（断线可恢复）；
    ``UNHEALTHY`` 是"进程在但功能不可用"（需要重启/修复）。
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


def parse_plugin_kind(value: object) -> PluginKind | None:
    """解析插件类型；未知类型返回 ``None``（调用方必须显式拒绝，不得默认放行）。"""
    key = str(getattr(value, "value", value) or "").strip().casefold()
    for item in PluginKind:
        if item.value == key:
            return item
    return None


def parse_data_locality(value: object) -> DataLocality:
    """解析数据本地性；未知/空值按**最保守**的 ``local_only`` 处理。

    宁可让云端能做的工作被拒，也不能让本地数据被误判成可以出网。
    """
    key = str(getattr(value, "value", value) or "").strip().casefold()
    for item in DataLocality:
        if item.value == key:
            return item
    return DataLocality.LOCAL_ONLY


def isolation_for_trust(trust: TrustLevel | str) -> IsolationLevel:
    """信任级别 → 允许的最弱隔离（服务端不得给第三方插件进程内权限）。"""
    key = str(getattr(trust, "value", trust) or "").strip().casefold()
    if key == TrustLevel.BUILTIN.value:
        return IsolationLevel.IN_PROCESS
    if key == TrustLevel.OFFICIAL.value:
        return IsolationLevel.RESTRICTED_WORKER
    if key == TrustLevel.LOCAL_DEV.value:
        return IsolationLevel.RESTRICTED_WORKER
    return IsolationLevel.SANDBOXED


def deployment_allows(
    locality: DataLocality | str,
    deployment: Deployment | str,
    *,
    policy_allows_switch: bool = False,
) -> bool:
    """``数据本地性 × 运行位置`` 是否允许（唯一判定处）。

    * ``local_only`` → 仅 ``client``；服务端一律拒绝，且**不提供**降级到云端的路径；
    * ``cloud`` → 仅 ``server``；
    * ``hybrid`` → 两边都允许，但服务端→客户端（或反向）的**切换**需要
      ``policy_allows_switch=True``；同侧执行不需要额外授权。
    """
    where = parse_data_locality(locality)
    site = str(getattr(deployment, "value", deployment) or "").strip().casefold()
    if site not in {item.value for item in Deployment}:
        return False
    # ``local_dev`` 是**客户端侧**的开发者模式加载：按本地位置对待（生产默认拒绝
    # 由安装期把关，见 Installer 的 local_plugin_path 规则）。
    client_sites = {Deployment.CLIENT.value, Deployment.LOCAL_DEV.value}
    if where is DataLocality.LOCAL_ONLY:
        return site in client_sites
    if where is DataLocality.CLOUD:
        return site == Deployment.SERVER.value
    # hybrid
    if site == Deployment.SERVER.value:
        return policy_allows_switch
    return site in client_sites | {Deployment.WORKER.value}


__all__ = [
    "ACTIVATABLE_PLUGIN_KINDS",
    "APPROVAL_REQUIRED_SIDE_EFFECTS",
    "CapabilityStatus",
    "DEVELOPER_ONLY_PLUGIN_KINDS",
    "DataLocality",
    "Deployment",
    "IsolationLevel",
    "PluginKind",
    "ProviderHealth",
    "SideEffectKind",
    "TrustLevel",
    "deployment_allows",
    "isolation_for_trust",
    "parse_data_locality",
    "parse_plugin_kind",
]
