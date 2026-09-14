# 能力子系统两代实现：行为对照与裁决建议（结构重构 P2 交付物）

方案 §六把这件事列为**悬置决策**：`catalog.py` vs `resource_catalog.py`、
`dispatch.py` vs `resource_dispatch.py` —— "不按新旧阶段判定胜负，先做行为对比：
能力条目差异、Provider 映射差异、预检结果差异、Broker 路由差异、测试覆盖差异。
确认等价后指定权威实现"，并且"P3 抽包前必须裁决"。

本文是那次对照的**结果**。结论一句话：

> **两代不等价，但也不冲突**：四个可对拍维度逐条相等，新一代是严格超集，
> 差异集中在"新代独有的资源类型/Provider"与**三处结构性缺口**上。
> 因此**现在不能把任何一代切成唯一权威**，切换前必须先补缺口，再按开关逐阶段切换。

> **进展（2026-09）**：缺口 1（Broker 收窄）、2b（`execution_env` 工作侧对齐）、
> 2d（`native_action` 类别）、2a（memory 改成"仅声明"）、**2c（office_document 独立 id +
> 兼容期）** 与**缺口 3（旧测试迁移，批次 1–4）** 全部关闭。
>
> **排期裁决的落地顺序**：先收口事实不一致（2a）→ 再迁测试加护栏（缺口 3 四批）→
> 最后动跨端可见协议（2c，**后端一侧完成兼容迁移，桌面端可选跟进**）；
> 补真 `memory_provider` 仍作为独立功能（P3）另行推进，不混在收口任务里。

> 代码位置按 P2 之后的路径写；括注旧路径便于对照
> （`catalog.py → catalog/legacy.py`、`builtin.py → registry/builtin.py`、
> `tool_registry.py → catalog/tool_registry.py`、`broker.py → broker/broker.py`、
> `resource_dispatch.py → broker/resource_dispatch.py` 等）。

---

## 1. 对拍证据（最强的一条）

`catalog.tool_registry.shadow_compare()` 在真实注册表上实测
（P2 收尾后该函数位于 `views/tool_shadow.py`，结论逐字未变）：

| 对拍维度 | 差异行 |
| --- | --- |
| `tool→capability` | **0** |
| `capability→mcp_target` | **0** |
| `intent→tool_window` | **0** |
| `tool→risk_tier` | **0** |
| `intent→resource_window`（资源层，披露项） | 1：`['CREATE', 'workspace_write', 'workspace_navigator,workspace_write']` |
| `tool→model_surface`（收敛面，披露项） | 6 |

`shadow_parity_totals()` → `{parity_total: 0, declared_total: 7, missing_dimensions: [], switch_safe: True}`。

**怎么读这张表**：前四个维度是"打开 `TOOL_REGISTRY_DERIVED` 后行为会不会变"的判据，
全为 0 意味着**派生结果与静态表逐条一致**；后两个是**披露**维度（资源层窗口、收敛面），
它们**本来就不该相等**（新层刻意多给读取入口、刻意收敛模型可见名），所以不计入 `switch_safe`。
判定"missing dimension ⇒ unsafe"的规则见 `shadow_parity_totals`。

---

## 2. 逐维度差异

### 2.1 能力条目

| | 旧一代（`catalog/legacy.py` + `registry/builtin.py` + `broker/dispatch.py`） | 新一代（`catalog/resource.py`） |
| --- | --- | --- |
| 能力名 | 9 个扁平名：`workspace.read/write/edit/move/delete`、`code.execute`、`code.scan`、`git.operations`、`artifact.create` | 7 个统一名：`resource.read/write/edit/move/delete`、`code.execute`、`artifact.create`（+ 别名 `sandbox.run→code.execute`、`resource.search→resource.read`） |
| 资源类型 | **不存在** | 5 个：`workspace` / `office_document` / `knowledge` / `artifact` / `memory` |
| 工具表 | `TOOL_CAPABILITY_MAP` 25 条（大小写**敏感**）、`IMPLEMENTATION_MAP` 19 条、`CAPABILITY_TOOL_MAP` 8 条 | `TOOL_BINDINGS` 31 条（大小写**不敏感**）、`LEGACY_CAPABILITY_BINDINGS` 9 条、`RESOURCE_PROVIDERS` 7 条 |

