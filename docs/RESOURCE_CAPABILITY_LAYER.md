# 统一资源能力层（方案《资源能力层》）—— 后端落地记录

目标：把"Office 工具 / Workspace 工具 / MCP 工具 / 内部 Skill 工具各自一套名称、映射和注入
逻辑"改造成**一个中间抽象**：

```text
Office/ReAct        = 编排层（保持现状：ReAct、Workflow Runner、审批、Effect Journal、
                      Step Checkpoint、SSE 过程日志、重试、恢复、前端展示全部复用）
Resource Capability = 统一工具抽象（模型与编排只认这一层）
Workspace / Document / Knowledge / Artifact = Provider（各自负责"怎么执行"）
MCP                 = Provider 的底层通信协议
```

```text
resource.read / resource.write / resource.edit / resource.move / resource.delete
code.execute / artifact.create
        │ resource_type
        ├── workspace        → workspace_provider（+ git_provider 并列候选）
        ├── office_document  → office_document_provider
        ├── knowledge        → knowledge_provider（只有声明，尚无实现）
        ├── artifact         → artifact_provider
        └── memory           → memory_provider（Phase 6 验收对象）
```

**不删旧工具**：底层 `workspace_write` / `office_doc_edit` / `mcp__…` 仍然存在，
只是逐步从"模型路由的输入"退化成"Provider Adapter 的实现细节"。

---

## Phase 1（已完成）：兼容能力目录

模块：`app/agents/capabilities/resource_catalog.py`

| 内容 | 说明 |
| --- | --- |
| 统一能力词表 | `resource.read/write/edit/move/delete`、`code.execute`、`artifact.create`；别名 `sandbox.run` → `code.execute`、`resource.search` → `resource.read`（同一件事，不加第二个概念） |
| 资源类型 | `workspace` / `office_document` / `knowledge` / `artifact` |
| 旧能力 → 新能力 | `LEGACY_CAPABILITY_BINDINGS`：九张既有能力声明**一个不漏**，是"旧名 → 新名"的唯一落点 |
| 工具级例外 | `TOOL_BINDINGS`：能力级映射不够精确的地方（`workspace_diff` 属 `git.operations` 但只读；办公文档族的资源类型是文档不是工作区；客户端原子名 `Read/Write/Edit/Rename/Delete/Bash/Glob/Grep`） |
| Provider 声明 | `RESOURCE_PROVIDERS`：`workspace_provider` / `git_provider` / `code_provider` / `office_document_provider` / `artifact_provider` / `knowledge_provider`（**声明但未注册**）。同一 (能力, 资源类型) 可以有**多个候选，按声明顺序**，选择权在 Broker |
| 三态可见性 | `resource_visibility()` → `unregistered` / `registered` / `visible` / `available` / `unavailable`。**"系统不认识"与"有但此刻调不到"必须分开**——混在一起会让模型以为工具不存在而去编替代做法 |
| 绑定查询 | `binding_for_tool()`：工具级例外 → 显式传入的能力（注册表派生/插件声明）→ 静态表 → **不认识就 `unknown`，绝不猜** |

验收（方案 Phase 1）：`Read`、`workspace_navigator`、`office_doc_read` **都映射到
`resource.read`**（前两者 `resource_type=workspace`，后者 `office_document`）。

注册表条目新增四个**只读元数据**字段（`unified_capability` / `resource_type` /
`resource_provider` / `provider_candidates`），管理端
`GET /api/v1/admin/policies/tools` 额外回报：

```json
{
  "resource_catalog": { "unified_capabilities": [...], "providers": [...], "legacy_bindings": {...} },
  "resource_bound_count": 53,
  "resource_unbound_tools": ["AskUserQuestion", "Calculator", "web_search", "task_memory", "..."]
}
```

`resource_unbound_tools` 是**迁移进度表**：Phase 2–6 的每一步都应该让这个列表变短；
它只披露、不参与任何判定。当前（加载全部插件）**53 / 79 已绑定，26 个未绑定且全部有归类**。

### 刻意**不**接入的工具族（写下来，而不是"忘了"）

`DEFERRED_TOOL_FAMILIES`：

