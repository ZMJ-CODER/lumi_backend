# 统一工具注册表（方案《工具发现链路》P1）

目标：把"同一个工具的事实散在五张静态表里"收敛成**一个查询入口**，让新增工具不再需要
人肉同步多处；同时**默认不改行为**，用影子对比证明派生与静态一致之后再切真相源。

## 之前的问题（为什么必须收敛）

同一个工具的事实分散在五处，漏改一处就会出现
"MCP 能发现 → 目录能看到 → 预检不认识 → Broker 不知道对应能力 → 执行时变成未知工具"：

| 位置 | 表 | 回答的问题 |
| --- | --- | --- |
| `capabilities/builtin.py` | `TOOL_CAPABILITY_MAP` | 工具 → 能力 |
| `capabilities/catalog.py` | `IMPLEMENTATION_MAP` | 实现名 → 能力 |
| `capabilities/dispatch.py` | `CAPABILITY_TOOL_MAP` | 能力 → 首选 MCP 工具 |
| `orchestration/capability_preflight.py` | `ACTION_TOOL_WINDOW` | 动作意图 → 工具窗口 |
| 各 Skill / manifest | `allowed_tools` / 声明 | 可见场景、审批、执行环境 |

## 现在：一个入口，八个派生点

新模块 `app/agents/capabilities/tool_registry.py`：

```text
ToolSpec（影子注册表）
  + CapabilityDescriptor（数据本地性 / 副作用 / 权限 / 契约版本）
  + ToolCapability（场景 / 审批 / 环境 / 确认模式）
  + Tool / PluginManifest 的档位声明（risk_tier / approval_policy / side_effects）
        │
        ▼
  ToolRegistryEntry ── capability / provider_id / mcp_target / action_intents
                       / approval_policy / risk_tier / execution_env / scenes
```

查询入口：

* `resolve_tool(name)` —— 兼容裸名 / `mcp__server__tool` / `server.tool`；
* `entries_by_name()` / `build_registry_entries(capabilities=None)`；
* `capability_of(tool)` / `mcp_target_for(capability)` / `action_window(intent, fallback=…)`；
* `risk_tier_of(tool, arguments, capability)` / `approval_policy_of(tool, capability)`。

四个调用点已经改为经注册表查询（开关关闭时**逐字**走原静态表）：

* `builtin.capability_for_tool`（工具 → 能力）
* `dispatch.mcp_tool_for_capability`（能力 → MCP 目标）
* `capability_preflight.tool_window_for_actions`（动作意图 → 窗口）
* `admin_policies` 新增只读视图 `GET /api/v1/admin/policies/tools`

## 审批档位：声明一次，全链路生效（三档 A/B/C）

档位词表只有一份（注册表），审批引擎 `approval_policy.py` 在开关打开时**不再自己判断**，
而是问注册表。派生的优先级被刻意设计成"**可信数据 > 自述声明，且自述只能收紧**"：

| 顺序 | 来源 | 可不信 |
| --- | --- | --- |
| 1 | `TOOL_TIER_OVERRIDES`（工具级例外，如 `workspace_diff` 只读 → A） | 我们的数据 |
| 2 | `CAPABILITY_TIER`（能力级，如 `git.operations` → C） | 我们的数据 |
| 3 | `CapabilityDescriptor.side_effects` + `needs_local_confirmation`（插件新能力） | Provider 声明 |
| 4 | `Tool.risk_tier` / `ToolCapability.risk_tier` / `PluginManifest.side_effects`（自述） | 只能**收紧**上面的结论 |
| 5 | 都没有 | 保守判 C 档 |

三个声明渠道都进了注册表：

1. **插件工具类属性**：`class MyTool(Tool): risk_tier = "critical"`——插件加载时实例化，
   类属性即声明；`ToolCapability` 与契约 `ToolSpec`（`risk_level` / `requires_approval`）
   会同步带上，别的调用点不必再维护一份清单；
2. **Provider 运行期能力对象**：`ToolCapability.risk_tier` / `approval_policy`；
   桌面端 MCP 工具声明里的 `risk_tier` / `approval_policy` 会如实透传。
   审批调用点（`execute_tool_call`）必须把能力对象一起传下去，否则声明看不见；
3. **`PluginManifest`**：`side_effects` + `permissions[].needs_local_confirmation`
   → `manifest_tier_of()` / `manifest_requires_local_confirmation()`；安装视图
   `to_api()` 暴露 `declared_risk_tier` / `requires_confirmation`，前端展示的档位与
   审批引擎用的是同一张表。

"自述只能收紧"是硬约束：Provider 把 `workspace.write` 声明成 `auto` 也压不回 A 档
（基线是 B），把只读能力声明成 `critical` 会生效。**"自述不算授权"**与
`PluginManifest` 的既有原则一致。