集合差（实测）：

* **只在旧**：能力 `code.scan`、`git.operations`（新代折叠进 `resource.read` / `resource.write`）；
* **只在旧的工具名 27 个**（`code_edit`、`desktop_open_app`、`user_clarify`、`workspace_*` …）——
  新代仍能绑定其中 24 个，只有 `desktop_open_app` / `desktop_open_url` / `user_clarify`
  被判 `unknown`（旧代显式 `None` = **本机动作**，见 2.4）；
* **只在新 26 个**（`bash`、`glob`、`grep`、`edit`、`read`、`office_doc_*`、`query_knowledge` …）；
* **旧目录 9/9 能力全部有统一绑定**（无损）。

⚠️ 新代**不是自足实现**：`catalog/resource.py::_static_legacy_capability` 反向读旧表
（`TOOL_CAPABILITY_MAP` / `IMPLEMENTATION_MAP`）来兜底。

### 2.2 工具 → 能力（并排，实测）

| 工具 | 旧 `capability_for_tool` | 新 `binding_for_tool` | 判定 |
| --- | --- | --- | --- |
| `workspace_write/read/edit/move/delete` | `workspace.*` | `resource.*` / `workspace` | 一致（名字不同） |
| `sandbox_run` | `code.execute` | `code.execute` / `workspace` | 一致 |
| `office_doc_read/analyze`、`read_document` | **None** | `resource.read` / `office_document` | 旧代无法表达 |
| `office_doc_edit` | **None** | `resource.write` / `office_document` | 旧代无法表达 |
| `task_memory` | **None** | `resource.write` / `memory`（依赖该工具已注册） | 新代独有 |
| **`workspace_diff`** | `git.operations`（写类，属 `NEVER_FALLBACK`） | **`resource.read`**（`legacy_capability` 仍是 `git.operations`） | **语义分叉：读 / 写** |
| `workspace_commit` | `git.operations` | `resource.write` | 一致（都算写） |
| `workspace_code_scan` | `code.scan` | `resource.read` | 读类一致、能力名不同 |
| `Workspace_Write` / `WORKSPACE_WRITE` | **None**（大小写敏感） | `resource.write` | 解析口径不同 |
| `desktop_open_app/url`、`user_clarify` | **None（显式本机动作）** | 无绑定 → `unknown`（仅登记在 `DEFERRED_TOOL_FAMILIES.ui_orchestration`） | **旧代表达力更强** |
| `mcp__srv__sub__workspace_write` | `workspace.write` | `resource.write` / `workspace` | 一致（都取最后一段） |

### 2.3 Provider 映射

| | 旧 `CapabilityRegistry`（注册后 4 项） | 新 `RESOURCE_PROVIDERS`（7 条声明） |
| --- | --- | --- |
| 项 | `lumi.server.artifact`、`lumi.local.workspace`、`lumi.local.code`（含 `code.scan`）、`lumi.local.git` | `workspace_provider`、`git_provider`、`code_provider`（仅 `code.execute`）、`office_document_provider`、`artifact_provider`、`knowledge_provider`(registered=False)、`memory_provider`(registered=True) |

三处**声明与实现不对齐**（都实测过；`register_builtin_providers()` 注册后
`capability_registry.providers()` 只有 4 项：`lumi.server.artifact` / `lumi.local.workspace`
/ `lumi.local.code` / `lumi.local.git`）：

1. `memory_provider.registered=True`，但上面那 4 项里**没有** `lumi.server.memory`
   → `provider_ids_for("resource.write", "memory")` 返回一个**不存在的注册项**；