| 族 | 例子 | 为什么不接 |
| --- | --- | --- |
| `ui_orchestration` | `AskUserQuestion` / `EnterPlanMode` / `Task` / `TodoWrite` / `Skill` / `SlashCommand` / `Calculator` / `OpenApp` / `ProcessList` … | 它们不操作"资源"，是编排与交互原语；硬给资源类型只会让 Broker 多一条无意义候选 |
| `external_service` | `send_email` / `calendar_manager` / `web_search` / `web_fetch` / `curl` | 资源在**外部**，需要 `external_service` / `web` 这类新资源类型——新增资源类型要连带 Provider 与审批语义，属于 Phase 2+ 的显式决定 |
| `memory` | `task_memory` | Phase 6 的验收对象（`memory_provider`，`resource_type=memory`），刻意留白以证明"新 Provider 不改静态映射也能接入" |

有测试把这条边界**钉住**：静态工具全集里，每个未绑定工具都必须落在某个 deferred 族里，
否则报错并点名。于是"还没接入"从含糊的现状变成了一份可点数、可验收的清单。

### Phase 1 的三条不变量

1. **不认识就不猜**：没有绑定的工具返回 `source="unknown"`、能力为空。绝不"名字里有
   write 就当写"——猜错的代价是把写操作当只读派发；
2. **旧能力名仍是合法输入**：兼容层接受 `workspace.read`，统一名是它的**上层表达**而不是
   替换（客户端协议冻结在旧名上，`tests/test_capability_client_contract.py` 守着）；
3. **零行为变化**：Phase 1 只加元数据。影子对拍仍为零差异，`capability_of` /
   `classify_tool_risk` / 工具窗口逐字不变（有测试钉住），元数据派生失败也不影响条目构造。

---

## Phase 2（已完成）：统一预检与工具窗口

模块：`app/agents/capabilities/resource_window.py`；开关 `RESOURCE_CAPABILITY_WINDOW`（默认关闭）。

```text
action_intent + resource_type
        ↓ required_capability（INTENT_UNIFIED_CAPABILITY）
        ↓ Registry 候选 Provider（resource_catalog.providers_for）
        ↓ 规范工具（CANONICAL_TOOL_BY_CAPABILITY 种子表 → 缺省按注册表候选定序）
    本轮工具窗口
```

| 内容 | 说明 |
| --- | --- |
| 意图 → 统一能力 | `READ/SEARCH→resource.read`、`CREATE→resource.write`、`MODIFY→resource.edit`、`DELETE→resource.delete`、`MOVE→resource.move`、`EXECUTE→code.execute`；`SEND/PUBLISH` **刻意没有**（外部资源属 deferred 族，宁可不派生也不编一个） |
| 资源类型来源 | `TaskProfile.target_scope`（优先）→ `info_sources`；都没有时**从旧窗口反推**（`resource_types_from_window`），既不猜、也不让老画像失去窗口 |
| 规范工具 | `CANONICAL_TOOL_BY_CAPABILITY` 是**种子表**：表里没有的组合（新 Provider 的新能力，如 Phase 6 的 `memory`）按注册表候选定序自动选 → "新增 Provider 不改动作映射表"成立 |
| **超集不变量** | 资源层窗口 **⊇ 旧窗口**，只补位、不删工具。旧表 `ACTION_TOOL_WINDOW` 降级为 fallback；Phase 5 之前不删任何工具 |
| 读优先 | 所有变更类意图（含 `CREATE`）把该资源的**读取入口排在最前**（静态表只对 MODIFY/DELETE/MOVE 这么做） |
| **截断保护** | `read_guards()`：候选池里出现某资源的变更类工具时，该资源的读取入口被当作**强制工具**（额外占位，不参与 Top-K）。保护刻意作用在 `apply_tool_window` 的**截断层**——丢工具真正发生在那里；且只在读取入口**真的在池里**时才钉（钉不存在的名字会变成 `dropped_core` 噪音，那是"上游过滤"的故障信号） |

**Phase 2 验收**（方案原文）："新增 `workspace_write` 不会让 `resource.read` 消失"。
测试用办公文档资源（`office_doc_read` 不在 `CORE_TOOLS` 里，只有新规则能救它）：
池里 8 个噪音工具 + `office_doc_edit` + `office_doc_read`、`limit=1` →
读取入口仍在（`pinned` 且 `mandatory_reason` 带 `+resource.read`）；**开关关闭时照旧被截断**
（逐字不变）。

