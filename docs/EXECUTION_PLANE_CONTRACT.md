# 执行位置 / 运行方式契约（execution_plane + runtime_kind）

> 解决什么问题：``deployment`` 与 ``isolation`` 把"在哪一侧执行"和"用什么隔离方式"混在
> 一起，``worker`` 既可能是服务端受限 Worker 也可能是客户端 Worker —— 前端读到
> ``provider.executor_type`` 也无法稳定回答"到底谁在执行"。现在拆成两个正交字段。

## 1. 词表

| 字段 | 取值 | 含义 |
|---|---|---|
| `execution_plane` | `server` \| `client` | 执行发生在服务端还是用户设备 |
| `runtime_kind` | `in_process` \| `worker` \| `container` \| `sandbox` | 具体运行/隔离方式 |

派生的**兼容字段** `executor_type`（前端既有读法，继续保留）：

| execution_plane | runtime_kind | executor_type |
|---|---|---|
| server | in_process | `server` |
| client | in_process | `client` |
| server/client | worker | `worker` |
| server/client | container | `container` |
| server/client | sandbox | `sandbox` |

前端展示示例：`execution_plane=server` + `runtime_kind=worker` → **"隔离 Worker · 服务端"**；
`execution_plane=client` + `runtime_kind=worker` → **"隔离 Worker · 客户端"**。

旧字段的映射（缺省推导，显式声明优先）：

* `deployment` → `execution_plane`：`server`/`worker` → `server`；`client`/`local_dev` → `client`
  （旧 `worker` 语义含糊，按服务端受限 Worker 处理，建议调用方显式声明 `execution_plane`）；
* `isolation` → `runtime_kind`：`in_process` → `in_process`、`restricted_worker` → `worker`、
  `sandboxed` → `container`、`client_device` → `worker`。

未知值一律保守折叠（位置 `client`、运行方式 `in_process`），不会因为拼错而"看起来像
服务端进程内"。

## 2. 贯穿链路（实现位置）

| # | 环节 | 落点 | 说明 |
|---|---|---|---|
| 1 | Provider 注册 | `CapabilityRegistry.register(execution_plane=, runtime_kind=)` → `ProviderRegistration.to_snapshot()` | 声明值；`deployment` 仍作为兼容输入 |
| 2 | ProviderLease | `ProviderLease.execution_plane/runtime_kind` + `plane()/runtime()/executor_type()` | **本次租约实际绑定**；客户端注册/心跳可上报并可纠正；Redis 编解码保留 |
| 3 | CapabilitySelection | `CapabilitySelection.execution_plane/runtime_kind` + `to_snapshot()` | Broker 最终选中了哪个 Provider（取自租约，不重新猜） |
| 4 | CapabilityResult | `CapabilityResult.execution_plane/runtime_kind` + `plane()/runtime()/executor_type()` | **实际执行来源**；派发路由 `route` 里也带一份，供客户端对账 |
| 5 | 审计 / Job 快照 | `CapabilityAuditRecord`、`PluginSnapshot.ProviderRef/CapabilityBinding`、`broker.capability_snapshot()`、`lease.to_snapshot()` | 保存当时的真实值；刷新/重试/复盘都能解释"当时谁在执行" |
| 6 | 插件 Manifest | `PluginManifest.declared_plane()/declared_runtime()` + `to_snapshot()` | **只当声明**：快照里同时给 `declared_*` 与实际派生值，绝不把声明当执行结果 |

客户端上报入口（`POST /capabilities/register` 与 `/heartbeat`）：

```jsonc
{
  "providers": [
    {
      "provider_id": "lumi.local.workspace",
      "execution_plane": "client",   // 可选：Provider 级
      "runtime_kind": "worker",      // 可选：Provider 级
      "capabilities": [
        { "capability": "workspace.read@1", "execution_plane": "client", "runtime_kind": "in_process" }
      ]
    }
  ]
}
```

粒度优先级：**能力级 > Provider 级 > 请求级 > 按 deployment 推导**。只给
`executor_type` 也能被接受（会解析回两个字段），但正式契约以两个字段为准。

## 3. 读取面（后端已经给出这些字段的地方）

* `GET /api/v1/capabilities`：`catalog[]`（能力声明）、`leases[]`（我的租约）、
  `providers[]`（注册项快照）三处都带 `execution_plane` / `runtime_kind` / `executor_type`；
* `GET /api/v1/capabilities/health`：`leases[]` 同上；
* `GET /api/v1/capabilities/dispatch-map`：工具↔能力对照（派发一律按租约）；
* 能力 SSE 事件：`capability_*` / `approval_required` 帧带 `execution_plane` /
  `runtime_kind` / `executor_type`（白名单已放行这三个字段）；
* Job 快照：`run_view.plugin_snapshot.providers[]`、`run_view.capability_snapshot[]`
  都带实际执行来源；`run_view.operation_summary` 见 `docs/WORKSPACE_OPERATIONS.md`；
* 契约版本：`packages/contracts/ts/lumi-contracts.d.ts` 已重新生成
  （`ExecutionPlane` / `RuntimeKind` 类型 + 各接口字段），前端直接引用即可。

## 4. 语义边界

* 一个 Provider / 一条租约声明**一对** (plane, runtime)。同一个 Provider 若两侧都能执行，
  按侧各注册一条租约（这正是"按租约派发"的既有语义），不引入"集合型声明"。
* `execution_plane=server` + `runtime_kind=in_process` 表示"在 API 进程内执行"；
  内置服务端 Provider（如 `artifact.create`）用它。第三方服务端插件必须用
  `container`（或更严），客户端插件宿主用 `worker`。
* 工作区操作（`workspace.write/edit/move/delete`）由**服务端操作网关**编排
  （`execution_plane=server` + `in_process`），原子落盘仍由客户端原子工具执行；
  客户端侧的执行事实体现在租约/路由（`provider_id` / `device_id`）上，两者不混为一谈。