2. `code_provider.execution_env="sandbox"`，而 `lumi.local.code` 真实租约是 `deployment=client / plane=client`；
3. `office_document_provider` 借用通用转发标签 `lumi.client.forwarder`（与"任意客户端能力"共用 provider_id）
   ——**已迁移**（缺口 2c，见 §3.6）：现在有自己的 id `lumi.client.office_document`，
   兼容期同时接受旧 id。

候选解析实测：`workspace.read/write → {lumi.local.git, lumi.local.workspace}`、
`resource.edit/move/delete → {lumi.local.workspace}`、`code.execute → {lumi.local.code}`、
`artifact.create → {lumi.server.artifact}`、`resource.read/knowledge → {}`（知识域只有声明）。

### 2.4 预检（工具窗口）

* 开关全关（当前默认）：9 个意图 + 未知意图与静态表**逐条相同**（`TOOL_REGISTRY_DERIVED=True` 也相同）；
* `RESOURCE_CAPABILITY_WINDOW=True`：`CREATE` 变 `(workspace_navigator, workspace_write)`（**补读取入口**）；
* 带 `resource_types=["office_document"]`：`READ/SEARCH → (workspace_navigator, office_doc_read)`、
  `CREATE → (office_doc_read, workspace_write, office_doc_edit)`、`MODIFY/DELETE/MOVE` 把
  `office_doc_read` 提到最前。

`shadow_compare_windows` 实测只有 1 行差异 → 新窗口**只增不减**。
需要裁决的语义点：`CREATE` 在办公文档资源下**派生出 `office_doc_edit`** 是否符合产品预期。

### 2.5 Broker 路由

* `broker/broker.py` **零引用**新层：`select()` 签名里没有 `resource_type` / `provider_ids`，
  只吃 `capability_catalog` + 租约 + 参数校验 → **Broker 层无法按资源类型收窄**；
* 新代唯一接入点是派发适配层：`policy/routing.py::provider_ids_for` → `CapabilityDispatchAdapter.dispatch(provider_ids=…)`
  → `broker/dispatch.py`（收窄为空时**保留收窄前候选**并打 debug 日志），且仅在
  `RESOURCE_CAPABILITY_DISPATCH=true` 且 `skills/capability_route.py` 解析出结构化目标时生效；
* 实测分歧：两条租约都广告 `code.execute` 时，旧（不收窄）选心跳最新的
  `lumi.local.workspace`，新（收窄到 `{lumi.local.code}`）选 `lumi.local.code`。

**这一条是"两代永不天然等价"的根因**：只要 Broker 不认识资源类型，新旧派发在
"同一能力多 Provider"场景下就会选出不同 Provider。裁决新层之前，必须给
`Broker.select` 加 `resource_type` / `provider_ids`。

### 2.6 测试覆盖

| | 专属测试文件 | 用例数 |
| --- | --- | --- |
| 旧一代 | 19 | ≈307 |
| 新一代 | 7（+3 旗标级） | ≈101 |
| 两代同时引用 | 3 | 56 |

`packages/*/tests` 对本能力层**无覆盖**（唯一命中的 `test_runtime_capabilities.py` 是
`lumi_execution.ResourceDispatcher` 并发原语的假阳性）。

**旧代回归保护约为新代 3 倍**——这是"不能急着删旧代"的最硬理由。切换前应把旧代用例
逐个迁到新实现上跑通，**禁止**删旧测试来"完成"迁移。

### 2.7 调用面

* 生产代码（非测试）：旧代 **32 文件 / 208 处**，新代 **25 文件 / 213 处**，
  **15 个文件两代同时引用**（`catalog/tool_registry.py`、`policy/routing.py`、
  `orchestration/capability_preflight.py`、`skills/base.py`、`api/v1/admin_policies.py` …）；