对拍口径：新增披露维度 `intent→resource_window(resource layer)`，列出
`[意图, 静态窗口, 资源层窗口]`。差异**全部是新增**（有测试断言 `static ⊆ derived`），
因此与声明维度同口径**不计入** `switch_safe`，只作为"打开后多出什么"的预览。

---

## Phase 3（已完成）：统一 Broker 派发

模块：`app/agents/capabilities/resource_dispatch.py`；开关 `RESOURCE_CAPABILITY_DISPATCH`（默认关闭）。

改造前，"这次调用属于哪个能力"是**从工具名字猜出来的**（`workspace_write → workspace.write`
查静态表）；名字里带什么就认定是什么，写错的映射会把写操作当只读派发。改造后派发拿到的
是一份**结构化目标**：

```text
capability         = workspace.write        ← Broker/租约/目录的键（兼容名，客户端协议冻结在它上面）
unified_capability = resource.write         ← 统一能力（模型与编排的表达）
resource_type      = workspace              ← Broker 按它收窄 Provider 候选
provider_id        = lumi.local.workspace   ← 由 Provider Adapter 决定，不由名字猜
mcp_target         = workspace_write        ← 底层原子工具（Adapter 的职责）
```

| 内容 | 说明 |
| --- | --- |
| `resolve_dispatch(tool, capability_hint=, resource_type=)` | 工具名 → 结构化目标。**不认识就 `unknown`**；调用方给出能力/资源类型时以它为准（旧 MCP 依赖兼容解析走这条） |
| `ProviderAdapter` | 一个资源 Provider 的**唯一**转换点：`mcp_tool_for(capability, resource_type)` → 底层原子工具；`adapters_for` 给出有序候选，`provider_ids_for` 给出 Broker 的收窄集合 |
| **歧义不猜** | `resource.write` 跨多种资源，只给能力不给资源类型 → 判"不知道"，而不是挑一个资源 |
| Broker 收窄 | `select_lease(..., provider_ids=…)` 按资源类型收窄候选；**收窄后一个候选都不剩时按收窄前继续**（声明缺失不该表现成"工具不可用"），并记 DEBUG 原因 |
| 写入安全链不变 | 租约、审批门禁（`policy_guard`，在选 Provider **之前**）、版本校验、写闸全在原位——Phase 3 只改"能力从哪来"，不改"能不能执行" |
| 兼容回落 | 关闭开关 → `capability_for_mcp_tool` 逐字不变；`AGENT_CAPABILITY_ROUTING_MODE=off` 时连结构化解析都不做（零开销） |
| 结果 metadata | `SkillResult.metadata` 带 `unified_capability` / `resource_type` / `provider_name`（前端据此显示"正在写入工作区"） |

迁移期不变量（有测试钉住）：Adapter 给出的底层工具必须与既有 `CAPABILITY_TOOL_MAP` 一致
（`resource.edit + workspace → workspace_edit`、`artifact.create + artifact → create_office_document` …），
否则就是回归。

---

## Phase 4（已完成）：Workflow Skill 依赖迁移到能力声明

模块：`app/agents/capabilities/resource_workflow.py`；开关 `RESOURCE_CAPABILITY_WORKFLOW`（默认关闭）。

```text
改造前  allowed_tools = [mcp__lumi_client__workspace_navigator, …workspace_write,
                        …workspace_edit, …sandbox_run, …]   共 14 个底层名字
改造后  required_capabilities = [resource.read, resource.write, resource.edit,
                                resource.move, resource.delete, code.execute]
        resource_types        = [workspace]
        providers             = [workspace_provider]
```

底层工具名由 Provider Adapter（Phase 3）解析；Workflow 内部照旧按 读取 → 修改 →
测试 → 提交 执行，每次调用仍走统一执行器（授权/审计/审批/副作用日志/互斥锁不变）。

