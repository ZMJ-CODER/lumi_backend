# 工具发现链路：必须保留工具与四层诊断

对应评审《工具发现链路》的 P0 两项。这一轮**只做后端**，目标是让"工具其实注册好了，
但模型看不见"这个故障**消失或至少可定位**。

## 问题复述（为什么要做）

```text
Provider 注册 → get_capabilities_for_scene → 场景/角色过滤
              → select_capabilities_with_trace → 域/语义排序 → Top-K
              → ChatGraph / ReAct 合并注入 → 再截断 → make_skill_tool → bind_tools
```

链路上至少 6 处硬编码截断：ChatGraph 最终池 `[:8]`、Office 初选 `limit=3`、
ReAct 每轮 `[:8]`、会话缓存合并 `[:8]`、域发现后 `[:8]`、工作区阶段注入
`[: 8 - len(injected)]`。它们都是**普通 Top-K 竞争**，所以"新装一个写工具"就可能把
`workspace_navigator` 挤出模型可见窗口——现场表现正是"模型识别不到读工具"。

并且**没有任何一层留证据**：工具消失后只能靠翻代码猜。

## P0-1：核心工具脱离 Top-K 竞争

新模块 `app/agents/skills/mandatory_tools.py`：

* `CORE_TOOLS` 是**唯一**的强制清单，当前只有 `workspace_navigator`。
  刻意最小：每加一个都挤占可选工具的空间，而"可选工具之间的竞争"本身是要保留的能力；
* `trim_with_mandatory(capabilities, limit=...)`：`limit` 的语义是**可选工具的预算**，
  强制工具额外占位。强制工具按候选池原顺序先占位（保留上层排序语义），可选工具再填
  `limit` 个，超出的记入 `trimmed_optional`；
* `apply_tool_window(...)`：**唯一**的"上限内选工具"入口，所有截断都改走它。

已经改过来的截断点（每处都带 `layer` 标签，日志里能定位是哪一处丢的）：

| layer | 位置 | 原来 |
| --- | --- | --- |
| `executor.select_capabilities` | `skills/executor.py` | Top-K 只看可选 |
| `chat_graph.cache_merge` | `langchain/chat_graph.py` | `[:8]` |
| `chat_graph.domain_discovery` | 同上 | `[:8]` |
| `chat_graph.final` | 同上 | `[:8]` |
| `react.workspace_stage` | `orchestration/react_runner.py` | `[: 8 - len(injected)]` |
| `react.domain_expand` | 同上 | `[:8]` |
| `react.bootstrap` | 同上 | `[:8]` |
| `react.cache_merge` | 同上 | `[:8]` |
| `react.document_discovery` | 同上 | `[:7]` 硬预留 |

**阶段工具也是强制项**：工作区写/执行/提交阶段注入的工具、探索原语
（`Read`/`Glob`/`Grep`/`run_in_sandbox`/`run_static_check`）、文档发现前置
（`inspect_document_set`）都属于"本轮明确要求"，同样不参与竞争——否则会出现
"注入了 8 个阶段工具 → 旧工具（含读工具）被清空"。

## P0-2：四层快照

`ToolWindowSnapshot` 记录并自动算逐层差集：

```text
1) catalog   系统知道哪些工具
2) eligible  场景/角色/权限过滤后
3) ranked    排序后的候选
4) final     真正传给模型的 tools
```

每条日志形如：

```text
[tool-window] layer=chat_graph.final scene=office limit=8 final=6
  pinned=workspace_navigator trimmed=web_search,calculator dropped_core=-
[tool-window] layer=... layers={'catalog': 24, 'eligible': 12, 'ranked': 9, 'final': 6}
  dropped={'catalog→eligible': [...], 'eligible→ranked': [...], 'ranked→final': [...]}
  visibility={'workspace_navigator': 'available', ...}
```

另外两点：

* `dropped_core`：**强制工具已经不在候选池里** = 上游（场景/权限/在线）把它过滤掉了。
  这种情况以前只表现为"模型说没有读工具"，现在直接告警并点名；
* 新增指标 `lumi_tool_window_final_tools{scene,layer}`：最终进模型的工具数趋势下降
  就是"有工具在被截断"的早期信号。

这些快照还会**按任务**落一份结构化副本（Redis list `tool_window:{job_id}`，最多 20 条、
TTL 24h，写失败静默），前端排障面板据此展示"这次模型到底拿到了哪些工具"：

```http
GET /api/v1/agents/jobs/{job_id}/tool-window?limit=20
```

字段含义与展示约定见 [`TOOL_REGISTRY_P1.md`](./TOOL_REGISTRY_P1.md#工具窗口诊断接口前端排障面板的数据源)
（关键：`final` 才是模型看到的集合；`dropped_core` 非空标红；`available=false` 只代表
没有诊断帧，不代表工具正常；窗口大小不是固定 8）。


## P1（本轮一并收口的部分）

**三态可见性** `visibility_state()`：`catalog`（系统知道）/ `eligible`（允许用）/
`available`（Provider 健康且租约有效）/ `unavailable`（允许但当前没有 Provider）。
Provider 暂时离线时，工具不再是"像从系统消失"，模型能区分"没有这个工具"与
"工具暂时不可用"。

**工具唯一标识** `tool_identity()` = `plugin_id|provider_id|name@version`。
`ToolDiscoverySession.loaded_tools` 的主键从裸工具名改成它——裸名会让两个插件提供的
同名工具后者静默覆盖前者。展示给模型的名称仍然简洁。

**注册表版本** `registry_epoch()` = ``ToolSpec`` 影子注册表（数量 + 限定名 + 版本 +
输入 schema 摘要）+ 能力目录（限定名 + 契约版本）的 hash。会话发现缓存的失效条件从
"只看工具域策略版本"改成 **同时看注册表版本**，于是插件安装/卸载、Provider 增删工具、
schema 升级都会立刻让旧会话缓存失效，而不是等 30 分钟 TTL。

## 明确**没做**的部分（避免误读）

评审里的 P1 大项——"用注册表替换所有静态映射表（`TOOL_CAPABILITY_MAP` /
`CAPABILITY_TOOL_MAP` / `ACTION_TOOL_WINDOW`）、让预检/Broker/模型注入全部从 Registry
派生"——**在本轮之后的 P1 收口里已经完成**：见
[`TOOL_REGISTRY_P1.md`](./TOOL_REGISTRY_P1.md)。四个判定维度（工具→能力 /
能力→MCP 目标 / 意图→工具窗口 / 工具→审批档位）影子对比**零差异**，插件与 Provider 的
声明（工具类属性 / 运行期能力对象 / `PluginManifest`）都已成为注册表输入。

仍未做的只有两件，且都不在本模块范围内：

* **删除历史业务路由正则**（`task_shape.py` / `task_preflight.py`）：需要先跑一段
  影子模式的命中率/误判率报告；
* **任务级 deadline 下传到后台 Job**、`/jobs` 读取独立连接池（稳定性议题）。


## 测试

`tests/capabilities/test_mandatory_tools.py`（13 例）：

* 8 个强相关写工具 + 核心读工具、上限 8 → 读工具仍在（**这条就是现场 bug 的复现**）；
* 上限 1 时核心工具仍在；核心工具按池原顺序占位；
* 被裁掉的可选工具**留痕**（不再是"悄悄消失"）；
* 本轮显式钉住的工具（写工具）一定在；
* 核心工具不在池里 → `dropped_core` 点名告警；
* 四层快照的层内计数与逐层差集正确；
* 三态可见性分得开；复合主键能区分同名工具；注册表版本稳定且可区分注册表变化。