* `app/agents/capabilities/__init__.py` 只导出旧代公开面，**不导出任何 `resource_*`**；
* 统计必须用**文件系统级搜索**：`git grep` 分别只命中 24/22 个文件，文件系统扫描命中 38/33 个
  （仓库里有 188 个未跟踪文件，其中 `tests/test_resource_*.py`、`tests/capabilities/test_capability_dispatch.py`
  正是新代/旧代的主要回归保护）。

---

## 3. 裁决建议（基于事实，不基于新旧）

| 层 | 建议权威 | 理由 |
| --- | --- | --- |
| 声明与解析（能力名、资源类型、Provider 候选、工具窗口、模型可见面、Workflow 能力声明） | **新一代** | 严格超集（26 个工具新独有、0 个旧能力丢失）、有对拍证据、有安全纠正（`workspace_diff` 按读、不认识就 `unknown` 不猜） |
| Broker / 租约 / 审批 / 执行的**键**与在线路径 | **旧一代**（暂时） | 客户端协议冻结在旧能力名上（两代 docstring 都写明）、Broker 只有旧代实现、旧代回归保护 ≈3×、默认开关就是旧派发在线 |
| 两代并存期 | 维持现状 | 4 个 `RESOURCE_CAPABILITY_*` 开关全 False ⇒ 新层目前只是元数据/对拍/展示，无运行时风险 |

### 三处缺口：现状与影响（缺口 1 已关闭，2/3 待办）

| # | 缺口 | 状态 | 影响面（谁必须配合） |
| --- | --- | --- | --- |
| 1 | Broker 按资源类型收窄 | **已关闭**（见 3.1） | 后端内部；默认不传 = 旧行为，接口零变化 |
| 2b | `code_provider.execution_env="sandbox"` 与真实租约 `plane=client` 不一致 | **已关闭**（见 3.2） | 管理端 Provider 卡片的 `execution_env` 值由 `sandbox` 变 `client`（前端可见） |
| 2d | `desktop_open_app/url`、`user_clarify` 在统一层是 `unknown` | **已关闭**（见 3.2） | 快照新增 `native_action_tools` / `deferred_families` 两个键；这三条工具条目的 `execution_env` 由 `server` 变 `client`（前端可见） |
| 2a | `memory_provider` 声明「已注册」但没有实现 | **已关闭**（裁决：改成未注册，见 3.4） | 管理端 Provider 卡片 `registered`/`provider_id` 变化；条目 `provider_candidates` 变空；过程条目/事件不再带 `provider_id`；**前端 B-1 文案要改成"仅声明 / 待接入"** |
| 3 | 旧代 ≈307 例回归迁到新实现 | **已完成**：批次 1–4（见 3.5） | 纯测试工作；**旧用例只迁不删** |
| 2c | `office_document_provider` 借用通用 `lumi.client.forwarder` | **已关闭（后端兼容迁移，见 3.6）**：新 id `lumi.client.office_document` + 兼容期接受旧 id + 出口加稳定展示字段。桌面端**无需立刻改**；前端请改用 `provider_kind` / `provider_label` | 桌面端可选跟进（双注册或切新 id） |

#### 3.1 缺口 1 已关闭：Broker 收窄（`CapabilityBroker.select`）

**问题**：`select()` 不认识 `resource_type` / `provider_ids`，只有派发适配层
（`CapabilityDispatchAdapter` + `lumi_capability.selection.select_lease`）会收窄。于是
"同一能力有多条租约"时两条路径选出不同 Provider（实测：`lumi.local.workspace` 心跳更新
就赢，而资源声明允许的是 `lumi.local.code`）——"新代是权威"在 Broker 层无法成立。

**改法**（`app/agents/capabilities/broker/broker.py`）：

* `select()` / `invoke()` 新增 `provider_ids` 与 `resource_type` 两个可选入参，
  **默认不传 = 逐字保持收窄前的行为**（既有调用点零影响，无需同步修改）；