| 内容 | 说明 |
| --- | --- |
| `WorkflowSkill` 声明位 | `required_capabilities` / `resource_types` / `providers`（空 = 走旧 `allowed_tools` 兼容路径） |
| 声明归一 | 别名归一（`sandbox.run → code.execute`）、去重、**非法能力名丢弃**（写错的声明退回旧路径，不悄悄变成另一个能力） |
| 兼容解析 | `legacy_tools_to_capabilities()`：旧 MCP 名字 → 统一能力，老 Skill 不改一行也能被能力层读懂 |
| **只补不替** | `effective_dependencies()` 保留全部旧工具行，能力派生的行以 `via="capability"`、`required=false` **追加**。替换会让 `resolve_dependencies` 看到的依赖变少——那是"依赖检查变松"，是回归 |
| 歧义不猜 | 只声明 `resource.write` 而不给资源类型 → 不解析工具（它跨 workspace/office_document/…） |
| 工具解析 | `tools_for_capabilities()` → Provider Adapter → **客户端规范名**（`code.execute → sandbox_run`，与桌面端广告的名字对齐） |
| 选择 | `select_capabilities()`：能力声明优先，旧名字白名单兜底（并集）；什么都不声明 → 不选（绝不"全选"） |
| 真实 Skill | `workspace_code_change` / `workspace_operation` 已声明能力；**不出圈**有测试钉住：能力解析出的工具必须本来就在旧 `allowed_tools` 里，否则"迁移"会悄悄扩大 Skill 的工具面 |

---

## Phase 5（进行中）：模型可见工具面收敛

模块：`app/agents/capabilities/resource_surface.py`；开关 `RESOURCE_CAPABILITY_SURFACE`（默认关闭）。

```text
收敛后（模型可见）：  Read / Write / Edit / Move / Delete / Run / Search
                                    ↓ 执行时按能力+资源类型解析（Provider Adapter）
底层实现（不再直接暴露）：workspace_navigator / workspace_write / workspace_edit /
                          Rename / Bash / Glob / Grep / sandbox_* …
```

这是整个改造里**唯一会拿走东西**的一步，因此先交付"可计算的可见面 + 可对拍的差异"：

| 内容 | 说明 |
| --- | --- |
| 名称映射 | `MODEL_TOOL_BY_CAPABILITY`：能力 → 对外名；`Search` 由**检索意图或检索入口**（`Glob`/`Grep`/`workspace_search`）判定——两条都要有，否则同一批工具在不同轮次会算出不同的对外名字 |
| **分类不了就不隐藏** | 没有统一能力绑定的工具（本机动作、编排原语、外部服务）与**刻意不收敛**的 `artifact.create` 一律保留原名："不认识"绝不等于"可以拿走" |
| 收敛面 | `converged_face(tools)`；`hidden_tools(tools)` 列出会消失的名字；`surface_diff()` 给出对拍行 `[能力, 当前工具名, 收敛后名字]` |
| 改名集合 | `Read/Write/Edit/Delete` 与客户端原子名同名（原地保留）；`Move→Rename`、`Run→Bash`、`Search→Glob/Grep` 需要别名 |
| 别名解析 | `resolve_alias(model_name, available)` → `(真实工具, 能力)`：先客户端原子名、再 Provider Adapter 规范工具；**解析不出返回空**，调用方必须报结构化失败（不能静默换工具——那等于绕过用户批准的那次调用） |
| 影子披露 | 新增披露维度 `tool→model_surface(converged)`；管理端 `resource_surface` 给出词表、`hidden_tools` 与逐工具去向 |

实测（加载全部插件）：模型面 `['Read','Write','Edit','Delete','Run','Search','Move']`，
**28 个实现层名字会被隐藏**，其中 `resource.read` 一个能力当前就暴露了 10 个工具名——
这正是本次收敛要解决的问题。

**Phase 5 第二步（已完成，chat 路径）**：`make_skill_tool(display_name=…)` +
`collapse_for_surface()` + `chat_graph` 注入收敛面。

```text
模型看到 Write  →  StructuredTool(name="Write")
                →  execute_tool_call(function.name="Write")
                →  _resolve_model_alias → workspace_write（实现名，进审计）
                →  租约/审批/版本校验/写闸（与改造前完全同一条路径）
```

* `collapse_for_surface(capabilities)`：**唯一**的"减少模型可见工具"入口。同一对外名
  只保留**第一个**（候选池已按相关性排序）；未登记/不收敛的工具保留原名、不参与合并；
* 关闭时是**恒等映射且不去重**——逐字等于改造前（有测试钉住）；
* 工具窗口快照在收敛打开时记录**对外名**（它才回答"模型这次拿到了什么"）；
* 实测（加载全部插件）：模型可见名 **79 → 34**，48 个实现层名字被隐藏。

**Phase 5 第三步（已完成，ReAct 路径）**：`react_runner` 的注入与派发同样收敛。

与 chat 路径的关键差别：ReAct 有**基于名字的安全逻辑**，因此收敛不能只改 schema：