遗留/辅助工具（`LEGACY_TIER_TOOLS`：`read`/`write`/`bash`/`office_doc_read`/
服务端实现名…）的 `risk_tier_of()` 返回 `None`，这是**显式契约**（"不归注册表管"），
调用方必须回落静态词表——把 `None` 当 `auto` 会直接放大审批面。

## 能力归属也可以声明（新工具不必回头改表）

`Tool.capability` / `ToolCapability.capability`（形如 `workspace.read`，可带 `@N`）
声明"这个工具属于哪个能力"。**静态映射表优先**：声明只在 `TOOL_CAPABILITY_MAP` 与
`IMPLEMENTATION_MAP` 都不认识这个工具时生效，因此插件不可能把 `workspace_commit`
改写成只读能力（路由/租约/审批都建立在表上）。

一旦声明生效，以下全部自动派生，无需再改任何静态表：

| 派生点 | 来源 |
| --- | --- |
| 能力归属 | 声明（`capability`） |
| Provider 路由 | 能力 → 服务端内联白名单，其余一律"客户端转发"（安全默认） |
| MCP 目标 | `CAPABILITY_TOOL_MAP` 优先，新能力回落工具自身名 |
| 动作类型 / 动作意图 | 能力描述符的 `side_effects` |
| 审批档位 / 策略 | 描述符 + 声明（见上） |
| 执行环境 | `ToolCapability.environment`（默认 `server`） |
| 可见场景 | `ToolCapability.scenes` / `annotations.scenes`（空 = 全场景） |
| 动作窗口 | 新能力按副作用交集补进窗口（见下） |

### 动作窗口的"声明补位"

`action_window()` = **静态对齐部分**（`action_window_static()`，与 `ACTION_TOOL_WINDOW`
逐条相等，是对拍基线）+ **声明补位部分**（`action_window_declared()`）。

补位只对"既不在 `INTENT_CAPABILITIES`、也不在能力目录"的能力生效，因此对既有窗口零影响；
补进来的工具必须**真的注册过**（`tool_registration_facts()` = 进程内 Skill ∪ 静态客户端
工具名 ∪ 影子注册表 ToolSpec，不再只看静态表——否则插件工具永远被判成"不存在"，
声明补位形同虚设）。

两套副作用词汇在此处桥接一次：契约的 `SideEffectKind.EXTERNAL` ↔ 意图表的 `publish`
（`_EFFECT_TO_INTENT`）。没有这层桥接，`SEND`/`PUBLISH` 的补位永远匹配不上"对外发布"
能力——那不是保守，是漏。`network` 刻意不映射（访问网络 ≠ 对外发布）。


## 灰度与真相源

`TOOL_REGISTRY_DERIVED` **默认关闭**：

* 关闭 —— 静态表是唯一真相源，行为逐字不变；注册表只用于影子对比与审计视图；
* 打开 —— 查询优先走派生，**静态表作为兜底**（派生拿不到结论时回落），因此不存在
  "打开开关后某些工具突然查不到"。

`TOOL_REGISTRY_SHADOW_LOG`（默认开）在每次任务提交时打点一次差异（只记录、不改行为）。

## 影子对比：四个判定维度当前**零差异**

```python
from app.agents.capabilities.tool_registry import shadow_compare
shadow_compare()
# {'tool→capability': [], 'capability→mcp_target': [], 'intent→tool_window': [], 'tool→risk_tier': []}
```

或 `GET /api/v1/admin/policies/tools` → `switch_safe: true`。

**这就是可以切真相源的证据**：四个判定维度差异为空，说明打开开关不会改变**静态表已经
认识的那些工具**的能力归属、MCP 目标、动作窗口或审批档位。任一维度非空时，正确动作是
**修派生逻辑**，而不是切开关。

两个容易读错的地方（接口里都如实回报，不要自己推）：

* **仅披露维度**（不计入 `shadow_diff_total` / `switch_safe`）：
  `tool→risk_tier(declared)` 与 `intent→tool_window(declared)`——静态词表结构上表达不了
  插件/Provider 的声明，那类差异是**有意为之**（`shadow_declared_total` 单独给数）；
* `shadow_missing_dimensions` 非空表示"某个维度没算成"，此时 `switch_safe` 一律为
  `false`——**"没跑成"不等于"没差异"**（旧实现用 `all(not v for v in diffs.values())`，
  维度因异常缺席时会把没跑成读成一致，这是最危险的一种误报）。

维度清单也由后端给，**前端不要硬编码**（本仓库真实发生过一次：新增 `tool→risk_tier`
后前端的固定维度列表把新维度静默丢掉）：

```json
"shadow_dimensions": {
  "parity": ["tool→capability", "capability→mcp_target", "intent→tool_window", "tool→risk_tier"],
  "declared": ["tool→risk_tier(declared)", "intent→tool_window(declared)"],
  "missing": []
}
```