* 只给 `resource_type` 时经 `broker/resource_dispatch.provider_ids_for()` 推导
  （与派发层同一份声明，"已注册且有 provider_id 的候选"才算）；
* 收窄只作用在**已经通过绑定/健康/位置过滤的候选**上，且**收窄后为空时保留收窄前候选**
  并打 debug 日志——与内核 `lumi_capability.selection.select_lease` 的"收窄不倒过来卡死"
  是同一条规则。这一条同时兜住了缺口 2a/2c 的"声明与实现不对齐"：错误的收窄集合
  只会让收窄失效，不会让能力变成"不可用"。

**证据**：`tests/capabilities/test_broker_provider_narrowing.py`（6 例）钉住四件事——
默认按心跳、显式 `provider_ids` 收窄、`resource_type` 经统一层推导、空收窄回落；
另有一例证明 `invoke` 与 `select` 是同一口径（否则执行期又会选错 Provider）。

**还没接线的部分（有意留下）**：`POST /capabilities/invoke` 的请求体里**没有**资源类型
字段，因此在线路径上真正的收窄仍只发生在 routing/dispatch 那条链上（`maybe_route_capability`
已经把 `provider_ids` 传给适配层）。给 `/invoke` 加字段是 **wire 变化**，不属于"缺口 1"，
应当和缺口 2a/2c 一起作为"前端可见的变化"统一排期。

#### 3.2 缺口 2b / 2d 已关闭：声明对齐 + `native_action` 类别

**2b：`execution_env` 只回答"哪一侧承接"，不再借用运行方式词汇。**

`code_provider` 曾声明 `execution_env="sandbox"`，而它的真实租约是
`deployment=client / plane=client`——同一个字段在两个维度之间摇摆，管理端于是显示
"代码能力跑在沙箱侧"。现在：

* 字段语义在 `ResourceProviderSpec` 上写死：``client`` = 客户端租约承载，
  ``server`` = 服务端内联；**沙箱是运行方式**，属于租约的 `runtime_kind`；
* `code_provider.execution_env → "client"`，`note` 里保留"客户端沙箱内执行"这一事实；
* 词表由测试钉死：任何 Provider 的 `execution_env` 只能是 `client` / `server`
  （再混回 `sandbox` 会直接红）。

**2d：`native_action` 成为一等类别，不再退化成 `unknown` 噪音。**

旧代对"打开应用/浏览器、向用户澄清"有过**显式决定**：`TOOL_CAPABILITY_MAP` 里标 `None`
= 本机动作、不参与租约。新层原先只把它们算进 `unbound_tools()` 的"未接入"里，而且派生
条目还把工作侧写成 `server`（"在你机器上打开记事本"被标成服务端执行）。现在：

* 新增 `native_action_tools()` / `is_native_action()`：**从旧路由表派生**（标 `None` 的条目），
  新层继承的是那个决定本身，不是又抄一份名单；
* `DEFERRED_TOOL_FAMILIES` 增加 `native_action` 族，这三条工具从 `ui_orchestration`
  （编排原语）挪进来；**族名单与派生集合必须相等**由测试钉住，防止两边漂移；
* `catalog_snapshot()` 新增 `native_action_tools` 与 `deferred_families` 两个键，
  管理端可以把"本机动作（有意不接）"和"漏接的资源工具"分开显示；
* 这三条工具的派生条目 `execution_env` 由 `server` 纠正为 `client`（本机动作在客户端执行）；
* `unbound_tools()` **语义不变**：它们确实没有资源绑定，仍然出现在未绑定清单里
  （既有用例 `test_local_actions_without_capability_are_visible_not_silent` 明确要求如此）——
  变的是"能不能被正确分类"，不是"要不要出现"。

**前端可见的差异（两处值 + 两个新键）**：
`resource_catalog.providers[code_provider].execution_env: sandbox → client`；
`entries[desktop_open_app|desktop_open_url|user_clarify].execution_env: server → client`；
`resource_catalog` 新增 `native_action_tools: string[]`、`deferred_families: {族: 工具名[]}`。
除此之外没有接口形状变化。