| 名字型逻辑 | 收敛后的处理 |
| --- | --- |
| `_requires_prior_read(name)`（改/删前必须先读） | 先用 `_impl_name(name)` 解析回实现名再判断——否则模型叫 `Edit` 会绕过针对 `workspace_edit` 的护栏（`Edit` 不在实现名词表里） |
| `_successful_reads`（读过哪些目标） | 按实现名记录 |
| `_failed_tools`（候选池按实现名排除失败方法） | 按实现名记录；SSE 事件里的 `tool` 仍是模型可见名 |
| `_call_key`（相同工具+参数去重） | 按实现名，保证收敛前后是同一个键 |
| `allowed_tools`（传给执行器） | 传**实现名**（执行器先解析对外名再校验，解析只在一处） |
| `build_tool_selection_contract` | 传按对外名改写的候选（`:func:`collapse_for_surface` 的 display），保证提示词与 schema 一致 |

实测（`tests/test_react_surface_convergence.py`）：收敛打开时绑定给模型的是 `Read/Edit/Write`；
模型先 `Edit` 仍被护栏拦下（错误回给 `Edit` 这个名字），`Read` 之后再 `Edit` 才真正执行。

**Phase 5 第四步（已完成，Workflow 路径）**：schema 与 SOP 提示词同时收敛，用**运行期翻译**
而不是改提示词内容。

| 内容 | 说明 |
| --- | --- |
| schema | 两个工作流的 `_tool_defs()` 改吃 `collapse_with_names()` 的对外名 |
| SOP 文本 | `WorkflowSkill.effective_prompt()` 与插件内的 system 文本都过 `translate_prompt_names()`：实现名 → 对外名，**逐字替换、不碰业务语义**；关闭开关时返回原文 |
| 名字型前置判断 | 插件的提交/测试/diff 判断（`name.endswith("workspace_commit")` 等）先按 `surface_alias` 解析回实现名 |
| **收敛边界** | `UNCONVERGED_TOOLS`：暂存/提交/回滚/diff/沙箱准备与输出——七个动词表达不了的**阶段/辅助动作保留原名** |

收敛边界是这一步验收逼出来的：最初 `workspace_commit` 也被收敛成 `Write`，结果是
①"提交"与"写入"在审计/前端里再也分不出；②`collapse_for_surface` 按对外名去重，
`workspace_commit` 被 `workspace_write` 挤掉，**提交路径彻底消失**。

实测（加载全部插件）：模型可见名 **79 → 48**（隐藏 34 个实现名），其中 9 个阶段/辅助动作
按边界保留原名（`workspace_commit` / `workspace_diff` / `workspace_stage_*` /
`sandbox_prepare` / `sandbox_output_read` / `sandbox_reset` / `workspace_rollback`）。

**Phase 5 剩余**：无（三条模型注入路径 chat / ReAct / Workflow 均已收敛；`base_tools.yaml`
的"白名单只描述安全策略"是方案 §六的独立议题，不在本目标范围内）。

---

## Phase 6（已完成）：新 Provider 验收

验收对象：`memory_provider`（`resource_type = memory`，`provider_id = lumi.server.memory`），
背后是仓库里真实存在的 `task_memory`（任务内工作记忆，服务端 Redis）。

**接入成本 = 两处声明，零静态映射改动**：

```python
# plugins/tools/office/task_memory.py
class TaskMemorySkill(Tool):
    capability = "resource.write"     # ← 声明一次
    resource_type = "memory"          # ← 声明一次

# app/agents/capabilities/resource_catalog.py
ResourceProviderSpec(name="memory_provider", provider_id="lumi.server.memory",
                     resource_types=("memory",),
                     capabilities=(resource.read, resource.write), ...)