### 派生过程中被暴露并修掉的真实不一致

影子对比第一次跑就抓到两处静态表自身的冲突（不是派生错，是表之间早就对不上）：

1. `CAPABILITY_TOOL_MAP["git.operations"]` 曾登记实现名 `git`，而
   `IMPLEMENTATION_MAP`/`ACTION_TOOL_WINDOW` 写的是 `workspace_diff` ——
   反查"能力→规范入口"会得到两个答案。已统一为 `workspace_diff`（真的会读工作区做
   diff 的那个）。
2. `workspace_commit` 在 `IMPLEMENTATION_MAP` 里是 `workspace.write`、在权威路由表
   `TOOL_CAPABILITY_MAP` 里是 `git.operations`。**以权威路由表为准**（派发/租约/审批
   实际用它），派生里显式记录了这个优先级，并有测试守住（`workspace_commit` →
   `git.operations`，客户端契约测试也是这么冻结的）。
3. 档位对拍抓到 `workspace_code_scan` 漏登记：它只读（给骨架不给正文），却因为落在
   `workspace_` 前缀兜底里被判成"例行确认"。已显式登记为 A 档；
4. `SIDE_EFFECT_TIER` 漏了契约的 `external`/`network` 两种 `SideEffectKind`，会掉进
   "默认 routine"——把"对外发布/发送"（收不回来）误判成普通写入。已补齐为 C 档/ B 档。

### 派生里的两个自递归（都已修）

条目构造会问档位，档位派生又会查条目表——这类"回头问自己"极易写成自递归，
而且症状不是报错而是**慢**（实测把插件加载拖到 35 秒 + 刷 RecursionError 日志）：

* `_static_mcp_target()`：能力 → MCP 目标不能调 `mcp_tool_for_capability`（它会反查注册表）；
* `_static_capability()`：工具 → 能力不能调 `capability_for_tool`（同上）。
  条目构造一律用这两个纯静态函数，热路径（每个候选工具一次的
  `build_registry_entries(capabilities)`）也走它，不再触发全局条目重建。


## 动作窗口的派生规则（为什么不是"按副作用全收"）

同一能力下挂着十几个工具别名（`workspace_list` / `stat` / `search` / `read` /
`content_extract`…）。如果按"副作用有交集就收"，READ 窗口会一次给模型 12 个同义工具，
把名额占满还让模型无从选择。窗口的价值在于**收窄**，所以：

* `INTENT_CAPABILITIES`：动作意图 → **候选能力序列**（顺序即优先级）；
* `_canonical_for_capability`：能力 → 规范入口（单一事实源 = `CAPABILITY_TOOL_MAP`）；
* 静态表里的工具**始终保留**（它们是真的注册过的，预检窗口不能出现不存在的工具）；
* 派生只**补位**静态表漏掉的规范入口，且只对 `SUPPLEMENTAL_CAPABILITIES`
  （工作区读/写族）生效：
  `code.execute` 刻意不在其中——它的执行窗口按子动作分叉
  （prepare/run/output_read/reset），客户端原始名本身就对应显式子动作，硬塞一个
  "规范入口"反而让模型不知道用哪个。

## 没做的部分（明确边界）

* ~~插件 manifest 的声明还不是注册表的输入~~ —— **已接入**（能力归属 + 档位 + 策略，
  三渠道见上）；
* ~~审批策略尚未接入 `approval_policy.py` 的三档~~ —— **已接入**（`risk_tier_of` +
  `classify_tool_risk(capability=…)`，四维零差异）；
* ~~新能力进不了动作窗口~~ —— **已接入**（`action_window_declared`，只补静态表没有的
  能力，单列披露）；
* **业务路由正则尚未删除**：`task_shape.py` / `task_preflight.py` 里的关键词路由仍在，
  要删得先有一段影子模式的命中率/误判率报告（属方案 4 的后续阶段）；
* **`code.execute` 执行窗口仍是静态**：它的子动作分叉（prepare/run/output_read/reset）
  由客户端原始名表达，硬塞规范入口反而让模型不知道选哪个；
* **任务级 deadline 还没下传到后台 Job**（当前只有请求级），以及 `/jobs` 读取仍与主
  流量共用连接池——两项都是独立的稳定性议题，不在本轮范围内。

因此结论是：**能力归属 / MCP 目标 / 动作窗口 / 审批档位 / Provider 路由 / 执行环境 /
可见场景都已是注册表派生，插件与 Provider 的声明就是输入**；"新增工具完全不用改代码"
对**能声明能力的新工具**已经成立。尚未达成的只剩"删掉历史业务路由正则"与
"任务级 deadline 下传"。

## 工具窗口诊断接口（前端排障面板的数据源）