**证据**：`tests/capabilities/test_resource_catalog.py` 新增/加强 4 例
（工作侧词表、族与路由表同源、快照键、本机动作不被标成服务端）。

#### 3.3 为什么没有顺手做 2a/2c

2a 是**二选一的产品决定**（补真 Provider vs 改声明），两个方向对前端文案与用户预期
完全不同：补实现要新增能力名（wire 可见，客户端/管理端都要认），改声明则要让
前端 B-1 卡片把 `memory_provider` 也显示成"仅声明"。

2c 需要**桌面端配合**：`office_document_provider` 现在借用通用转发 id
`lumi.client.forwarder`，给它独立 id 意味着客户端注册时要用新 id，而过程条目/事件里
已经发出的 `provider_id` 标签也会跟着变（现有测试正断言旧值）。
这类跨端改动要先定"改成什么"，再和前端/桌面端一起排期。

#### 3.4 缺口 2a 已关闭：memory 明确改成"仅声明 / 待接入"

**裁决**（2026-09）：**不补真 `memory_provider`，先把声明改成未注册。**

理由：补真实现看起来只是"补一个类"，实际会引入一条新能力链路——能力名、权限、
Broker 选择、事件展示、管理端文案，可能还有 memory 数据的安全边界。这属于**独立功能**，
不该作为"两代能力收口"的尾巴顺手做。更要紧的是：**当前最危险的不是"功能少"，
而是"声明说有、运行时没有"**——它会同时污染 Broker 候选、管理端展示、能力发现与测试判断。

改法（`app/agents/capabilities/catalog/resource.py`）：

* `memory_provider` 保留在资源声明里（目录、默认 Provider 映射、能力/资源类型都在），
  但 `registered=False` 且 `provider_id=""`（与 `knowledge_provider` 同形）；
  设计意图（计划 id `lumi.server.memory`）写进 `note`，不再写在一个不存在的字段上；
* **"有没有实现"的判据收敛为一处**：新增
  `registered_providers_for()`（`registered=True` 且有 `provider_id`），
  由三条路径共用——Broker 收窄集合（`provider_ids_for`）、Adapter 首选（`adapter_for`）、
  工具条目的 `provider_candidates`。以前三处各判一次，才会出现"某条路径把待接入当可用"；
* 条目字段分工写清楚：`resource_provider` = **声明的**逻辑 Provider 名（可以只有声明），
  `provider_candidates` = **可用**候选（memory 现在是空）；
* **不新增任何 wire 能力**（没有 `lumi.server.memory` 注册项），也不接进在线派发。

**前端可见的差异**：

| 位置 | 变化 |
| --- | --- |
| `resource_catalog.providers[memory_provider].registered` | `true` → `false` |
| `resource_catalog.providers[memory_provider].provider_id` | `lumi.server.memory` → `""` |
| `entries[task_memory].provider_candidates` | `["memory_provider"]` → `[]` |
| 过程条目/事件里的 memory 标签 | 不再带 `provider_id`（仍带 `capability` / `resource_type` / `provider_name`） |
| B-1 卡片文案 | 需要把 memory 显示成"仅声明 / 待接入"，与 knowledge 一致 |

**证据**：`tests/memory/test_memory_provider_acceptance.py` 整体口径改为"只有声明"，
其中 `test_declaration_only_provider_is_never_claimed_available` 逐条钉住"三个可用性入口
都给空"；另加过程条目/事件标签的反向断言（`provider_id` 不得出现）。

**顺带修掉一个真问题（写批次 1 用例时发现）**：`binding_for_tool("resource.write")`
以前会命中客户端原子工具 `write`——`_tool_key` 取末段是为 `server.tool` / `mcp__srv__tool`
服务的，能力名因此被折成工具名，属于"按名字猜"。现在能力名（统一名与旧名）在
`binding_for_tool` 一律返回 `unknown`，并由批次 1 的参数化用例钉住。