```

| 环节 | 证据（`tests/test_memory_provider_acceptance.py`，22 例） |
| --- | --- |
| **被发现** | 注册表条目带 `unified_capability=resource.write` / `resource_type=memory` / `resource_provider=memory_provider`；它从"未绑定清单"里消失 |
| **被注入** | `tool_window_for_actions(["CREATE"], resource_types=["memory"])` 含 `task_memory`；`plan_for_actions` 给出能力→工具→候选 Provider；Workflow 只声明 `required_capabilities`/`resource_types` 就能拿到工具 |
| **被执行** | `resolve_dispatch("task_memory")` → 统一能力/资源类型/Provider；`adapter_tool_for("resource.write","memory") == "task_memory"`；`execute_tool_call` 真实跑通（`allow_internal=True`：它目前是内部执行实现，尚未进公共模型池） |
| **没改静态映射** | 反向断言：`Router / Preflight / ChatGraph / ReActRunner / Broker / dispatch / builtin / catalog` 八个模块的源码里**不得出现** `memory_provider` / `task_memory` / `"memory"`；三张静态映射表无记忆条目；影子对拍四个判定维度仍为空、`switch_safe` 仍为 true |

### 验收暴露并修掉的两个真问题

1. **收敛面必须按资源类型限定**（Phase 5）：`resource.write` 跨 workspace/office_document/memory，
   模型只叫 `Write` 时执行期无法判断该落哪个 Provider，`resolve_alias` 只能按优先级挑——
   结果是"编辑办公文档却调了工作区写入"。现在 `CONVERGED_RESOURCE_TYPES = {workspace}`，
   其余资源保留原名（`task_memory` 曾被错误收敛成 `Write`）。
2. **`DispatchTarget.known` 的判据**：原先要求"有旧能力名"，而纯声明式工具（`task_memory`）
   在旧能力目录里没有条目 → 明明认识却被判 `unknown`。现在判据是**统一能力 + 资源类型**；
   旧能力名只是 Broker/租约的键，缺失时走服务端内联执行路径。

### 测试隔离教训（已修）

验收测试最初调 `load_skill_plugins()` 加载全量插件，顺带注册了 `python_exec`；而办公脚本
Agent 在 `python_exec` **未注册**时会跳过沙箱预检，于是 `tests/test_office_docs.py` 的三个
用例在整包运行里失败（单跑通过）。现在验收测试按文件名**精确加载**一个插件并在结束时撤销：
验收测试不该改变别人的前置条件。


### 执行期接线（已就位，开关关闭时逐字不变）

`executor._resolve_model_alias(name)`：

| 输入 | 结果 |
| --- | --- |
| 开关关闭 / 不是模型可见名 | **原样返回**（零行为变化） |
| `Move` | 池里有 `Rename` → `Rename`；否则 `workspace_move` |
| `Run` | 池里有 `Bash` → `Bash`；否则 `sandbox_run` |
| `Search` | 池里有 `Glob`/`Grep` → 它；否则 `workspace_search` |
| `Read` | 池里有 `Read` → 它；否则 `workspace_navigator` |
| 解析不出任何实现 | 结构化失败 `MODEL_ALIAS_UNAVAILABLE`（**绝不静默换工具**） |

映射发生在 `allowed_tools` 校验**之前**（Workflow 白名单登记的是实现名），并把
`model_tool` / `alias_capability` 写进审计 scope，排障能回答"这次到底是哪个工具在跑"。


`base_tools.yaml` 按方案分两步：先"白名单只描述安全策略"（是否注册由 Registry 决定、
是否可见由 scene+policy+permission 决定、是否可用由 Provider+lease 决定），
再改成"默认暴露策略"（`public_by_default: false` + `safe_operations`）。

---

## 过程条目/事件的结构化标签（跨层收尾，无开关）

Phase 5 之后前端仍剩一处"只能靠工具名猜"：实时步骤与刷新后的过程日志里给的是
`tool_name: "workspace_write"`，界面主文案就会冒出实现层名字。根因是**事件里没有能力字段**，
前端只能自己维护一张"工具名 → 能力"的表。现在由后端在事件里给出目录事实：

| 字段 | 取值来源 | 是否受开关影响 |
| --- | --- | --- |
| `capability` | 统一能力目录（Phase 1 元数据） | 否 |
| `resource_type` | 同上 | 否 |
| `provider_id` | `DEFAULT_PROVIDER_BY_RESOURCE` / 派发解析 | 否 |
| `provider_name` | `RESOURCE_PROVIDERS` 里的 Provider 名 | 否 |
| `display_name` | 本轮模型**实际看到**的对外名 | **是**（收敛关闭时等于工具名，故省略不下发） |

五条不变量（`tests/test_process_dispatch_labels.py`，15 例）：

1. **开关无关的目录事实**：`capability`/`resource_type`/`provider_id`/`provider_name` 与任何
   flag 无关——它们是"这个工具是什么"，不是"模型现在叫什么"；
2. **不谎报**：`display_name` 受收敛开关约束（关闭时不给，前端已有的 `tool_name` 就是模型看到
   的名字）；调用方若已记下当时的对外名（`step["display_name"]`），则以它为准；
3. **不猜**：认不出的工具返回空（前端回退到原逻辑），形状怪异的输入返回空；
4. **绝不含用户数据**：`label_value()` 是闭集形状闸门（`^[A-Za-z0-9_.:@-]{1,80}$`），
   路径/正文/参数不可能借标签漏出（有用例专门断言 `/home/secret/.env`、
   `TOP-SECRET-TOKEN` 不出现在任何标签里）；
5. **老载荷逐字不变**：字段默认 `None` + `exclude_none=True` ⇒ 老条目 JSON 一个键都不多
   （新增用例断言默认载荷仍是原来那 13 个键）。

接线位置（都是"取到就带上，取不到什么都不发生"）：

| 出口 | 位置 |
| --- | --- |
| 契约 | `ProcessLogEntry` 五个可选字段 + `from_event`/`to_sse_fields`/`merge_process_log`（后到帧可补前帧缺的标签） |
| 服务端过程日志 | `app/contracts/process_log.py::dispatch_labels_for_step` |
| SSE 步骤帧（canonical） | `app/contracts/event_adapter.py::_step_payload` |
| TypeScript | `packages/contracts/ts/lumi-contracts.d.ts`（由 `scripts/export_ts.py` 生成，已同步） |

前端因此可以删掉自己的"工具名 → 能力"映射表：拿到 `capability`/`resource_type` 直接显示
"写入工作区"，`display_name` 有值时优先用它作为"模型本轮的工具名"。

## 测试

`tests/test_process_dispatch_labels.py`（15 例，过程条目结构化标签）：

* 标签是目录事实（开关关闭也下发）、`display_name` 只在收敛后有值、未知/畸形工具名返回空；
* 形状闸门 `label_value` 只放行闭集字符；契约侧默认载荷**一个键都不多**、`from_event`
  丢弃畸形值、`merge_process_log` 用后到帧补齐先到帧缺的标签；
* 服务端过程日志与 SSE 步骤帧都带标签、未知工具不加任何标签；
* **安全用例**：步骤参数里的路径与正文不出现在任何标签里；
* 预览 ≠ 现状：管理端预览面照旧算出 `Write`，而实际下发名在开关关闭时仍是 `workspace_write`。

`tests/test_resource_workflow_surface.py`（9 例，Phase 5 第四步）：

* 关闭开关时 SOP 翻译**逐字不变**；打开时实现名 → 对外名，且 `workspace_writer_helper`
  这类子串**不被误替换**；
* **收敛边界**：`workspace_commit` / `workspace_diff` / `sandbox_prepare` 保留原名，
  提交路径仍然可达（不会被 `workspace_write` 挤掉）；
* **一致性断言**：schema 里的名字与翻译后的 SOP 名字同属一套（否则模型会照提示词调一个
  schema 里不存在的名字）；
* 真实 SOP（`plugins/workflows/prompts/*.md`）在收敛打开时不再残留任何收敛过的实现名。

`tests/test_react_surface_convergence.py`（3 例，Phase 5 第三步）：

* 收敛打开 → 绑定给模型的是 `Read/Edit/Write`（实现层名字不出现）；关闭 → 逐字是
  `workspace_navigator/workspace_edit/workspace_write`；
* **护栏不因改名失效**：模型先叫 `Edit` 仍被"改前必须先读"拦下，`Read` 之后再 `Edit`
  才真正执行（`_impl_name` 把对外名解析回实现名）；
* `_impl_name` 恒等/映射语义与 `search_tools` 编排原语不受影响。

`tests/test_resource_surface_injection.py`（9 例，Phase 5 第二步）：

* 关闭时是恒等映射**且不去重**；打开时改名 + 合并，同一对外名保留第一个能力；
* 未登记工具（`AskUserQuestion`）与刻意不收敛的 `create_office_document` 原样保留；
* `make_skill_tool(display_name=…)`：模型看到对外名，**执行器收到的也是对外名**
  （由它一处解析回实现名）；没给对外名时逐字不变；
* 端到端解析：对外名 → 实现名 + 能力；解析不出报 `MODEL_ALIAS_UNAVAILABLE`；
* 办公文档/本机动作不参与收敛，调用路径逐字不变。

`tests/test_resource_surface.py`（20 例，Phase 5）：

* 名称映射（含 `Search` 的两条判定路径、别名归一）、刻意不收敛与未知能力不给模型名；
* **分类不了就不隐藏**：无绑定工具与 `create_office_document` 均保留，隐藏项必须有能力绑定；
* 收敛面/差异行形状（三格，与其它披露维度一致）、改名集合被钉住
  （`Read/Write/Edit/Delete` 原地保留，`Rename/Bash/Glob/Grep` 被改名）；
* 别名解析：关闭开关时逐字不变、客户端原子名优先、服务端规范名兜底、解析不出返回空；
* 执行期接线：`execute_tool_call` 对不可解析的别名报 `MODEL_ALIAS_UNAVAILABLE`；
  关闭开关时同名工具照旧走旧路径（`SKILL_NOT_FOUND`），证明没有隐式改名；
* 真实注册表上可算出收敛面与隐藏清单，且收敛面一定小于当前面。

`tests/test_resource_workflow.py`（19 例，Phase 4）：

* 声明归一（别名/去重/非法丢弃）、老 Skill 的能力兼容解析、歧义不猜、Provider 声明 ∪ 推导；
* **只补不替**：旧工具行全部保留、能力行以 `via=capability` 追加且 `required=false`；
  只声明 `allowed_tools` 的 Skill 依赖清单**逐字不变**；能力行不会让本来可用的 Skill 变不可用；
* 能力 ↔ 工具解析（经 Provider Adapter，客户端规范名）、资源类型为空不解析；
* `select_capabilities` 的能力筛选 + 旧名字兜底 + "什么都不声明就不选"；
* 两个真实 Skill 的声明与**不出圈**断言（解析出的工具 ⊆ 旧声明）。

`tests/test_resource_dispatch.py`（19 例，Phase 3）：

* **结构化解析**：`workspace_write` → `workspace.write` / `resource.write` / `workspace` /
  `workspace_provider` / `lumi.local.workspace` / `mcp_target=workspace_write`；
  未知工具不猜；旧 MCP 依赖给能力名时照旧能映射到统一能力；
  **统一能力 + 无资源类型 = 歧义 → 判不知道**；
* **Provider Adapter**：首选已注册 Provider（只有声明的知识库不参与）、能力→底层工具、
  缺实现时不编名字、`provider_ids_for` 收窄集合；
* **迁移期不变量**：Adapter 的底层工具与既有 `CAPABILITY_TOOL_MAP` 逐条一致；
* **收窄**：按 `provider_ids` 收窄后排除无关 Provider；**收窄到空时回退收窄前的候选**；
  没有候选时仍然报"绑定不匹配"（不掩盖真实原因）；
* **接线**：开关打开时结构化字段进入 `RoutingDecision`/打点/`SkillResult.metadata`；
  开关关闭时逐字走 `capability_for_mcp_tool`；`off` 模式零开销；
  结构化解析抛异常时回落兼容解析（不让一次工具调用打挂）。

`tests/test_resource_catalog.py`（41 例）：

* **验收**：`Read` / `workspace_navigator` / `office_doc_read` → `resource.read`，
  且资源类型区分工作区与办公文档；注册表派生/插件声明的能力也能落进统一层；
* 映射完备性：九张能力声明一个不漏、反向无多余项、有能力工具全部绑定、
  无能力本机动作**看得见**（出现在未绑定清单）、未知工具不猜；
* **边界被验证**：静态工具全集里未绑定的工具必须落在某个 deferred 族里
  （否则测试点名报错）；族名归一化容错（`mcp__lumi_client__web_search` → `external_service`）；
* 词汇与 Provider：统一能力封闭、别名归一、每个资源类型都有 Provider 声明、
  `resource.write + workspace` 有**两个有序候选**、知识库 Provider 明确标"未注册"
  且它的读取入口 `query_knowledge` 已绑定；
* 三态可见性：`unregistered` / `registered` / `visible` / `available` / `unavailable`
  各自可达，且未知工具绝不报 `available`；
* 零行为变化：影子对拍全空、`switch_safe` 为真、`capability_of` 与静态表逐字相同、
  绑定矩阵 20 条逐一钉住、**元数据派生抛异常也不影响条目构造**。

`tests/test_admin_policy_api.py` 增加：`resource_catalog` / `resource_bound_count` /
`resource_unbound_tools` 的形状断言。