```http
GET /api/v1/agents/jobs/{job_id}/tool-window?limit=20
```

```json
{
  "code": 0,
  "data": {
    "job_id": "…",
    "available": true,
    "count": 3,
    "max_entries": 20,
    "dropped_core_total": 0,
    "has_dropped_core": false,
    "windows": [
      {
        "scene": "chat", "limit": 8, "layer": "chat_graph.final", "job_id": "…",
        "layers": {"catalog": [], "eligible": [], "ranked": [], "final": []},
        "counts": {"catalog": 32, "eligible": 12, "ranked": 8, "final": 8},
        "dropped_by_layer": {"catalog→eligible": ["…"]},
        "pinned": ["workspace_navigator"],
        "dropped_core": [],
        "trimmed_optional": ["…"],
        "visibility": {"workspace_navigator": "available"},
        "mandatory_reason": "core"
      }
    ]
  },
  "message": "已返回工具窗口诊断快照"
}
```

读法（写进前端的展示文案里，避免误读）：

* `layers.final` 才是模型真正看到的集合，**别拿 `catalog` 当窗口**；
* `dropped_core` 非空 → 强制核心工具被挤掉，是**服务端缺陷**，标红而不是提示用户去处理；
* `dropped_by_layer` 直接指出"候选是在排序层丢的、还是在截断层丢的"（键是
  `上一层→这一层`）；
* `visibility` 里的 `unavailable` 表示能力本身不可用（客户端离线/租约过期），
  与"被窗口截断"是两种完全不同的处置；
* `available=false` 只代表"没有诊断帧"（未开启派生、或 24h 内没有该任务的窗口），
  **不代表工具正常**。

窗口大小不是固定 8：`limit` 是**可选工具预算**，强制项（`pinned`/`CORE_TOOLS`）额外保留，
因此 `final` 可能大于 `limit`。前端要做自适应布局，不要按 8 个槽位写死。


## 测试

`tests/test_tool_registry.py`（18 例）：

* 开关默认关闭；关闭时 `capability_of` / `mcp_target_for` / `action_window`
  **逐字**等于静态表结果；
* `shadow_compare()` 四个判定维度必须为空（切换前提）；`tool→risk_tier(declared)`
  当前不该出现；维度缺失时 `switch_safe` 必须为 `false`；
* 打开开关后工具→能力、能力→MCP、动作窗口与静态表逐条相同；
* `resolve_tool` 认三种写法；未知工具返回 `None`（不按关键词猜）；
* 条目带齐派生字段（含 `risk_tier`）；`local_only` 能力 `requires_lease=True`；
  `artifact.create` 是唯一服务端内联能力；
* 本机动作（`desktop_open_url`）没有能力、不参与租约；
* `workspace_commit` → `git.operations`（两条静态表冲突时的裁决口径）；
* 运行期 `ToolCapability` 派生：环境/审批/动作类型/场景（场景在 `annotations` 里）。

`tests/test_approval_tier_derivation.py`（22 例）：

* **逐条对拍**：注册表负责的每个工具，`risk_tier_of` == `static_tier_of`，
  且 `classify_tool_risk` 与注册表给同一档位；
* 遗留工具的 `None` 是显式契约（不是"派生不出来"）；
* 三个声明渠道都能进来：工具类属性、Provider 能力对象、`PluginManifest`；
* 声明**只能收紧**：`workspace.write` 声明 `auto` 压不回 A 档；
  全新能力没有基线时声明即答案，连声明都没有才判 C；
* **能力归属可声明**：`capability="workspace.read"` → 能力/Provider/本地性/租约/
  动作类型/动作意图/档位全链路派生；静态表优先（`workspace_commit` 声明也改不动）；
  非法能力名（大写/无点/含空格）视为未声明；
* **新能力自动进动作窗口**：目录里新增描述符 + 声明它的工具 → `PUBLISH`/`SEND`
  窗口出现该工具（`external` ↔ `publish` 桥接生效），且只出现在
  `intent→tool_window(declared)`，`switch_safe` 仍为 `true`；
* 声明带来的差异只出现在披露维度里；描述符 `needs_local_confirmation` → 策略
  `confirm`，但**工具级例外优先**（`workspace_diff` 仍为只读无确认）；
* 开关关闭时声明完全不参与判定。

`tests/test_tool_window_diagnostics.py`（12 例）：

* 窗口快照最新在前、按 `TOOL_WINDOW_MAX_ENTRIES` 裁剪、带 TTL、坏数据跳过、
  读写失败静默；
* 无事件循环时不硬造 loop；有循环时异步落盘且带 `job_id`/`layer`；
* 接口形状（四层 + `dropped_core` 计数）、无快照时 `available=false`、
  越权 404、请求上限被夹到保留条数。