#### 3.5 缺口 3 批次 1 已完成：发现 / 目录 / 窗口的跨代守卫

旧代那 ≈307 例回归**不删**，按四批迁成"新旧共跑 / 新代等价断言"：

| 批次 | 范围 | 状态 |
| --- | --- | --- |
| 1 | 工具发现 / catalog snapshot / capability window | **已完成**：`tests/capabilities/test_two_generation_parity_b1_discovery.py`（15 例） |
| 2 | Broker selection / provider narrowing / dispatch | **已完成**：`tests/capabilities/test_two_generation_parity_b2_broker_dispatch.py`（28 例）+ 缺口 1 的 `test_broker_provider_narrowing.py`（6 例） |
| 3 | preflight / approval / execution gate | **已完成**：`tests/capabilities/test_two_generation_parity_b3_preflight_approval.py`（26 例） |
| 4 | SSE 事件 / 过程日志 / 前端可见 provider 标签 | **已完成**：`tests/capabilities/test_two_generation_parity_b4_events_labels.py`（9 例）+ 2c 的 `test_office_document_provider_migration.py`（12 例） |

批次 1 的做法是**在真实表上全量跑性质**，而不是把旧用例逐条抄一遍：

1. 旧路由表认识的工具，新代必须认识且旧能力名可追溯（`missing == []`）；
2. 新代多出来的绑定必须能追溯到写下来的声明（工具级/能力级映射），
   且旧目录 9 个能力 100% 有统一绑定；
3. 形状可疑/能力名一律**没有结论**（参数化 9 个样例）；
4. 每个意图的资源窗口必须**包含**静态窗口（只增不减），指定资源类型后读入口仍在；
5. 新增一个插件工具后，既有工具的发现结果**一个都不能少**。

批次 2 把同样五条性质搬到**派发面**（选错 Provider 的代价是"这一步交给了不该执行的那一侧"，
所以这一批以**跨代差分**为主）：

1. 能力 → 规范入口、MCP 名 → 能力，在 `TOOL_REGISTRY_DERIVED` **两种状态下逐条相同**
   （开关只能改"谁提供答案"，不能改答案）；`lumi.<tool>` 这种命名空间写法按末段匹配，
   只对不含点的工具名成立（`code.edit` 走 `mcp__…__` 形式）；
2. 收窄只可能**缩小**候选：选中的 Provider 一定在收窄集合里；
3. 形状可疑的名字在派发面一律没有结论；**能力名不是工具名**（两个方向分开断言，
   避免一刀切把能力入口也关掉）；
4. 收窄后为空 → 两条路径都按收窄前候选继续，**在线路径也照常派发**（不是"能力不可用"）；
5. 新增工具不改变既有"能力 → 规范入口"的答案；工具自述**不能**改写静态表里已有的归属。

差分口径：同一条租约列表 + 同一个绑定 + 同一组收窄条件，
`CapabilityBroker.select` 与派发适配层的 `select_lease` 必须选中同一个 Provider
（7 组输入，覆盖"同能力多 Provider"与"收窄排除全部"两类真机场景）。
另外钉住 routing 的 off/shadow 语义：off 连适配层都不碰，shadow 查询打点但
`handled=False`、`result is None`，不认识的工具在 active 模式下也不产生派发尝试。

批次 3 守**安全边界**（错了不是"选得不理想"，而是"该拦的没拦"）：

1. 预检事实与 Broker 的真实选择**逐条一致**（有租约/无租约两种状态都对拍整个能力目录）；
   探测报了事实就**绝不允许**给 READY；事实 → 对外状态只有一处映射，
   且四个 Provider 侧事实的"下一步"各不相同（设备没连 ≠ 工作区绑错）；
2. 审批档位**只紧不松**：静态词表与派生档位对每个已知工具逐条相同；注册表不接管的
   工具必须显式登记为遗留工具（否则"没档位"等于放行）；自述只能收紧
   （把 routine 说成 auto 不生效）；不认识的工具一律 `critical`；
3. 门禁不因新层而放宽：`NEVER_FALLBACK_CAPABILITIES` 全是 `local_only`；
   无租约时写/执行**结构化失败**（调用方"请求回退"也不行），只有只读能力允许回退；
   需要审批的能力缺审批时**一次都不调用客户端**；预检 V2 开关只改文案、不改错误码。

批次 4 守**出口**（前端看到的过程条目与事件流）：

1. 标签是目录事实：两种 flag 状态下逐条相同；`provider_id` 出现时必须在"可用 Provider"
   集合里（只有声明的不许出现）；本机动作/编排原语不填任何标签；
2. 出口只放闭集词汇（形状闸门），带参数/路径/引号的工具名一律丢弃；
3. 能力事件带执行来源（`execution_plane`/`executor_type`）但**绝不带载荷正文**；
4. 历史兼容：老过程条目不带新字段也能解析、老载荷逐字不变、已落盘的 `provider_id` 不被改写；
5. 新增工具不改变既有出口标签。

## 3.6 缺口 2c 已关闭：`office_document_provider` 独立 id（后端兼容迁移）

**问题**：办公文档能力借用通用转发 id `lumi.client.forwarder`，与"任意客户端能力"共用。
`provider_id` 已经进过程条目与事件流，直接改名会让前端展示、fixture 与历史任务回放一起抖。

**做法**（`app/agents/capabilities/catalog/resource.py`）：

```text
声明：provider_id        = lumi.client.office_document
      legacy_provider_ids = ("lumi.client.forwarder",)      ← 兼容期
收窄：provider_ids_for(...) 同时含新旧两个 id ⇒ 用旧 id 注册的租约仍是合法候选
出口：provider_kind / provider_label 两个稳定展示字段 ⇒ 前端不必按物理 id 分支
历史：已落盘的旧 provider_id 原样回放（setdefault），只补稳定字段，不改写事实
```

**桌面端无需立刻改**：旧 id 仍在收窄集合里，注册行为可以原样不动；等桌面端排期时
再切新 id（或双注册），后端不用再改。**没有兼容期尾巴的其它 Provider**
（`legacy_provider_ids == ()`）行为逐字不变，`catalog_snapshot()` 会列出
`legacy_provider_ids` / `accepted_provider_ids`，管理端能看见"这条路还在接旧注册"。

**前端要做的**：把展示分支从 `provider_id` 换成 `provider_kind`（ASCII 闭集，如
`office_document`）与 `provider_label`（后端给的中文文案，如"办公文档能力"）；
`provider_id` 只用于排障。老行（历史步骤）现在也会被补上这两个字段（`dispatch_labels_for_step`
用 `setdefault` 补全），前端不必为老数据单独兜底。

**证据**：`tests/capabilities/test_office_document_provider_migration.py`（12 例：声明/兼容窗口/
兼容 id 的租约照样派发/稳定展示字段/文案闭集/历史不被改写/老载荷可解析）；
`tests/contracts/test_process_dispatch_labels.py` 里那条**不再死断言**
`provider_id === lumi.client.forwarder`，改为断言"办公文档能力被正确路由"。

**契约**：`ProcessLogEntry` 新增 `provider_kind`（闭集闸门）与 `provider_label`
（文案闸门）两个可选字段；TS 类型已重新生成（`packages/contracts/ts/lumi-contracts.d.ts`）。

### 不要做的事

* 不要按"哪个文件新"来决定删谁；
* 不要在 P2/P3 一次改动里同时做"改权威 + 改开关 + 删旧代码"；
* 不要为了让新代自足而去改旧表（`LEGACY_CAPABILITY_BINDINGS` 这类桥梁表的存在是**有意**的）。
