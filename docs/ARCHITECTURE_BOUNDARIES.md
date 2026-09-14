# 后端结构重构（方案 v2）—— 落地记录

配套方案：《Lumi 后端结构重构方案（修订版 v2）》。本文只记录**已经做完的事、实测数据、
以及与方案的偏差**，不重复方案文本。

```text
本次重构要解决的两件事
  1. 依赖方向错乱（orchestration → app.services.* 越界、转发壳成片、两代实现并存）
  2. 目录结构无法支撑继续膨胀（services 65 文件平铺、core 大杂烩、tests 平铺）

本次重构明确不做
  · 不改运行时行为（重试、降级、审批语义、审计写入模型都不动）
  · 不做领域模型重构（Memory 只改依赖方式，不改模型）
  · 不做审计语义统一（单独立项）
  · 不动 orchestration/models.py（150 处引用）
```

---

## 一、依赖方向（立法）

```text
lumi_contracts
      ↑
lumi_capability        （P3 才创建，只含纯决策）
      ↑
app/agents/capabilities（内部先纵切，P2）
      ↑
app/agents/orchestration
      ↑
app/api / app/bootstrap
```

横向（业务域之间）：`app/knowledge` `app/memory` `app/workspace` `app/office` 互相禁止
import 对方内部实现；允许直接依赖对方公共 DTO、只读查询模型、领域错误、稳定纯函数；
数据库仓储、Redis、文件系统、外部模型调用、编排任务、加密服务、工作区访问必须走 Port。

例外（方案明确放宽）：Memory 不得依赖 `app.agents.orchestration.models`，改走稳定的任务
引用协议即可，**不必**为此新建复杂 Port 抽象。

## 二、门禁：`tools/check_architecture.py`

AST 实现（不是 grep），与 `scripts/check_unsafe_calls.py` 同一风格。**为什么必须 AST**：
边界规则全长在 import 上，而有五种形状——`import a.b`、`from a import b`、
`from . import b`（相对导入）、`import a.b as c`、`importlib.import_module("a.b")`
（字符串式）。正则既会漏掉相对导入与动态导入，又会把文档字符串里的路径算成依赖。

| 规则 | 名称 | 模式 | 含义 |
| --- | --- | --- | --- |
| 1 | `packages_no_app` | **阻塞** | `packages/*` 不得 import `app.*` |
| 2 | `domain_isolation` | **阻塞**（P4 收尾转） | 业务域之间只走对方 `api` / 公共 DTO / Port |
| 3 | `agents_no_domain_impl` | **阻塞**（P4 收尾转） | `app/agents/**` 不得 import 业务域实现（只能用 `api`/Port） |
| 4 | `no_new_shim` | **阻塞** | 禁止新增跨包 re-export 兼容壳（白名单制，**P7 后为空**） |
| 5 | `package_layering` | 报告 | 包之间只允许既定依赖方向（等内核环裁决后收紧） |
| 6 | `core_no_domain` | 报告 | `app/core` 不得出现 domain 词（当前 0 条） |
| 7 | `pure_package_no_runtime` | **阻塞**（P3 加） | 纯决策包（`lumi_capability`）不得 import 运行时设施 |

**一条依赖只报一次**：`from app.knowledge.retrieval.knowledge import search` 在 AST 里是两条 ref
（模块 + 成员），按 ref 逐条报会把同一件事写两遍、让基线虚高一倍。现在按
`(规则, 目标域/包)` 归并，保留最短的模块路径——门禁陈述的边界事实是"这个文件依赖了那个域"。

### 基线纪律（存量不清零，但只减不增）

| 文件 | 作用 | 纪律 |
| --- | --- | --- |
| `tools/architecture_baseline.txt` | 已经存在的违规（`规则\|文件\|细节`） | 只应该**变短**；**新增违规阻塞** |
| `tools/compat_shims.txt` | 兼容壳白名单（每条写明理由与删除计划） | 只减不增；删壳必须同时删条目 |

两条测试把纪律钉死（`tests/structure/test_architecture_gate.py`）：

* `test_baseline_has_no_stale_entries` —— 修好的违规必须从基线消失（否则基线会变成
  "永久豁免清单"，CI 会提示运行 `--update-baseline`）；
* `test_compat_shim_whitelist_is_empty_after_p7` —— P7 之后白名单**必须为空**，
  且规则 4 的豁免数为 0；万一又出现条目，里面每条都必须仍是真实存在的纯壳。

### 用法

```bash
python tools/check_architecture.py                  # 全仓库，按规则表判定
python tools/check_architecture.py --rules 1,4      # 只跑指定规则
python tools/check_architecture.py --block 2,3      # 临时把规则 2/3 当阻塞试运行
python tools/check_architecture.py --update-baseline# 重写基线（只用于让它变短）
python tools/check_architecture.py --json           # 机器可读
```

CI 作业 `architecture`（阻塞）与本地 `scripts/pre-commit.sample` 使用同一入口。

---

## 三、P0 实测发现（不是猜的，是扫出来的）

当前基线 23 条，逐条都是真问题：

| 规则 | 条数 | 代表发现 |
| --- | --- | --- |
| 2 | 7 | `office → knowledge`（`office_docs` 11 处 rag 调用）、`memory → knowledge`（嵌入）、`memory → office`（`office_task_memory` → `office_docs`）、`workspace → knowledge`（`code_structure`） |
| 3 | 14 | `agents/*` 直接 import 办公/知识域实现：`chat_agent`→`scene_manager`、`builtin`→`office_docs`、`workflow_runner`→`rag.knowledge`… |
| 5 | 1 | **`lumi_orch` ↔ `lumi_execution` 互相依赖**（见下） |
| 6 | 1 | `app/knowledge/config.py` 是 knowledge 域的配置，却住在 core |

### 两个需要裁决的实测结论

1. **两个内核包成环**：`lumi_execution` → `lumi_orch` 只用 `job_spec` / `dag`
   （公共 DTO + 稳定纯函数，属于方案允许的直接依赖）；反向
   `lumi_orch.execution_mode` → `lumi_execution.step_contract` 是**共享词汇表**。
   环出现在"词汇表/DTO"层，裁决方向应是把共享词汇下沉到 `lumi_contracts`，
   **不要**把这条边加进允许集合（那等于把环藏起来）。规则 5 转阻塞前必须先做这一步。
2. **`app/knowledge/config.py`** 属于 knowledge 域，P5 的归宿是 `app/knowledge/config.py`
   （方案已写明），在此之前它由规则 6 持续报出来。

---

## 四、P1 已完成的搬家（纯搬家，行为零变化）

### 4.1 编排薄壳集中到 `app/agents/orchestration/adapters/`

`execution_mode` / `execution_policy` / `job_run_view` / `step_resume` / `step_sequence`
五个壳集中到一个子包，并**在包入口写下"新代码直接 import 内核包"的规矩**。
这五个壳当前**零引用**（调用点早已直接指向 `lumi_orch.*` / `lumi_execution.*`），
已登记 `tools/compat_shims.txt`，P7 统一删除。

### 4.2 资源租约壳：两层合并为一层（**与方案的偏差，理由是实测数据**）

方案写的是"`resources.py` 与 `resource_coordination.py` 两层壳合并为 `execution/resources.py`"。
实测冷启动 import 耗时：

| 模块 | 耗时 |
| --- | --- |
| `app.agents.resource_coordination` | **1.1s** |
| `app.agents.orchestration` | **26.5s** |
| `app.agents.orchestration.resources`（已删） | **43.8s** |

把租约适配器搬进编排包（含 `adapters/`）会让**原子工具执行路径**从 1.1s 变成 26.5s 起步，
这是运行时行为变化，违反"零行为变化"。因此实际做法是：

* 删除 `app/agents/orchestration/resources.py`（外层壳）；
* 保留 `app/agents/resource_coordination.py` 作为**唯一一层**，并把 3 个生产调用点
  （`execution/node.py` ×2、`temporal/activities.py` ×1）与 3 个测试调用点改指它；
* 在该模块 docstring 里写下实测数字，防止后来者"顺手把它搬进 orchestration"。

`app/agents/orchestration/adapters/__init__.py` 里也写明了这一点。

### 4.3 插件控制面 `app/services/plugins/` → `app/plugins/`

66 处引用一次性改完（含 `app/agents/sandbox/*`、`plugins/tools/shell/python_exec.py`、
`scripts/check_unsafe_calls.py` 的 `ALLOWED_MODULES`、CI 注释、文档）。
控制面（manifest/签名/配额/生命周期，Python 代码）与内容面（仓库根 `plugins/` 下的
YAML/工具实现）现在路径上分得清。

### 4.4 迁移脚本 `scripts/migrate_*.py` 等 → `scripts/migrations/`

9 个脚本（7 个 `migrate_*` + `reembed_vectors` + `rotate_memory_key`）移入子目录，
新增 `scripts/migrations/__init__.py` 写清与 `alembic/` 的分工
（alembic = 可重放 schema 迁移；这里 = 一次性、幂等、需人工确认的数据迁移）。
`reembed_vectors.py` 直接 import `app.*`，`sys.path` 基准已从 `parent.parent` 改为
`parents[2]`（测试会断言它仍然指向仓库根）。

### 4.5 P1 的验收测试

`tests/structure/test_structure_p1_layout.py`（22 例）：

* 新路径可 import、旧路径 `ModuleNotFoundError`（`RETIRED_MODULES` 逐个断言）；
* **全仓库 AST 扫描**：没有任何模块还引用旧路径（本文件自己排除——它必须写下旧名字）；
* 五个壳仍然覆盖内核公开面（按内核 `__all__`，不要求把 `re`/`dataclass` 这类
  实现细节也再卖一遍）；
* 资源租约适配器只有一层，且真实调用点确实指向它；
* **启动回归**：`app.main` 能构建 FastAPI 应用，OpenAPI schema 里能查到
  `/api/v1/plugins`、`/api/v1/agents/jobs/{job_id}/tool-window`、
  `/api/v1/admin/policies/tools`（FastAPI 0.141 的 `include_router` 是嵌套结构，
  `app.routes` 看不到子路由，所以断言 schema 才是权威）；
* **热重载回归**：`importlib.reload(app.plugins)` 后公开 API 完整且模块对象不变。

### 4.6 一次真实的翻车与教训（记下来，避免下次）

第一次搬完五个壳跑全量测试时，6 个测试模块在 **collection 阶段**就炸了：

```text
ImportError: cannot import name 'execution_mode' from 'app.agents.orchestration'
```

原因不是搬错了，而是**引用清点用的是 `git grep`**——它只搜**已跟踪**文件，而本仓库有一批
尚未 `git add` 的测试文件（`tests/orchestration/test_execution_mode.py`、`test_job_run_view.py`、
`test_unified_sse_contract.py` 等）正好引用了旧路径。

教训与对策：

1. 搬迁前清点引用要用**文件系统级搜索**（`rg`/编辑器全局搜索），不要只用 `git grep`；
2. 这类断裂应该在测试里被抓住，而不是在 collection 时才发现——所以
   `RETIRED_MODULES` 现在把**搬迁过的旧路径**（不只是"删除的路径"）也一并列为禁止引用，
   并且断言测试代码本身也不许依赖兼容壳（否则 P7 删壳时会连带炸掉测试）。

---

## 五、`var/logs`、`var/data` 出包（P0 只写方案，不执行）

问题：仓库根有 `logs/`、`data/`、`artifacts/`、`app/data/`、`app/logs/`，历史日志与上传产物
混在代码树里。方案要求收敛到 `var/logs` + `var/data` 并**单独提交**，因为它同时牵动
Docker、权限、部署路径与 `.env`。

**执行清单（留给独立改动，不在本次重构里做）**：

1. 先把 `settings` 的默认路径改成 `var/logs` / `var/data`（保留环境变量覆盖，
   部署方仍可挂载到别处）；
2. Dockerfile / `docker-compose*.yml` 的 volume 映射同步改并挂载 `var/`；
3. 迁移历史文件（`git mv` 受版本控制的样本，其余由运维在部署机上移动）；
4. `.gitignore` 收紧为 `var/`，并检查 `logs/`、`data/`、`artifacts/`、`app/data/`、
   `app/logs/` 是否都可以从忽略清单里删掉；
5. 启动时确保目录存在（`mkdir(parents=True, exist_ok=True)`）——这一步已经在
   `app/main.py` 的 `UPLOAD_DIR/chat` 上有先例，按同一写法处理；
6. 上线顺序：先发"写新路径 + 兼容读旧路径"的版本，再发"只读新路径"的版本，
   避免滚动发布期间两个副本写不同目录。

---

## 六、P2 已完成：能力域内部纵切（不出 package，不改行为）

`app/agents/capabilities/` 从 21 个平铺模块整理成 7 个子包：

```text
app/agents/capabilities/
├─ contracts/   context                     授权事实（纯数据）
├─ catalog/     legacy / resource / tool_registry / tool_tables   目录声明（两代并存）
├─ registry/    registry / builtin / resolver                     Provider 协议与注册表
├─ policy/      gate / policy_guard / policy_packs / approvals / routing /
│               resource_window / resource_workflow               门禁裁决与策略（纯决策）
├─ broker/      broker / dispatch / resource_dispatch             选择与派发
├─ audit/       audit                                             审计写入（只搬目录）
└─ views/       views / snapshots / resource_surface              只读投影
```

三条实现纪律：

1. **子包 `__init__.py` 刻意不做 re-export**：包级稳定入口由
   `app/agents/capabilities/__init__.py` 独占（`__all__` 66 个名字与 P2 之前逐字相同），
   多一层转发只会把运行时适配拖进纯决策模块的导入链（P3 要按依赖方向判断）。
2. **只有两个模块改名**（`catalog.py → catalog/legacy.py`、`resource_catalog.py → catalog/resource.py`），
   其余保留原基名——改名的收益（少几个 `broker/broker.py`）不值得多一倍改写量；
   `legacy` 这个名字本身就在陈述"两代并存"。
3. **大文件禁止整文件搬迁**（方案 §三）：`tool_registry.py`（原 1612 行的"热文件"）
   按"纯决策 / 运行时适配 / 配置加载 / 业务适配"逐段标注（段标写在文件里），并拆出两段：

| 段 | 类别 | 结果 |
| --- | --- | --- |
| 静态词表（意图副作用、档位例外、能力/副作用档位、环境词汇） | 配置加载 | ✅ 拆到 `catalog/tool_tables.py`（180 行），`tool_registry` 原样再导出 |
| 影子对拍（`shadow_compare` / `shadow_parity_totals` / `log_shadow_differences`） | 诊断投影 | ✅ 拆到 `views/tool_shadow.py`（226 行），叶子消费者，8 个调用点直接改路径（不留转发壳） |
| 条目装配与缓存 / 影子注册表 / Provider / 运行期能力读取 | 运行时适配 | **决定不拆**（理由见下） |
| 声明解析、档位与审批派生 | 纯决策 | 留在原处，作为 P3 抽包候选，**禁止**在此引入 IO |

`tool_registry.py` 1612 → 1232 行。**"运行时适配"段为什么不再拆**：它与查询入口、纯决策段
**双向调用**（`entries_by_name` → `build_registry_entries`；`risk_tier_of` → 条目表 →
`_static_capability`），再拆一层只能靠把"函数内延迟 import"散布到多个模块来解环，
会把最热路径的调用关系切碎。而且它**最终的归宿是 P3 的反面**：P3 把纯决策段抽进
`lumi_capability`，剩下的运行时适配本来就该留在 `app/`——现在不拆，等 P3 按类搬运，
边界反而更清楚。这个决定与理由都写在文件自己的四分类表里。

### 6.1 两代实现的对照结论（悬置决策的第一半）

方案要求 `legacy` 与 `resource` 两代"先行为对照，后裁决"，对照报告在
**`docs/CAPABILITY_TWO_GENERATIONS.md`**。要点：

* 四个对拍维度（`tool→capability`、`capability→mcp_target`、`intent→tool_window`、
  `tool→risk_tier`）实测**各 0 行差异**，`switch_safe=True`；
* 新代是**严格超集**（26 个工具新独有、0 个旧能力丢失），但有**三处结构性缺口**：
  Broker 不认资源类型（`select()` 无 `resource_type`/`provider_ids`）、
  Provider 声明与真实注册表不对齐（`memory_provider.registered=True` 却无注册项等）、
  旧代回归保护约为新代 3 倍（≈307 vs ≈101 专属用例）；
* **裁决**：新代做"声明与解析"的权威，旧代继续做"Broker/租约/审批的键与在线路径"的权威；
  补完三处缺口再按开关切换，**不做**"删旧代码"式裁决。

### 6.2 P2 的验收测试与两次翻车

`tests/structure/test_structure_p2_layout.py`（35 例）：子包布局可导入、16 个旧扁平路径彻底消失、
5 个"旧模块名变成子包"的语义变化（含 `catalog.catalog` 已改名）、
**包级公开面 66 个名字逐字钉住**、AST 扫描禁止旧扁平路径与
`from app.agents.capabilities import <子模块>` 形状、静态词表已拆出且**同一对象再导出**、
影子对拍拆出后结论逐字不变（`parity_total=0`、`switch_safe=True`）、大文件四分类段标存在。

三次真实的翻车（都记下来）：

1. **搬家脚本把点号前缀写成了路径前缀**：`dotted_repl` 复用了 `PKG = "app/agents/capabilities"`，
   产出 `from app/agents/capabilities.registry.builtin import …` —— 820 个语法错误。
   教训：路径前缀与点号前缀**必须是两个常量**；好在形状唯一（正常路径引用带斜杠结尾），
   可以全局精确还原。
2. **`from 包 import 子模块` 这种形状正则扫不到**：第一轮改写只匹配 dotted 路径，
   漏了 8 个测试文件（collection 阶段才炸）。教训与 P1 相同——引用清点必须用 AST，
   P2 的测试因此直接用 `ast.ImportFrom` 判定，并只放行"包级公开名"。
3. **删行用的正则把 `__all__` 与文档字符串改坏了**：`re.sub(r"\n(?:    \"|)", "\n", …)`
   里的空分支让整个模式匹配**每一个换行**，于是所有"4 空格 + 引号"的行首被吃掉
   （88 行缩进 + 9 个 `__all__` 条目）。教训：**永远不要用"带空分支的正则"做文本手术**；
   正确做法是按行处理，或直接按块（`__all__ = [` … `]`）精确删除。修复方式是
   **从 HEAD 原文重放全部四步确定性变换**（import 重写 → 拆静态词表 → 拆影子段 → 标注），
   而不是逐行猜哪里被吃——重建比重修可靠。

## 七、P3 已开工：`lumi_capability` 第一批（协议与纯函数）

`packages/capability/`（`lumi_capability`）已建立并接入 uv workspace（`uv.lock` 已更新，
CI 的 `uv sync --locked` 照常工作）。已抽两批，**只抽纯决策**，七个模块：

| 模块 | 内容 | 原位置 | 批次 |
| --- | --- | --- | --- |
| `vocabulary` | 统一能力名/别名/资源类型 + 归一 | `catalog/resource.py` | 1 |
| `tiers` | 契约副作用词表 + 档位算法（`side_effect_tier` / `stricter` / `normalize_tier` / `floor_for_local_confirmation` / `manifest_tier_of` / `descriptor_declared_tier` / `merge_declared`） | `catalog/tool_registry.py` | 1 |
| `deployment` | `descriptor_allows_deployment` | `registry/registry.py` | 1 |
| `selection` | `select_lease`（能力 → 绑定 → 资源收窄 → 有效期 → 健康 → 心跳） | `broker/dispatch.py` | 1 |
| `fingerprint` | `capability_fingerprint`（审批绑定与审计定位的唯一对象） | `policy/policy_guard.py` | 2 |
| `audit` | 审计记录结构（`CapabilityAuditRecord` / `audit_record`）+ 本地拒止转换 + 过程条目 | `audit/audit.py` | 2 |
| `gating` | 步骤级执行前门禁（`NodeCapabilityGate` / `resolve_declared` / `evaluate_node_capabilities` / `node_capability_failure`） | `policy/gate.py` | 2 |
| `state` | 可见性状态机（五个状态 + 映射 + 排序；池词表可注入） | `catalog/resource.py` | 3 |
| `planning` | 工具窗口计划（`WindowPlan` / `WindowPlanning` / `plan_window` / `plan_names` / `read_guards` / `canonical_tool_for`） | `policy/resource_window.py` | 3 |

四条落地规矩：

1. **算法进包，表/目录/词表留在 app 并通过参数注入**：
   * `tiers` 只带契约词表；应用侧 `SIDE_EFFECT_TIER` 多一个合成名 `publish`，通过 `table=` 传入
     （所以 `tool_registry.side_effect_tier(["publish"]) == "critical"` 而
     `lumi_capability.side_effect_tier(["publish"]) == "routine"`——这个差异是**设计**，测试钉住了它）；
   * `gating` 不认识应用的能力目录与抽象词表：`catalog=` 与 `abstract_map=` 由 app 注入
     （app 侧 `evaluate_node_capabilities(node)` 不传目录时仍然用应用单例 `capability_catalog`）。
2. **app 侧调用面零变化**：抽走的公开名在 app 模块里是**同一对象**再导出（测试断言 `is` 相同），
   原函数保留原签名、内部委托（`select_lease` 仍吃 `AgentExecutionContext`，
   回退打点用 `warn=logger.debug` 回调——**纯包不依赖日志库**）。
3. **纯包不许长出 IO**：门禁规则 7（**阻塞**）——`lumi_capability` 不得 import
   `redis/fastapi/starlette/sqlalchemy/alembic/celery/temporalio/langchain/httpx/requests/loguru/app/lumi_orch/lumi_execution`，
   字符串式动态导入也扫。
4. **运行时适配明确不抽**：审计的进程内缓冲（`CapabilityAuditLog`）与"要不要审计"开关
   （`audit_enabled`，读策略包配置）留在 app；`CapabilityRegistry`、Broker、派发、租约、
   Provider 健康检查、FastAPI 视图、settings、持久化、前端投影全部留在 app。

### 7.0.1 一个明确的"不搬"决定

`catalog/tool_tables.py`（198 行纯数据）**不搬进包**。它是**应用侧配置**：
`TOOL_TIER_OVERRIDES` / `LEGACY_TIER_TOOLS` 表里写的是本仓库的工具名，
`INTENT_*` 是本仓库的动作词表。搬进"可被第二个服务复用的纯决策包"会把应用配置伪装成协议。
正确的分工已经在 `tiers` 上体现了：**词表与算法进包，具体表由应用注入**。

### 7.1 本批的验收

* `packages/capability/tests/`（113 例）：纯函数单测，**不 import app**——它们能跑起来本身就是边界证据；
* `tests/structure/test_structure_p3_package.py`（21 例）：workspace 成员与依赖声明（只 `lumi-contracts`）、
  app 侧再导出是同一对象（词表/状态/审计/指纹/门禁/窗口计划逐一断言）、
  应用表与目录注入后行为逐字一致、`select_lease` 签名不变、`gate` 注入目录与抽象词表、
  窗口计划三不变量、规则 7 的对抗性用例；
* CI 的单测范围从 `tests packages/orchestration/tests` 扩到 `tests packages`，
  这样新包的单测在 CI 里真的会跑（否则"抽了但没人测"）。

### 7.1.1 P3 v1 收口：方案清单逐项对账

| 方案 §P3「进包」项 | 结果 |
| --- | --- |
| 能力描述协议（descriptor） | **无需搬迁**：`CapabilityDescriptor` 本来就在 `lumi_contracts.plugins` |
| Provider / Lease 数据模型 | **无需搬迁**：`ProviderLease`/`ProviderRef` 等已在 contracts |
| 能力状态机 | ✅ `state`（五状态 + 映射 + 排序） |
| 纯门禁裁决 | ✅ `gating`（执行前门禁）+ `tiers`（档位裁决） |
| 能力匹配与选择算法 | ✅ `selection`（候选择优）+ `planning`（窗口计划/规范工具/读保护） |
| 审计记录结构 | ✅ `audit`（记录 + 拒止转换 + 过程条目）+ `fingerprint` |
| capability 错误码 | **无需搬迁**：`CapabilityErrorCode` 已在 contracts |

| 方案 §P3「不抽」项 | 结果 |
| --- | --- |
| Redis 租约实现 / Broker / MCP 派发 / Provider 健康检查 / FastAPI 视图 / settings / 持久化 / 前端投影 | 全部留在 app ✔ |
| 完整 Registry | 留在 app ✔（它持有能力目录与运行期状态；方案本来也只要求"最后考虑"） |
| 应用配置表（工具名例外、动作词表、Provider 声明） | 留在 app ✔，通过参数注入纯算法（见 §7.0.1） |

**P3 到此收口。** 后续若要继续抽（例如 `capability_lease` 的纯状态转移），
前置条件是先把"Lease 生命周期"从 Redis 读写里分离出来——那是 P4/P5 之后的事。

### 7.2 侦察结果（已消化，供后续参考）

对每个候选模块做了 AST 侦察（模块级 import + 函数内延迟 import）：

| 候选 | 模块级依赖 | 函数内 app 依赖 | 结论 |
| --- | --- | --- | --- |
| `catalog/tool_tables.py`（198 行） | **零依赖**（纯数据） | 无 | **不抽**（应用侧配置，见 §7.0.1） |
| `catalog/resource.py::resource_visibility` / 状态常量 | `loguru` | `skills.registry`、`skills.mandatory_tools` | ✅ 已拆：状态机进包，事实读取留 app（第三批） |
| `policy/resource_window.py::plan_for_actions`（449 行） | `catalog.resource`（应用表） | `catalog.tool_registry`、`feature_flags` | ✅ 已拆：计划算法进包，词表与三个查询注入（第三批） |
| `tiers` 里的 `risk_tier_of` / `approval_policy_of` | — | 条目表 | **不抽**：它们是运行时适配（吃注册表条目），只委托包内纯核 |

已存在于 `lumi_contracts` 的东西**不再抄一份**：`CapabilityDescriptor`、`CapabilityResult`、
`CapabilityErrorCode`、`CapabilityInvocation`、`ProviderLease`、`PluginManifest`、
`CapabilityStatus`——"能力描述协议"与"capability 错误码"两项因此**无需搬迁**
（事实来源就在 contracts），Provider/Lease 的数据模型同理。

### 7.3 不抽的清单（方案 §P3 明确）

Redis 租约实现、Broker、MCP 派发、Provider 健康检查、FastAPI 视图、settings 读取、
数据库持久化、前端投影，以及一切"读应用目录/工具注册表"的代码（那是运行时适配）。
`CapabilityRegistry`（进程内注册表）也留在 app——它持有 `CapabilityCatalog` 与运行期状态。

### 7.4 前置：两代裁决的三处缺口（见 §6.1）

`lumi_capability` 抽的是**新代**的纯部分（严格超集），但"谁是权威"仍受三处缺口约束：
Broker 收窄、Provider 声明对齐、旧测试迁移。P3 只做"抽纯代码"，不在这三处解决前切换真相源。

### 7.4 运维影响（本轮唯一需要别人配合的地方）

新增 workspace 成员会牵动**构建与部署**，这几处已同步改好：

| 位置 | 改动 | 不做的后果 |
| --- | --- | --- |
| `pyproject.toml` | 根项目依赖加 `lumi-capability>=0.1.0` | 应用的 `import lumi_capability` 在干净环境里失败 |
| `uv.lock` | `uv lock` 更新（`uv lock --check` 通过） | CI 的 `uv sync --locked` 直接失败 |
| `Dockerfile` | 元数据层补 `packages/contracts`、`packages/capability` 的 pyproject；源码层补两个包目录；本地安装行补这两个包；运行期依赖过滤从"排除 orchestration/execution"改成**排除所有 `lumi-*`**；导入自检加 `lumi_contracts`/`lumi_capability` | 镜像里没有 `lumi_capability`，容器起不来 |
| `.github/workflows/ci.yml` | 单测范围 `tests packages/orchestration/tests` → `tests packages` | 新包的单测在 CI 里根本不跑 |

⚠️ **顺手修掉一个既有的隐患**：Dockerfile 原来的过滤只排除 `lumi-orchestration` / `lumi-execution`，
于是 `lumi-contracts>=0.1.0` 会进 `runtime-requirements.txt`，被 `pip install -r` 拿到**公共索引**
去解析——本地包不该也不能从远端解析。现在过滤条件是 `lumi-` 前缀，四个本地包统一走
"复制源码后 `--no-deps` 安装"。本机没有 Docker，**这一条要在下一次镜像构建里验证**。

其他部署方式：任何用 `uv sync` / `pip install -e .` 的环境会自动带上新成员；
如果部署脚本手写 `pip install ./packages/xxx`，需要补 `./packages/capability`（顺带补上一直
漏掉的 `./packages/contracts`）。

**前端：本轮无需任何改动**（不改 API/事件/TS 契约/开关语义）。

## 八、P4 域 1（Knowledge）已完成

知识域的家现在在 `app/knowledge/`，按**数据流**分子包（不再平铺在 `app/services/` 里）：

```text
app/knowledge/
├─ config.py                 ← core/rag_config.py      全局 RAG 配置（Redis 覆盖 + .env 兜底）
├─ parsing/                  文档 → 文本 → 分块 → 分类/质检
│   document_parser, docling_parser, chunker, cleaner, classifier
├─ embedding/                文本 → 向量
│   embeddings, sparse_embeddings, code_embedding
├─ retrieval/                查询 → 改写 → 召回 → 重排
│   knowledge（1012 行，空间/文档管理 + pgvector 检索）, query_rewriter, reranker, scope
├─ code/                     本地代码结构索引
│   code_structure, project_index
└─ information_resolver      信息源解析
```

* **公开面逐字不变**：`app/knowledge.__all__` 与搬迁前的 `app.knowledge.__all__` 相同
  （29 个名字），因此兼容层可以逐字再导出；
* **兼容层**：`app/services/rag/__init__.py` = `from app.knowledge import *`，
  已登记 `tools/compat_shims.txt`（规则 4 白名单，P7 删除）；
* **调用点全部改到新路径**：生产代码里 `office_docs` / `orchestrator` / `celery_app` /
  `plugins` / 迁移脚本都直连 `app.knowledge.*`（方案要求"office_docs 改走 knowledge 公开接口"）。

### 8.1 门禁与基线的净收益

| 指标 | 迁移前 | 迁移后 |
| --- | --- | --- |
| 规则 6（`app/core` 不得出现 domain 词） | 1 条（`core/rag_config.py`） | **0 条**（文件已离开 core，且改名 config） |
| 规则 3（agents 不碰域实现） | 14 条 | 13 条（`tools.py` 的两个引用合并为一条） |
| 基线总量 | 23 条 | **22 条** |

### 8.2 两种"漏掉"的引用形状（P2 教训重演，这次两种都补了）

第一次改写只覆盖了点号路径，漏掉两个形状，都是 collection/import 阶段才炸：

1. **`from app.services import project_index`**（从包取子模块）——4 处（`agents/core/tools.py`、
   `api/v1/projects.py`、`repositories/project_repository.py`、`plugins/.../create_task_plan.py`）；
2. **`from app.knowledge import knowledge`**——第一遍把**包名**改成了 `app.knowledge`，
   子模块名却留在原地，于是变成 `from app.knowledge import knowledge`（4 处）。

对策（与 P2 相同）：引用清点必须同时覆盖"点号路径"和"从包取子模块"两种形状；
每类改写后立刻全仓库再扫一遍残留。**附带损害**：`tests/structure/test_architecture_gate.py` 里的
**合成样例字符串**也被改写了（如 `app.core.rag_config` → `app.knowledge.config`），
导致两条门禁测试失效——已改成"与迁移状态无关"的样例（`app.core.memory_store`、
`from ..knowledge.retrieval import knowledge`），这样它们验证的是**规则**而不是当前目录树。

### 8.3 顺带发现的一个真问题（不是本次改动引入的）

```text
import app.repositories.project_repository
  → app/repositories/__init__.py  (eager 导入 job_repository)
  → app/agents/orchestration/models.py
  → app/agents/orchestration/__init__.py  (eager 导入 orchestrator 单例)
  → orchestrator.py → fork_service.py → app.repositories.job_repository  ← 半初始化，ImportError
```

也就是说 **`app.repositories` 先于编排包被导入时会炸**（反之则没事）。
它与本次迁移无关（两处改动都在方法体内），但它是"编排包入口太重"的直接后果——
`app/agents/orchestration/__init__.py` 为了"启动时拿到单例"而 eager 导入 orchestrator。
**处置**：记在这里，作为 P5/P7 的候选（让该 `__init__` 惰性化，与 `adapters/` 的处理同一思路），
本次不动（改它要动最热的模块，且不属于 P4 的范围）。

另一处已知耦合：`app/contracts/__init__.py` 反向 import 了 `app.knowledge.code.code_structure`
（契约层 → 领域实现）。它在规则 2/3 的视野之外，是否要收（把 code_structure 的公开面提升为契约）
留待 P5 一起判断。

### 8.4 运维 / 前端

**前端：无需任何改动**（不改 API、事件、TS 契约与开关语义）。
**运维**：无镜像/依赖变化——`app/knowledge/` 在 `app` 包内，`Dockerfile`/`uv.lock` 不受影响。
文档里引用 `app/services/rag/...` 的路径已随改写更新（`docs/`、`scripts/`、注释）。

## 八点五、P4 域 2（Workspace）已完成

工作区域的家现在在 `app/workspace/`，**读 / 写按子包分侧**（方案对 navigator/operations 的要求）：

```text
app/workspace/
├─ service.py        ← services/workspaces.py           工作区本体：创建/列出/删除/设备与会话绑定
├─ context.py        ← services/workspace_context.py     上下文：权限画像/可见性/健康探测（读写共用）
├─ read/             只读一侧
│   navigator        ← services/workspace_navigator.py（2447 行：list/search/read/scan 四个 handler）
│   reader           ← services/workspace_reader.py（统一分段文本投影）
└─ write/            写入一侧
    operations       ← services/workspace_operations.py（1699 行：操作网关）
    revision         ← services/workspace_revision.py（修订值/换行策略/替换）
    trash            ← services/workspace_trash.py（回收站）
```

**为什么读写要分侧**：读路径要尽可能宽（先读才能改），写路径要尽可能窄（每个写操作都要过
写闸、修订校验、回收站）。混在一个模块里时，"加一个读能力"和"加一个写能力"看起来一样，
评审看不出风险差别；分成两个子包后，review 的默认预期就不同了。实测两个大文件的分工是
**干净的**：navigator 里 0 个写函数、operations 里 0 个读函数，所以这一刀正好落在模块边界上。
文件内部另加了段标（`[纯决策]` / `[运行时适配]` / `[读]` / `[写]`），进一步拆分的收益不大、
风险不小，因此**不做**（理由写在 `app/workspace/__init__.py`）。

**没有兼容层**：这 7 个模块过去是各自被 import 的（没有聚合入口），一次干净迁移即可
（111 处引用全部改完），不为它们新增 7 个转发壳——白名单只减不增。

### 8.5.1 这一轮又踩的坑：**本地名**变了

第一遍把 `from app.services import workspaces` 改成了 `from app.workspace import service`，
但函数体里仍然写 `workspaces.bind_workspace_to_conversation(...)` ——模块路径换了、**本地名**没换，
名字就没了（`NameError`）。ruff 只在部分情况下报 `F401`，靠它兜底不可靠。

正确做法是：**搬家时把新模块按旧名起别名**（`import service as workspaces`），
调用点一个字都不用改；"给模块改名"是另一件事，不该混在纯搬家那一刀里。
共 7 个文件按此修好（`app/api/v1/agents.py`、`chat.py`、`conversations.py`、`workspaces.py`、
`app/workspace/context.py`、两个测试）。

**三次搬迁（P2/P4-1/P4-2）的引用清点清单已经稳定下来**，每一步都要过：

1. 点号路径（`app.services.workspace_navigator`）；
2. `from 包 import 子模块`（`from app.services import workspaces`）；
3. **符号/本地名**（`workspaces.xxx`）——换路径时要么保留旧名做别名，要么同步改所有使用处；
4. 字符串形式（`importlib.import_module("...")`）；
5. 文档/注释里的**路径形式**（`app/services/workspace_navigator.py`）；
6. 测试里的**合成样例字符串**（改错了会让门禁测试"假通过/假失败"，见 §8.2）。

## 八点七、P4 域 3（Memory）已完成：含一次**反向依赖的切断**

```text
app/memory/
├─ conversation.py   ← services/conversation_memory.py   对话记忆（窗口/摘要）
├─ trim.py           ← services/conversation_trim.py     对话裁剪
├─ office_tasks.py   ← services/office_task_memory.py    近期办公任务索引与保守召回
├─ long_term/        跨会话长期记忆
│   extraction / retrieval / privacy / profile / lifecycle  ← services/memory/*
└─ repository.py     ← repositories/memory_repository.py 仓储 **Port**（编排层可依赖）
```

### 8.7.1 切断 `office_task_memory → orchestration.models`（方案点名的前置）

过去它直接 import 编排模型的三个枚举/类：

```python
from app.agents.orchestration.models import Job, JobStatus, TaskStatus
```

现在它只声明一个**轻量 Protocol**，并把状态比较从"枚举成员"改成"枚举的字符串值"：

```python
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "interrupted"})
_COMPLETED_STATUS = "completed"

def _status_value(status): return str(getattr(status, "value", status) or "")

@runtime_checkable
class TaskSnapshot(Protocol):        # 只声明"我需要任务的哪几个字段"
    job_id: str; user_id: str; scene: str; request: str
    conversation_id: str | None; status: Any; result: Any; routing: Any; nodes: Any
```

于是编排改枚举、改模型都不再牵动记忆域（方案也明确说"**不必**新建复杂 Port 抽象"）。
验证：`office_tasks.py` 里现在只剩注释提到 orchestration；规则 3 也没有因此新增违规。

### 8.7.2 第一次往 `DOMAIN_PUBLIC` 里填东西：把"仓库 Port"合法化

`app/repositories/memory_repository.py` 搬进 `app/memory/repository.py` 之后，
编排层（`memory_service` / `orchestrator`）对它的依赖在规则 3 里变成"agents → memory 实现"。
但这**正是方案允许的那类跨域依赖**——数据库仓储必须走 Port。处理方式不是加基线，
而是把它写进白名单语义的 `DOMAIN_PUBLIC["memory"]`：

```python
DOMAIN_PUBLIC = {"knowledge": (), "workspace": (), "memory": ("app.memory.repository",), "office": ()}
```

**区别很重要**：进基线 = "已知违规、暂不处理"；进 `DOMAIN_PUBLIC` = "这是被批准的依赖形状"。
规则 3 拦的是**实现**，不是 Port。这也是 P4 收尾（把各域公共面填全 → 规则 2/3 转阻塞）的第一步。

连带的清理：`app/repositories/__init__.py` 不再转发记忆仓储（否则这个聚合入口会变成
**跨包桥接**，规则 4 立即拦下）——同一个形状只能有一个家，调用方直接
`from app.memory.repository import ...`。

### 8.7.3 第四次踩同一个坑，这次定位到**顺序**

`from app.services.memory import extraction, profile as profile_svc, retrieval` 这类
"从包取子模块"的形状又漏了一次（`extraction` 等现在住在 `app.memory.long_term`）。
根因是**改写顺序**：脚本先做点号替换（`app.services.memory` → `app.memory`），
之后"从包取子模块"的匹配规则就认不出这个包了（它只匹配旧包名）。

**顺序规则（加进清单）**：先处理"从包取子模块"（需要旧包名才能识别），**再**做点号替换。
完整清单现在是：

1. `from 包 import 子模块`（**先做**，否则被第 2 步掩盖）；
2. 点号路径；
3. 符号/本地名（换路径时按旧名起别名，或同步改所有使用处）；
4. 字符串形式动态导入；
5. 文档/注释里的路径形式；
6. 测试里的合成样例字符串。

## 八点八、P4 域 4（Office）已完成 —— 四域迁移收口

```text
app/office/
├─ docs.py         ← services/office_docs.py（1524 行：结构化编辑引擎 + 产物管理）
├─ context.py      ← services/office_context.py（可信办公资料上下文）
├─ skill_utils.py  ← services/office_skill_utils.py（LLM 调用封装 + 合规敏感词）
├─ stream.py       ← services/office_stream.py（短生命周期文本流）
└─ render.py       ← services/document_renderer.py（确定性渲染器）
```

### 8.8.1 与方案的一处**有意的偏差**：三个模块没有搬进 office

方案把 `prompts` / `scene_manager` / `response_format` 列在 Office 名下，但实测它们是
**跨角色、跨场景**的：

| 模块 | 实际使用者 | 为什么不是办公专属 |
| --- | --- | --- |
| `prompts` | `agents/chat_agent`、`orchestration/react_runner`、`api/v1/prompts`、`api/v1/preferences` | 角色提示词服务（内置 `app/prompts/*.md` + 用户自定义），与办公无关 |
| `scene_manager` | `agents/chat_agent`、`agents/skills/workflow_runner`、`plugins/tools/network/query_knowledge` | 聊天/办公/代码场景共用模板与知识范围映射 |
| `response_format` | `orchestration/temporal/activities`、`office_skill_utils`、`scene_manager` | 面向聊天气泡的排版约定 |

把它们塞进 `app/office/` 会**制造**新的跨域依赖（`agents → office`、`orchestration → office`），
与方案自己的规则 2/3 直接冲突——那是把边界改坏，不是改好。因此：

* 本轮只搬 5 个办公专属模块；
* 那三个**暂时仍按 office 分类**（门禁里保留 `app.services.*` 前缀），
  于是既有的跨域违规**继续可见**，不会被悄悄藏起来；
* 它们的归属留给 P5（更像 `app/platform` 的展示/提示词设施）。

`app/agents/roles/office/agents.py` 按方案要求**留在 agents**（它是 Worker 角色/协议，不是领域逻辑）。

### 8.8.2 顺手改进门禁：目标细节优先取**真实存在**的模块

`from app.office import docs` 会产生两条 ref（`app.office` 与 `app.office.docs`），
原来的"最短者胜"会让基线细节在"换一种 import 写法"时漂移。现在 `_prefer_module`
**优先选磁盘上真实存在的模块**，同类里再比长度——基线更稳、细节更准。

### 8.8.3 P4 四域收口对账

| 域 | 新家 | 模块数 | 关键动作 |
| --- | --- | --- | --- |
| Knowledge | `app/knowledge/` | 16（4 个子包 + config） | 兼容层 `app/services/rag/__init__.py`（P7 删）；规则 6 清零 |
| Workspace | `app/workspace/` | 7（read/ write/ 分侧） | 读写分侧落成子包边界；两个大文件加段标 |
| Memory | `app/memory/` | 10（+ `long_term/`） | **切断** `office_task_memory → orchestration.models`；`repository` 作为 Port 进 `DOMAIN_PUBLIC` |
| Office | `app/office/` | 5 | 三个跨角色模块**有意不搬**（见 §8.8.1） |

四域迁完的净效果：`app/services/` 从 65 个平铺模块降到 **约 30 个**（且剩下的多为跨域应用服务），
`app/services/{rag,memory,plugins}` 三个子包分别变成兼容层/消失/搬到 `app/plugins`。

**门禁基线**：23 → **22 条**（规则 6 清零、规则 3 一条合并）；两次基线的"减"都来自真实修复，
没有靠刷新基线掩盖问题。

**搬迁工具箱（四次搬迁沉淀，按顺序执行）**：

1. `from 包 import 子模块`（**必须最先做**，否则被第 2 步的包名替换掩盖）；
2. 点号路径（长的排前面，避免前缀误替换）；
3. **本地名**：换模块名时按旧名起别名（`import docs as office_docs`），调用点零改动；
4. 字符串形式动态导入（`importlib.import_module("...")`）；
5. 文档/注释里的**路径形式**（`app/services/xxx.py`）；
6. 测试里的**合成样例字符串**（改错会让门禁测试假通过/假失败）；
7. 改完立刻用同一套模式**全仓库复扫**（含未跟踪文件）——`git grep` 只看已跟踪文件，不够。

## 八点九、P4 收尾：域隔离从"报告"变成 **CI 硬约束**

这一轮的目标是**逐条裁决**，而不是刷新基线。做法是给每个域一个**显式的公开接口模块**，
让"跨域依赖"这件事有一个可审计的落点：

```text
app/knowledge/api.py    20 个名字：检索/入库管线、解析、嵌入、代码索引、信息源解析
app/office/api.py        9 个名字：产物解析与目录、渲染、执行期文本流、办公 LLM 封装与路由哨兵
DOMAIN_PUBLIC = {
    "knowledge": ("app.knowledge.api",),
    "workspace": (),
    "memory":    ("app.memory.repository",),   # 仓储 Port
    "office":    ("app.office.api",),
}
```

**新规矩（写进门禁注释，也写进这里）**：跨域只准 import 对方的 `api` 模块（或登记过的 Port）。
`api.py` 是一处**可审计的公开清单**——新增跨域依赖时，作者必须先把名字加进对方的 api，
也就是必须先想清楚"这是公开契约"。它是**同包聚合**，不是跨包转发壳，因此不触规则 4。

### 8.9.1 基线：22 → **2** 条

| 处置方式 | 条数 | 说明 |
| --- | --- | --- |
| 改走对方 `api`（+ 声明 `DOMAIN_PUBLIC`） | 17 | 规则 2 全部 7 条 + 规则 3 的 10 条 |
| **重新分类**（不是业务域） | 3 | `chat_agent→prompts`、`react_runner→prompts`、`workflow_runner→scene_manager` |
| 保留在基线（写明理由） | **2** | 见下 |

关于"重新分类"那三条：`prompts` / `scene_manager` / `response_format` 是**跨角色、跨场景**的
提示词与展示设施（聊天场景、ReAct、办公都在用）。它们当初被归到 office 是**目录分组**，
不是依赖事实；留在 office 名下只会让规则 3 报出假违规、把真正的耦合淹掉。
（顺带说明为什么这不算"把问题藏起来"：它们**从未**是 office 的私有实现，
把它们算作域才是错的；归属在 P5 定，大概率进 `app/platform`。）

保留下来的两条（**都写明了理由**，不是"先放着"）：

| 基线条目 | 为什么现在不动 |
| --- | --- |
| `3\|app/agents/roles/office/agents.py\|agents→office: app.office` | 办公 Worker **本身就是办公执行者**，它直接组合办公域引擎。要改走能力层，得连 Worker 协议一起动——那是 P5 的议题，不该塞进"收尾" |
| `5\|lumi_orch/execution_mode.py\|lumi_orch → lumi_execution` | 两个内核包在**共享词汇表**上成环（`lumi_execution` → `lumi_orch.job_spec/dag` 是允许的 DTO 依赖，反向是词汇表）。裁决方向是下沉到 `lumi_contracts`，属于 P3 遗留项 |

### 8.9.2 规则表现状

| 规则 | 模式 | 基线 |
| --- | --- | --- |
| 1 `packages_no_app` | **阻塞** | 0 |
| 2 `domain_isolation` | **阻塞**（本轮从报告转） | **0** |
| 3 `agents_no_domain_impl` | **阻塞**（本轮从报告转） | 1 |
| 4 `no_new_shim` | **阻塞** | 15 个白名单壳 |
| 5 `package_layering` | 报告 | 1 |
| 6 `core_no_domain` | 报告 | 0 |
| 7 `pure_package_no_runtime` | **阻塞** | 0 |

从这里开始：**任何新的跨域耦合、任何新的 agents→域实现依赖，CI 直接红**，
除非先把依赖登记成对方的公开面。规则 5/6 仍留报告模式（分别是内核环与 core 洁净度，
要等 P5 的 `app/platform` 落地后再收紧）。

### 8.9.3 一个连带的测试坑：patch 要打在**名字解析处**

把跨域 import 改到 `api` 模块之后，两个测试立刻红了（都是 `monkeypatch.setattr` 打错地方），
规则很简单但值得记下来：

| 生产代码的写法 | monkeypatch 该打哪 |
| --- | --- |
| 模块级 `from app.office.api import render_document` | **使用处**：`app.<consumer_module>.render_document` |
| 函数内 `from app.knowledge.api import search_user_knowledge` | **提供处**：`app.knowledge.api.search_user_knowledge`（函数执行时从 api 命名空间取名字） |

也就是说"改 import 路径"这件事**会改变测试的 patch 点**：模块级 import 把名字绑在消费者
命名空间里（打定义处无效），函数内 import 每次都从来源模块取名（打消费者处无效）。
本次两处都按这个规则修好了，并在测试里写了注释说明原因。

### 8.9.4 运维 / 前端

**前端：无需改动。运维：无镜像/依赖变化。** 新增两个 `api.py` 是 `app` 包内文件，
`Dockerfile` 与 `uv.lock` 不受影响。

## 八点十、P5 已完成：`app/core` 瘦身 → `app/platform`

方案要求"迁移前对 core 剩余文件逐个标注四类，**只有第一类进 platform**"。标注与落点：

| 分类 | 文件 | 落点 |
| --- | --- | --- |
| **纯技术设施** | `llm` `llm_config` `model_catalog` `model_plan` `model_response`；`security` `security_hardening` `crypto` `throttling` `resource_policy` `agent_security`；`deadline` `executors` `feature_flags` `resilience` `read_view_cache`；`network` | `app/platform/{model,security,runtime,network}/` |
| **业务策略**（只迁移、不合并） | `model_capability_router`(1219) `model_roles`(808) | `app/platform/model/`，**长期与 `lumi_capability` 收进同一包（另行立项）** |
| **领域服务** | 已在 P4 迁进 `app/{knowledge,workspace,memory,office}` | — |
| **API 适配** | 路由/请求响应形状 | 留在 `app/api/` |

可观测性不进 platform：`observability`(728) 与 `logging`(65) → **`app/observability/`**
（方案 §二 把 observability 列为独立的 ★P2-A 目录）。`app/monitoring/` 门面本轮**未动**，
它属于 P2-A 的剩余范围。

**`app/core` 现在只剩 7 个文件**：

```text
app/core/
├─ config.py               863 行（全部配置的唯一事实来源）
├─ database.py              30 行
├─ redis.py                 32 行
├─ exceptions.py           126 行   exception_handlers.py  83 行
├─ error_mapping.py         42 行
└─ deps.py                  50 行
```

**规模**：21 个文件迁出（core 28 → 7），全仓库 **217 处引用、120 个文件**改写，
零行为变化（纯搬家）。门禁规则 6（`app/core` 不得出现 domain 词）保持 **0**。

### 8.10.1 方案点名"先标注再定去向"的两个文件（本轮**不搬**）

| 文件 | 四分类结论 | 为什么现在不定去向 |
| --- | --- | --- |
| `app/services/runtime_policy.py`（816 行） | **运行时设施 + 业务策略混合**：Redis 键空间 + epoch 轮询是纯技术设施；里面装的"降级/熔断阈值"是业务策略 | 它和方案 §五的"降级熔断与运行时干预"项目绑在一起，跟着那个项目一起定归属才不会返工 |
| `app/services/tool_output_projection.py`（66 行） | **领域服务**：面向模型的工具结果投影（摘要/截断），与工具输出管线同族 | 不属于横切技术设施，进 platform 会让 platform 认识"工具/模型输出"这类业务概念；随工具输出管线一起处理 |

### 8.10.2 运维 / 前端

**前端：无需改动。运维：无镜像/依赖变化**（`app/platform`、`app/observability` 都在 `app` 包内，
`Dockerfile`/`uv.lock` 不受影响）。部署脚本若硬编码了 `app/core/llm.py` 这类路径需要跟着改——
仓库内所有路径引用已随改写更新（含 `.md`/`.yml`/`.toml`）。

## 八点十一、P6 已完成：拆 `models/db_models`

817 行 / 29 个 ORM 类 → 按域拆成 9 个模块，**聚合入口保留**：

```text
app/models/
├─ db_base.py             Base / UUIDMixin（不变）
├─ db_models.py           **聚合兼容入口**：re-export app.models.db.* 的 29 个类
└─ db/
    ├─ __init__.py        刻意 import 全部兄弟模块（见下）
    ├─ user.py            User / RefreshToken / UserPrompt / UserPreference / UserPreset
    ├─ conversation.py    Conversation / Message / ConversationMemoryState / ConversationSegment / Attachment
    ├─ knowledge.py       KnowledgeSpace / Document / DocumentChunk / Project / ProjectIndex / CodeEmbedding
    ├─ memory.py          Memory / MemoryProfile
    ├─ office.py          OfficeSession / OfficeTaskIndex
    ├─ job.py             EffectJournal / JobRun / JobStep
    ├─ plugin.py          UserMcpToolBinding / SkillTelemetryDaily / UserWorkflowSkill
    ├─ audit.py           ControlLog
    └─ usage.py           LLMUsage / DailyTokenStat
```

（方案给的分组是 `{user,conversation,knowledge,memory,office,job,plugin}`；实测还差三张表
不属于这七类——操控日志、LLM 用量原始/聚合——所以补了 `audit` 与 `usage` 两个模块，
而不是把它们硬塞进不相干的分组。）

### 8.11.1 这一拆**没有改任何调用点**

因为聚合入口保留了全部 29 个名字：`Alembic` 的 `from app.models import db_models`、
以及仓库里 50 处 `app.models.db_models` 引用**一个字都不用改**。
这是"先建聚合入口、再拆实现"的收益——P6 的改动量因此全在新增结构上，风险面最小。

### 8.11.2 证据：拆分前后 **DDL 逐列一致**

不是"看着一样"，是跑出来的：把拆分前的文件当作独立模块加载、与拆分后的定义分别 dump
`{表: [列名, 类型, 可空, 服务端默认, 外键]}` 再对比——

```
tables old/new: 29 29 | missing: [] | extra: [] | identical tables: 29 / 29
```

### 8.11.3 两个真问题（都踩到了）

1. **跨模块的解析式注解**：SQLAlchemy 的关系目标写在注解字符串里（形状是
   `Mapped[list[类名]]`），拆开之后 ruff 的 F821 看不到别的模块里的类。
   标准修法是加 `if TYPE_CHECKING:` import（运行期不执行，不引入循环）。
2. **扫描器要看 AST 位置，不能扫原始文本**：我第一版按"文件里出现的类名字符串"来补
   TYPE_CHECKING，结果把 `order_by=类名.字段` 这种**运行期表达式**也算进去（补出 F401 噪音）；
   修成"只扫 `Mapped[...]` 里的引号名"之后，**模块 docstring 里那句示例又变成了假阳性**——
   最后把 docstring 里的示例改成不带具体类名的形状。
   教训与门禁那节一致：**任何做文本分析的工具都要按语法位置判断，别按整文件文本匹配**。

### 8.11.4 registry 完整性：包入口 import 全部兄弟模块

关系目标按类名从 registry 解析，所以"只 import 一个子模块"的入口会遇到解析不出来的 mapper。
`app/models/db/__init__.py` 因此**刻意 import 全部兄弟模块**；实测在一个干净进程里
只 `from app.models.db.memory import Memory` 后 `configure_mappers()` 也正常。

### 8.11.5 验收

`tests/structure/test_structure_p6_models.py`（14 例）：聚合入口**双向**覆盖校验（registry 里的每个
映射类都必须导出；导出的每个名字都必须是映射类）、每个域模块的表归属被钉住、
兄弟模块确实被加载、旧路径可用、Alembic 入口能看到 29 张表。

**运维 / 前端**：Alembic 的 `target_metadata` 与迁移脚本都没动，生成新迁移的方式不变；
无新依赖、镜像不变。**前端无需改动。**

## 八点十二、P7 第一半：兼容壳**全部删除**，白名单归零

`tools/compat_shims.txt` 从 15 条存量壳变成**空文件**（只留纪律说明）。顺序是先把调用点
改到真实来源、再删文件——反了会留下 import 失败：

| 原壳 | 真实来源 | 调用点情况 |
| --- | --- | --- |
| `orchestration/adapters/{execution_mode,execution_policy,job_run_view,step_resume,step_sequence}.py` | `lumi_orch.*` / `lumi_execution.*` | **零生产引用**（P1 就确认过），只有测试断言它们存在 |
| `app/contracts/plugins/`（8 个转发壳 + 包入口） | `lumi_contracts.plugins.*` | 仅被"应用侧入口"那条测试引用 |
| `app/services/model_output_protocol.py` | `lumi_orch.protocol` | 3 个测试 |
| `app/services/rag/__init__.py` | `app.knowledge` | 无生产引用 |

**白名单清零的意义**：规则 4 是阻塞规则。名单一空，任何**新增**转发壳立刻让 CI 变红；
文档里写明了处置口径——想"把旧路径再卖一遍"时，先回答"调用点为什么不能直接改"。

### 8.12.1 一个真坑：删包时忘了 `__pycache__`，它变成**命名空间包**

删完 `app/contracts/plugins/*.py` 之后 `import app.contracts.plugins` **居然成功**：
目录里还留着 `__pycache__`，PEP 420 把"只剩字节码缓存的空目录"当成命名空间包
（`__file__` 是 `None`、属性全无）。测试因此报 `DID NOT RAISE ModuleNotFoundError`。
**删包必须连目录一起删**，不能只删 `.py`。

### 8.12.2 测试侧的三处同步

* `tests/capabilities/test_plugin_contracts.py`：原来断言"应用侧入口与契约层同一对象"，现在改成
  "插件契约**只有一个家**"——旧路径 `ModuleNotFoundError` + 真实来源的 `__module__`
  指向 `lumi_contracts`；
* `tests/structure/test_structure_p1_layout.py`：原来断言 5 个壳"覆盖内核公开面"，现在断言它们
  **已经不存在**（并解释了为什么：零引用就不该留着，否则下一个读代码的人会以为
  还有一层适配逻辑要维护）；
* `tests/structure/test_architecture_gate.py`：白名单相关断言改成"**必须为空**"，并断言规则 4 的
  豁免数为 0。

## 八点十三、P7 第二半：测试目录**按域镜像**（含一次真 bug 的定位）

平铺的 200+ 个测试模块按"被测代码所在包"下沉到子目录，与 `app/` 的域边界一一对应：

| 测试目录 | 文件数（含 `__init__.py`） | 对应被测包 |
| --- | --- | --- |
| `tests/agents/` | 24 | `app/agents/*`（含 skills / roles / mcp / langchain） |
| `tests/capabilities/` | 44 | `app/agents/capabilities/**` |
| `tests/orchestration/` | 67 | `app/agents/orchestration/**` + `packages/orchestration` |
| `tests/contracts/` | 23 | `app/contracts/**` + `packages/contracts` |
| `tests/platform/` | 17 | `app/platform/**` |
| `tests/knowledge/` `workspace/` `memory/` `office/` | 5 / 19 / 6 / 5 | 四个业务域的 `app/<域>/**` |
| `tests/api/` `tests/services/` | 6 / 5 | `app/api/**`、`app/services/**` |
| `tests/structure/` | 10 | 结构门禁自身的回归（P0–P7 各阶段各一份） |

`tests/acceptance/`、`tests/fixtures/` 原地不动（前者是跨域端到端验收，不属于任何单一域）。
每个目录一个 `__init__.py`，因此跨测试引用可以写成 `tests.<域>.<模块>` 的确定形状。

### 8.13.1 深度变化：路径基准统一收到 `tests/_paths.py`

分域后"回退到仓库根"的层数不再固定（原来 `parent.parent`、现在 `parents[2]`），
三十余处这类写法一旦漏改就表现为"fixture 文件找不到"，与本次改动毫无关系的报错。
现在统一从 `tests/_paths.py` 取 `REPO_ROOT`（**锚在 `pyproject.toml` 上，不数层数**）：

```python
from _paths import REPO_ROOT          # tests/ 已在 pythonpath 里，裸名导入在任何深度都成立
fixture = REPO_ROOT / "tests" / "fixtures" / "x.json"
```

17 个测试文件（含 `tests/acceptance/` 的共享 helper）已改用该 helper。跨测试 import 也从裸名
改成 `from tests.<域>.<模块> import ...`（漏掉的话，只有整目录一起跑才会炸，单文件跑是绿的
——这类"只在组合运行时暴露"的问题本轮踩了两次，见 8.13.2）。

### 8.13.2 镜像后才暴露的三个失败：**不是测试写错，是真 bug**

`tests/memory/test_memory_provider_acceptance.py` 的三条用例单独跑全绿、按域分开跑也全绿，
只有"agents + api + capabilities + contracts + knowledge + memory"组合跑才失败：
`task_memory` 的条目里能力/资源类型/Provider 候选**全是空值**，而此刻
`ToolRegistry.get("task_memory")` 明确返回带 `capability="resource.write"` 的实例。

定位方式（可复用）：在诊断点打印"缓存 epoch、当前 epoch、失效重建后的条目"三样。
结论是**缓存键缺了一维**：

* 条目构造会读**工具自己声明**的 `capability` / `resource_type`（这是统一资源能力层的接入面）；
* 但缓存键 `_current_epoch()` 当时只有"影子注册表 spec 摘要 + 静态映射表指纹 + 能力目录指纹"；
* 插件安装的两半是分开落地的——**ToolSpec 先登记、Skill 稍后加载**。中间那一刻建过缓存后，
  后三项逐字不变，于是"能力为空"的条目被**永久**缓存，之后发现/预检/派发全都读到旧值，
  直到进程重启。生产上表现为"新插件装好了，但系统认定它不属于任何资源"。

修复：给 `ToolRegistry` 加**内容版本**（`register` / `unregister` / `clear` 自增，只增不减），
`_current_epoch()` 把它折进缓存键（`#tools:<version>`）。这是**缓存失效**维度，
不改变任何派生算法的结果；回归测试
`tests/capabilities/test_tool_registry.py::test_global_cache_reacts_to_tools_registered_after_first_build`
用 `register → unregister → 建缓存 → register` 复现"spec 摘要不变、只有运行期声明变化"的路径
（把 `_tools_version` 打回 `unknown` 时该用例必失败，已实测）。

**前端无需改动**：条目字段、接口形状、事件契约全部不变，变的是"这些字段能及时变新"。

**一处相邻风险（本轮不改，先记下来）**：`registry_epoch()`（`app/agents/skills/mandatory_tools.py`）
有同样的盲区——它只看 ToolSpec 摘要与能力目录，不看运行期 `ToolRegistry`，而
`app/agents/skills/discovery.py` 的会话发现缓存正是按它失效的。改动它会让**更多**
缓存跟着失效（含会话级发现缓存），属于"影响面更大的行为变化"，按本方案的纪律
应当独立评估（先量命中率，再决定是否把 `ToolRegistry.version()` 折进去），
不要顺手并到本轮里。

## 八点十四、应用编排层内部拆分：P0 入口轻量化 + P1 ReAct 拆分

第一阶段的"抽包"（`lumi_orch`）方向正确，但 `app/agents/orchestration` 内部**还没有拆完**：
根目录 ~63 个模块、~1.32 万行，最大的几个文件仍是 500–900 行。
本节的纪律先说清楚：**不把应用适配层继续搬进 `lumi_orch`**（Redis/PostgreSQL/Temporal
客户端、Worker/Skill 实现、LLM 调用、办公逻辑、SSE、运行时配置、提交/恢复/审批适配
本来就属于应用层）；`models.py` 不动（引用太多，要迁就走"新位置 → 兼容入口 → 统计引用 → 删除"）。

### 8.14.1 P0：编排包入口轻量化（先做，因为它是**工程风险**而不是整洁度）

**实测问题**：`app/agents/orchestration/__init__.py` 急切导入 orchestrator 单例，导致
**任何**叶子模块的导入都要付整包的代价：

| 导入目标 | 改动前 | 改动后 | 改动前拖入的重依赖 |
| --- | --- | --- | --- |
| `app.agents.orchestration` | **14.10s** | **0.36s** | orchestrator / redis / sqlalchemy / langgraph / app.repositories |
| `.models` | 13.27s | **0.30s** | 同上 |
| `.timeout_ladder` | 13.02s | **0.40s** | 同上 |
| `.plan_compiler` | 12.84s | **0.30s** | 同上 |
| `.effects` | 12.54s | 0.83s | 它自己确实需要 redis/sqlalchemy（不再是"被父包连坐"） |

它同时是循环的最后一环：`app.repositories → orchestration.models → orchestration.__init__
→ orchestrator → app.repositories`。

**改法**：入口只急切导入公共模型（`models` 是纯 data：`time` / `enum` / `lumi_orch` / `pydantic`），
`AgentOrchestrator` / `orchestrator` 改用 PEP 562 的模块级 `__getattr__` 按需加载。
**不缓存回包属性**——缓存会让 `import app.agents.orchestration.orchestrator as m` 拿到实例
而不是模块（修一个歧义的同时造出另一个）。

**调用点纪律**：包级重符号写法与同名子模块共享名字，语义取决于导入顺序，
因此仓库内 5 处 `from app.agents.orchestration import orchestrator` 全部改成
`from app.agents.orchestration.orchestrator import orchestrator`（真实来源）。

**守卫**：`tests/structure/test_orchestration_entry_light.py`（5 例）——
入口不得 eager import 重依赖（AST 静态断言）、懒加载对象必须与真实来源同一对象、
仓库不得再用包级歧义写法（AST 扫描）、循环两半都不成立、
`models.py` 只允许标准库/内核/pydantic。

### 8.14.2 P1：`react_runner.py`（949 行）→ `react/` 子包

| 模块 | 行数 | 职责 |
| --- | --- | --- |
| `react_runner.py`（**门面**） | 24 | 同包聚合再导出（既有调用点不动） |
| `react/state.py` | 39 | `ReactState` / `ReactRunResult`（纯数据） |
| `react/tool_selection.py` | 142 | 工具发现、名字判定（实现名）、L2 检索、领域申请 |
| `react/workspace_window.py` | 162 | 工作区阶段工具窗口（画像优先，关键词兜底） |
| `react/tool_execution.py` | 119 | 执行护栏（前置读取）、重复调用熔断、失败排除、结果记账 |
| `react/progress.py` | 44 | 进度事件与结果投影 |
| `react/prompt.py` | 56 | 系统提示词组装（信息边界与普通办公路径一致） |
| `react/graph.py` | 59 | 状态机装配（节点/边/路由条件） |
| `react/runner.py` | 627 | 运行流程：加载发现 → 模型 → 节点 → 编译 → 跑一轮 → 收口 |

**为什么可以用门面**：规则 4（`no_new_shim`）只把**跨包** re-export 判为兼容壳；
`react_runner.py` 聚合的是**同包**子模块，属于正常入口，因此既有调用点
（`app/agents/roles/react.py`、4 个测试文件）不必跟着搬家。

**monkeypatch 规则照旧**：模块级 import → 打**消费者**。因此
`tests/orchestration/test_react_runner.py` 等 24 处补丁目标从
`…orchestration.react_runner.get_chat_model` 改成
`…orchestration.react.runner.get_chat_model`（`make_skill_tool` /
`get_office_react_capabilities_with_trace` 同理）。

**反向断言跟着搬家**：`tests/memory/test_memory_provider_acceptance.py` 的
"不得出现记忆专属分支"扫描名单补上了 `react/` 各模块——代码一搬家，保护不能名存实亡。

**下一步（已记录，未做）**：`run()` 里的 `agent` / `finish` 两个节点仍是闭包（需要 `model`），
把它们连同一个显式的运行上下文搬到 `react/nodes.py` 是同一批拆分的第二步；
其余节点已改为 mixin 方法（`before_tool_node` / `after_tool_node` / `execute_tool_node`）。
另外，每轮的**候选窗流水线**（领域收窄 → 探索原语 → L2 缓存合并 → 多文档盘点前提）
已抽到 `react/tool_window_pipeline.py`（四个可单独测试的函数）。

### 8.14.3 P2：`recovery/` 收拢 + `step/` 拆分

**`recovery/`（7 个模块，纯搬家，1670 行）**：任务恢复、副作用恢复（Effect Journal）、
失败任务恢复、失败任务重规划、逻辑计划续跑、重规划证据、恢复策略，收进
`app/agents/orchestration/recovery/`。为什么单独成包：这些入口只在"出了事"时被调用，
却和正常执行共享 Job/Step 状态；混在根目录里读代码的人分不清"这条路径会不会被正常流程
走到"，熔断与幂等就难以收紧。搬动只改了 ~10 处 import（含 2 处子模块形式
`from … import effect_journal_recovery`），**无兼容壳**。

**`step/`（`step_run_service.py` 723 → 437 行）**：

| 模块 | 内容 |
| --- | --- |
| `step/state_adapter.py` | Job/routing ↔ `StepRunState`、依赖判定、当前步骤与副作用类型（判不出返回空，不猜） |
| `step/presentation.py` | 实时帧与刷新投影**同源**的 title/summary（`step_action`/`completed_text`/`failed_text`…） |
| `step/persistence.py` | 结果引用与检查点落盘（**完成事件必须在检查点之后**；写失败只降级不阻塞） |

`StepRunService` 本体与 SSE 事件常量留在 `step_run_service.py`（对外入口不变，
测试与 `orchestrator` 的 import 一行都不用改）。**不搬去 `lumi_execution`**：
它仍然带着应用层的 Job、事件与持久化依赖。

### 8.14.4 P4：冻结根目录规则（**棘轮**，不是一次拆完）

停止线写在 `tests/structure/test_orchestration_root_discipline.py`：

* 根目录模块**名单只减不增**（新增能力进 `planning/` `execution/` `runtime/` `preflight/` `submission/` 等子包）；
* 根目录**总行数只降不升**（12368 → 11216 → **1407**；模块 56 → 55 → **5**）；
* 单文件 > 400 行必须登记为"待拆对象"并写明理由（名单只减不增）；
* 每个根模块必须有 docstring（"职责能否一句话说清"是可执行的门槛）；
* 子包存在且根目录旧文件已删（防止"搬了但没删"）。

**验收看四件事**（不是"还剩多少文件"）：① `lumi_orch` 不依赖 app（门禁规则 1）；
② 基础设施没反向进内核（规则 7）；③ 根目录不再新增大型业务模块（本棘轮）；
④ 单文件职责与变更原因说得清（docstring 门槛）。

### 8.14.5 P1 收尾：ReAct 节点、Task 提交包、orchestrator 的恢复协调

**ReAct 节点**（`react/agent_node.py`，335 行）：`run()` 里的 `agent` / `finish` 闭包与
`execute_tool_node` 一起抽成 `AgentNodeMixin`。做法不是引入运行上下文对象，而是
**把一次运行的输入写成实例字段**（`_model` / `_cache_model` / `_cache_base` /
`_native_tools_supported` / `_instruction` / `_internal_docs`），因此没有闭包、没有
`partial`，行为逐字不变。三个节点必须留在**同一个模块**：它们共用同一批模块级依赖
（`make_skill_tool` / `get_office_react_capabilities_with_trace` …），拆到两个模块会让
monkeypatch 目标分裂——测试打一个、代码用另一个，会以极难排查的方式失效（本轮真踩到：
工具执行节点留在 runner、测试改了 agent_node 的补丁目标，表现成"工具从未被执行"）。
`react/runner.py` 因此收缩到 234 行，只留"加载 → 建模型 → 装配 → 跑一轮 → 收口"。

**Task 提交包**（`submission/`）：`job_submission_service.py`（773 行）整体移入
`submission/service.py`（同包聚合再导出 `JobSubmissionService`），根目录少一个 700+ 行模块；
提交链路的其余环节本来就在 `submission_context_service` / `submission_guard` /
`office_plan_selection_service` / `job_materialization_service` 里，包入口把它们的关系
写清楚。调用点只有 3 处（orchestrator + 两个测试），已全部改到真实路径。

**orchestrator 的恢复协调**（`recovery/coordination.py`）：`_has_terminal_model_failure` /
`_continue_logical_plan` / `_maybe_replan_logical_plan` / `_maybe_replan_failed_job` /
`_handle_task_escalation` / `_finalize_step_failed` 抽成 `RecoveryCoordinationMixin`
（orchestrator 855 → 762 行）。这正是 P2 说的"恢复与重规划集中管理"：这些方法只在
"出了事"时被调用，集中后熔断、幂等与审计有了单一落点。

**根目录棘轮随之下调**：模块 56 → 55、总行数 12368 → **11216**（预算已收紧锁定）。

### 8.14.6 P5：根目录收敛——55 模块 / 11216 行 → **5 模块 / 1407 行**

按"能拆的拆、能迁的迁"把根目录剩下的 46 个模块按职责迁进子包（**纯搬家 + 全仓引用改写**，
无行为变化）：

| 新/既有子包 | 收进来的职责 |
| --- | --- |
| `admission/`（新） | 准入判定、准入租约心跳、执行预算、通道并发限额 |
| `planning/` | 规划器、计划编译器、办公预规划策略与计划选择、逻辑计划与续跑、任务画像与形状、复杂度（TCA）、路由与置信度校准、成功案例库 |
| `preflight/`（新） | 任务级入口检查 + 能力级预检（含对外冻结状态映射） |
| `runtime/`（新） | 运行时网关、超时阶梯、Temporal 准入策略、Job 状态存储、副作用日志适配、Job↔内核规格适配、内核结果契约校验 |
| `execution/` | 执行循环、控制面、Job 终态/生命周期/协调、查询、分叉、质检、安全、节点上下文、展示、审批与升级、Job 物化、Worker 门面 |
| `office/`（新） | 办公专用辅助：读取目标判定、文档授权与产物交付、记忆边界 |
| `submission/` | 提交上下文与幂等/准入保护器（`service.py` 已在上一批迁入） |

**根目录最终只剩 5 个模块**：`__init__.py`（懒加载入口）、`models.py`（公共模型，
按方案**不动**）、`orchestrator.py`（门面 + 跨模块协调）、`react_runner.py`（ReAct 同包门面）、
`step_run_service.py`（执行流程门面）。棘轮随之收紧：`_ROOT_MODULES_ALLOWED` 只剩这 5 个，
`_ROOT_LINE_BUDGET` 11216 → **1407**，超限名单只剩 `orchestrator` 与 `step_run_service`。

**搬家的三种引用形状都要改**（本轮一次踩全，脚本已固化）：

1. 点号路径 `app.agents.orchestration.<mod>`（含 monkeypatch 字符串、`__import__`）；
2. 包内子模块写法 `from app.agents.orchestration import <mod>`——**不能**机械替换成
   `import <pkg>.<mod>`（那是语法错误），要变成 `from app.agents.orchestration.<pkg> import <mod>`；
   带括号的多名导入与 `as` 别名要逐行拆开；
3. 源码路径字符串（守卫测试直接读文件）：`app/agents/orchestration/<mod>.py`
   与 `REPO_ROOT / "agents" / "orchestration" / "<mod>.py"` 两种写法。



### 8.14.7 P3：Temporal Activity 按运行族细分（**已完成**）

两个模块改成**同名包**——这是关键手法：`temporal/activities.py` → `temporal/activities/`、
`temporal/logical_read_activities.py` → `temporal/logical_read_activities/`，包入口只做
**聚合再导出**，于是 Worker 注册、Workflow 的字符串调用、既有 import 路径**一行都不用改**
（Activity 以名字调用，改路径的风险最大）。

| 原文件 | 拆分后 | 最大模块 |
| --- | --- | --- |
| `activities.py`(610) | `static_dag.py`(349) / `replan.py`(147) / `synthesis.py`(100) / `persistence.py`(13) / `lifecycle.py`(12) | 349 |
| `logical_read_activities.py`(716) | `frontier_effects.py`(217) / `replan.py`(199) / `frontier_read.py`(173) / `helpers.py`(75) / `lifecycle.py`(54) / `__init__.py` / `approval.py`(27) / `_shared.py`(21) | 217 |

**先补断言再动手**（用户要求的前置条件）：`tests/orchestration/test_temporal_activity_registry.py`
把**三份清单钉在一起**——实现（扫 AST 找 `@activity.defn`）、注册（`worker.py` 的
`activities=[…]`）、调用（Workflow 里的 `execute_activity("名字", …)` 字面量），
外加一条"冻结的活动名清单"。少注册一个 Activity 的表现是"工作流在某一步静默挂住"，
这条断言把它变成 CI 里的红灯。

**monkeypatch 目标按既有规则迁移**：测试原先 `monkeypatch.setattr(logical_read_activities,
"RedisStateStore", …)`（模块级 import → 打**消费者**）。拆分后消费者分别是
`…logical_read_activities.approval` / `.lifecycle` / `.frontier_read`，4 处补丁目标已按此更新。

### 8.14.8 代码头只写"现在是什么"，迁移历史留在文档

拆分过程中给模块头加过"从 X 拆出（结构重构 Pn）"这类叙述；迁移收口后它们就是噪音——
读代码的人需要的是"这个模块负责什么"，而不是它曾经在哪。已清理 **50 个文件**：

* 删除"从 `X` 拆出/收拢（结构重构 Pn）"整句，保留职责、边界与不变量说明；
* 删除 `（结构重构 Pn）` 之类括号标记，`## 四分类与拆分结果（结构重构 P2）` → `## 四分类与模块划分`；
* `app/plugins/__init__.py` 的"原路径为 app.services.plugins，已整体迁到…"改成"本包是唯一位置"。

**规则**：`app/**` 的模块头只描述**当前职责与约束**；重构历程、阶段编号、缺口编号留在
`docs/`（本文档 §八点十四、`docs/CAPABILITY_TWO_GENERATIONS.md`）。

### 8.14.9 一个工具链教训：不要用 PowerShell `Set-Content` 改源码

批量替换 import 时用 `Set-Content -Encoding UTF8` 写回了 14 个文件，**它带 BOM**。
Python 从文件加载能容忍 BOM，ruff 与既有 AST 门禁也没报，于是它悄悄留在源码里；
直到新增的"根目录模块必须有 docstring"用例用 `ast.parse(源码文本)` 才把它抓出来
（`invalid non-printable character U+FEFF`）。已全部清理，并顺手修掉一个**既有**的
BOM（`app/platform/security/security.py`）。**结论**：源码编辑一律用编辑器工具或
Python 写入（`encoding="utf-8"`），批处理只用于"读取 + 生成补丁"。

### 8.14.10 `app/platform` 与标准库同名：入口必须自我修正 `sys.path`

基础设施层从 `app/core` 搬到 `app/platform` 之后，出现一个只在**脚本方式启动**时暴露的问题：

```text
python app/main.py            # sys.path[0] = app/
  → import sqlalchemy → import platform → 命中 app/platform/（同名包）
  → AttributeError: module 'platform' has no attribute 'python_implementation'
```

`uvicorn app.main:app` 不受影响（模块方式下 `sys.path[0]` 是仓库根），Docker 用的也是这种方式；
但 IDE / `uv run python app/main.py` 是合理用法，必须能用。

**做法**（`app/main.py`）：入口在**任何第三方 import 之前**调用 `ensure_import_root()`——
把 `app/` 目录从 `sys.path` 摘掉、把仓库根插到最前（幂等；模块方式下是空操作）。
顺带把入口 docstring 的分层清单更新到当前目录（`platform/`、`observability/`、四个业务域…）。

**守卫**：`tests/structure/test_entry_import_root.py`（6 例）——

1. 守卫调用必须排在**第一个第三方/app import** 之前（AST 顺序断言）；
2. `ensure_import_root()` 真的摘掉 `app/`、把根放最前，且**幂等**；
3. `sys.path` 里没有 `app/` 时是空操作 + 补根；
4. `app/` 顶层与标准库重名的名字**只有 `platform`**（新增重名必须在白名单里写理由；
   `app/types.py` 这类最危险）；
5. 进程里的 `platform` 必须是标准库（`hasattr(platform, "python_implementation")`），
   而 `app.platform` 仍是正常子包。

**规则**：`app/` 下新增顶层模块前先查一次 `sys.stdlib_module_names`；一旦重名，
要么换名，要么像 `platform` 一样在守卫测试里登记，并保证入口能自我修正。

### 8.14.11 导入冒烟门禁：`tools/check_imports.py`（CI 阻塞）

拆分/搬家的**真实盲区**是"没人测到的模块"：测试只 import 它们用到的模块，而只被 Celery 任务、
Temporal Worker 或某个懒加载分支引用的模块如果路径漏改，测试全绿、线上第一次跑到才炸。
`tools/check_imports.py` 把整棵导入图跑一遍——`app/` + `celery_app/` + `plugins/` 的每个模块
逐个 import，失败即非零退出；CI 里作为独立 job（`import-smoke`，阻塞）与其它门禁并列。

* 本地：`python tools/check_imports.py`（`--list` 只看清单，`--verbose` 打完整栈）；
* **刻意放在 `tools/` 而不是 `tests/`**：它会 import 模块，放进 pytest 进程会污染
  `sys.modules`，让"依赖尚未导入"的用例（例如入口轻量化守卫）变得顺序敏感。

本轮实测：**520 个模块，0 失败**——同时验证了 17 个子包、46 个模块的迁移路径全部正确。

### 8.14.12 现场排障：`python app/main.py` 崩溃、401 风暴、发消息 500（三个真实事故）

结构性重构的**回归证据**不是测试全绿，而是现场三条链路能跑。这一节记录联调阶段真实
踩到的三个事故与修法，供以后同类问题直接对照。

**（1）`python app/main.py` 崩溃：`module 'platform' has no attribute 'python_implementation'`。**
脚本方式启动时 `sys.path[0]` 是 `app/`，于是 `app/platform` 被当成顶层 `platform` 遮蔽标准库
（`uvicorn -m` 从仓库根启动则不会）→ 修法见 §8.14.10 的 `ensure_import_root()`。

**（2）"各种接口 401"：`.env` 重复键导致 JWT 密钥被静默轮换。** `.env` 里 `JWT_SECRET_KEY`
写了两遍（旧 48 字符 + 新 64 字符），dotenv/pydantic 后者胜 → 服务端用新键验签旧 token →
`InvalidSignatureError` → 全部接口 401（含 `/auth/refresh`，客户端无法自愈）。修法：
去重 `.env`（有效值不变）＋启动时指纹（`JWT 密钥指纹=<sha8>`，轮换会打日志）＋
`duplicate_env_keys()` 守卫（`tests/platform/test_auth_secret_diagnostics.py`）。
**结论：401 全线飘红先查密钥指纹，不要先怀疑重构。**

**（3）"发消息返回错误的结果"：缺模型凭据被兜底成 500。** BYOK 用户（`byok=True`，
密钥只随 `x-llm-api-key` 逐请求携带）漏带密钥时：

* 旧行为：`ChatOpenAI(api_key="")` 抛 `OpenAIError: Missing credentials` → 全局兜底
  **500 服务器内部错误**；SSE 通道更隐蔽——流已经开始，错误只能走帧，而
  `classify_model_error` 不认这个异常 → `{"type":"error","status":500,
  "code":"CHAT_STREAM_INTERNAL_ERROR"}`，前端只能显示"服务器内部错误"。

修法分三层，缺一层就还会漏：

1. **守住调用点**：`app/platform/model/llm.py::_model` 在创建客户端前判"有没有密钥"，
   抛 `ModelCredentialsMissingError`（**400**，`data.error_code=MODEL_API_KEY_MISSING`、
   `data.byok`、`data.base_url`）；本地/内网端点（`localhost` / `127.0.0.1` / `::1` /
   内网 IP / `*.local`）允许空密钥，自建 Ollama / vLLM 不受影响。
2. **分类器认结构化码**：`classify_model_error` 先看 `error_code` 再退化到文本；"缺密钥"
   一律 `MODEL_API_KEY_MISSING`（**不是** `MODEL_AUTH_ERROR`/401——401 会被前端当成登录
   过期而触发重新登录）；`_MODEL_ACTION_REQUIRED` 收录该码 ⇒ 办公任务不再重规划重试。
   `packages/execution` 的同类字面量集合（内核包不能 import app）同步收录。
3. **出口一致**：`app/api/v1/chat.py::stream_error_frame` 成为流内 `error` 帧的**唯一**构造点
   （模型码 → 402/401/400/404/422/503，未知 → 500），与 HTTP 路径共用同一套 code/文案；
   统一错误模型登记别名 `MODEL_API_KEY_MISSING → model.credentials_missing`
   （business / 不可重试 / "去填 API Key"），否则投影会落回 `model.provider_offline`
   （transient+retryable）显示成"模型服务暂时不可用，正在重试"。

**排障教训**：现场 500 一度与进程内 TestClient 的 400 矛盾——原因是**8000 端口上还挂着
改动前启动的旧进程**，新起的服务绑定失败（`WinError 10048`）静默退出，探针打到旧代码上。
换代码后复验前先 `Get-NetTCPConnection -LocalPort 8000`，确认监听者的启动时间。

**（4）"管理员页面什么都不显示"：客户端掉进游客态。** 桌面端启动时用**持久化的
refresh_token** 续期，服务端回 401 后客户端打印"游客模式启动，保留本地会话记录"，
随后 `/conversations`、`/user/models`、`/user/llm-config` 全部 401 —— 用户看到的就是
"几乎所有信息都不显示"。真实日志（`%APPDATA%\lumi-desktop\logs\lumi_2026-09-14.log`）：

```
[ERROR] [API] POST /auth/refresh → 401: 刷新令牌无效
[INFO] app "游客模式启动，保留本地会话记录"
[ERROR] [API] GET /conversations?scene=all&limit=20&offset=0 → 401: 请先登录
```

两个真实缺口（都已修）：

1. **轮换对竞态不宽容**：refresh_token 轮换是"用一次废一个"，而客户端会把 token 落盘
   复用（双开窗口、重复启动、持久化的是上一次写盘的旧副本）。抢输的那次必然 401 →
   整个客户端降级成游客态。现在轮换后 **120 秒宽限期**内重放旧 token
   （`app/api/v1/auth.py::_REFRESH_GRACE_SECONDS` + Redis `auth:refresh_grace:{hash}`）
   **照常签发新令牌对**（幂等重放，并记 warning）；宽限期不延长会话寿命，新令牌仍是
   完整 30 天。**没有加宽限期之前，"同一个 token 用两次"就等于把客户端打成未登录。**
2. **401 只有文案、没有码**：客户端无法区分"access 过期（去刷新）"与"refresh 无效
   （去重登）"，只能字符串匹配，很容易静默降级。现在 401 一律带机器可读字段：
   `require_auth` → `{"error_code": "LOGIN_REQUIRED", "action": "refresh_or_relogin"}`；
   `POST /auth/refresh` → `REFRESH_TOKEN_INVALID` / `REFRESH_TOKEN_EXPIRED` +
   `{"action": "relogin"}`（**注意 `AppException.error_code` 只进日志，前端能读到的是
   `data.error_code`**，两处都要写）。

排查口径：前端"页面全空"先看客户端日志里有没有 `POST /auth/refresh → 401`；有，就是
游客态，**先让客户端重新登录**，再去查服务端。服务端侧自检：用真实账号跑一遍
`/auth/captcha → /auth/login → /auth/refresh`，以及 `GET /api/v1/admin/system/overview`
（本次实测：登录 200、刷新 200、旧 token 重放 200、管理端 19 个 GET 全 200）。

回归守卫：`tests/api/test_auth_refresh_grace.py`（7 例：轮换/宽限期重放/过期与未知 token
的错误码/`require_auth` 的 `data.error_code` 出口）。

**（5）"发消息失败"的真凶：懒加载导入没跟上搬家。** 用户侧日志只有一行：

```
2026-09-14 21:18:26 | WARNING | app.api.v1.chat:event_gen:278 - 流式聊天失败:
    cannot import name 'ensure_rag_index' from 'app.office.api'
```

`ensure_rag_index` 早就在 `app/office/docs.py`（公开面 `app.office.api` 只 re-export 三个
写入接口），可 `app/office/context.py` 的**函数体内**还写着旧路径。这类错误的可怕之处在于
**所有现有门禁都是绿的**：

* `tools/check_imports.py` 逐个 import 模块 → 模块级导入完好，`context.py` 本身能 import；
* 全量 pytest → 没有用例执行到那一行；
* 只有用户发一条带办公文档的消息（走 `load_office_context`）才会炸，而且被
  `event_gen` 的兜底 except 吞成一句"流式聊天失败"。

修法两步：

1. **修导入**：`context.py` 改成从符号现在的归属模块导入
   （`from app.office.docs import ensure_rag_index, ensure_session, extract_full_text`）；
   同批还修了两处同类问题（都是新门禁扫出来的）：
   * `app/agents/core/patch.py`：`tree_sitter_typescript` 只导出 `language_typescript` /
     `language_tsx`，**没有** `language`——写错名字被 `except Exception` 吞掉，
     TypeScript 抽取静默退化成正则兜底（"看起来能用，精度不同"）；
   * `plugins/workflows/developer/create_task_plan.py`：`_build_planner_prompt` /
     `_extract_json` 在规划器拆分后就没了（现名 `build_planner_prompt` /
     `_parse_json_object_text`），插件把 ImportError 吞成"任务规划失败: …"。
2. **加门禁 `tools/check_import_symbols.py`（AST，阻塞）**：读全仓 AST，对**每一条**
   `from X import a, b`（含函数体内、含 `try/except ImportError` 兜底分支）验证
   `a`/`b` 能在 `X` 里解析。首轮实测：520 个文件、5352 条 from-import、**3 条失败**
   ——就是上面这三处。确实需要写"旧版本兜底导入"时，那一行加
   `# import-symbols: allow-missing` 显式豁免（`patch.py` 用了一处）。
   自身回归：`tests/structure/test_import_symbol_gate.py`（7 例，
   包含"坏导入藏在函数体里"与"藏在 except 分支里"两种事故形状）。

**这两道门禁是互补的**：`check_imports.py` 管"模块路径还在不在"，
`check_import_symbols.py` 管"符号还在不在"；少任何一道，"搬家"都会以
"线上第一次跑到才炸"的形式暴露。

**（6）管理面的"二次管理员密码"整体移除（2026-09 裁决）。** 起因是现场体验：
点一下"测试连接"要再输一次管理员密码，多窗口/多端时那个 5 分钟令牌还经常过期。

* **删掉的东西**：`app/api/v1/admin.py`、`app/api/v1/admin_policies.py`、
  `app/api/v1/capabilities.py` 里所有 `_require_admin_verified(x_admin_token, payload)`
  调用与 `x_admin_token: ... = Depends(get_admin_verified_token)` 形参；
  `_require_admin_verified` 包装函数（3 处）与 `app/core/deps.py::get_admin_verified_token`
  一并删除；`verify_admin_verified_token` 不再被授权路径使用。
* **保留的东西**：`POST /api/v1/admin/verify-password` 仍校验密码并签发 token
  （响应加 `deprecated: true`），只为旧客户端不 404；请求里带旧 `X-Admin-Token`
  会被忽略（FastAPI 未声明的 header 无副作用）。
* **授权口径**：唯一入口是 `Depends(require_superadmin)`。实测：非超管的合法 JWT
  仍然 403（`需要管理员权限` / `需要超级管理员权限`），未登录仍然 401
  `data.error_code=LOGIN_REQUIRED`——**角色门禁没有被一起删掉**。
* **前端同步**（`E:\javaidea\lumi`，随 Vite 热更新）：`LlmRolesSettings` /
  `RuntimePolicyPanel` / `SystemAdmin` 删掉密码输入框与 `verifyAdminPassword()`，
  `adminSystem.js` / `adminPolicies.js` 去掉 `X-Admin-Token` 与 token 形参；
  清单见 `docs/FRONTEND_MIGRATION_CHECKLIST.md` §4。

回归守卫：`tests/platform/test_model_role_admin_api.py::test_write_requires_only_superadmin_jwt`
（断言端点签名里没有 `x_admin_token`，写操作只用超管 JWT 即成功）、
`::test_verify_password_endpoint_is_kept_for_legacy_clients_but_deprecated`；
`tests/capabilities/test_admin_policy_api.py` 的依赖覆盖里已不再需要二次验证桩。

回归守卫：`tests/platform/test_model_credentials_guard.py`（15 例，含本地端点白名单）、
`tests/api/test_model_credentials_contract.py`（10 例，真跑 `/chat/stream` 的异常分支、
异常处理器 400 形状、`map_task_error` 保留码）、
`tests/contracts/test_unified_error_model.py::test_missing_model_key_is_not_reported_as_a_retryable_provider_outage`。

## 九、后续阶段（未开始）

| 阶段 | 内容 | 前置 |
| --- | --- | --- |
| 两代缺口 1 | **已完成**：`CapabilityBroker.select` / `invoke` 接受 `provider_ids` / `resource_type`，收窄语义与内核 `select_lease` 一致（空收窄回落）。零接口变化、默认不传 = 旧行为。详见 `docs/CAPABILITY_TWO_GENERATIONS.md` §3.1 | — |
| 两代缺口 2b/2d | **已完成**：`execution_env` 只回答工作侧（`code_provider` 由 `sandbox` 改为 `client`，词表由测试钉死）；新增 `native_action` 类别（从旧路由表派生、族名单与快照键、本机动作条目的工作侧纠正为 `client`）。详见 §3.2 | 缺口 1（已完成） |
| 两代缺口 2a | **已完成**（裁决：**改成未注册**，不补真实现）：`memory_provider` 变成 `registered=False` / `provider_id=""`，"有没有实现"的判据收敛为 `registered_providers_for()` 一处（Broker 收窄 / Adapter / 条目候选共用）；过程条目与事件不再带假 `provider_id`。**前端 B-1 文案要改成"仅声明 / 待接入"**。详见 §3.4 | — |
| 两代缺口 3 | **已完成**（批次 1–4）：`test_two_generation_parity_b1_discovery.py`（15）、`b2_broker_dispatch.py`（28）、`b3_preflight_approval.py`（26）、`b4_events_labels.py`（9）。**旧用例只迁不删**。详见 §3.5 | — |
| 两代缺口 2c | **已完成（后端兼容迁移）**：`office_document_provider` 新 id `lumi.client.office_document` + `legacy_provider_ids=("lumi.client.forwarder",)`；收窄接受新旧两个 id（旧租约照常派发）；出口新增 `provider_kind` / `provider_label` 稳定展示字段；历史步骤的旧 `provider_id` **不回写**。**桌面端无需立刻改**；前端请改用稳定展示字段。详见 §3.6 | 桌面端可选跟进 |
| P3（独立功能） | 补真 `memory_provider`（新增能力名/权限/事件/数据安全边界）作为**独立功能**推进，不混在收口任务里；当前状态是"仅声明"（缺口 2a 的裁决） | 产品立项 |
| P7 收口 | 测试镜像（§八点十三）已完成；剩余可选项：`tests/acceptance/` 是否也分域、测试文件与源文件的命名对齐（当前保持原文件名，便于 `git log --follow`） | — |
| 编排层拆分 P0 | **已完成**：包入口轻量化（14.10s → 0.36s，循环断开，入口守卫 5 例）。详见 §8.14.1 | — |
| 编排层拆分 P1 | **已完成**：① ReAct 全拆（`react_runner.py` 949 → 24 行门面 + `react/` 11 个模块，含节点 `agent_node.py` 与候选窗流水线）；② 提交链路收进 `submission/`（`job_submission_service.py` 773 行 → `submission/service.py`）；③ orchestrator 的恢复/重规划协调抽成 `recovery/coordination.py`（orchestrator 855 → 762 行）。详见 §8.14.2 / §8.14.6 | — |
| 编排层拆分 P2 | **已完成**：`recovery/` 收拢 7 个模块（1670 行，纯搬家无兼容壳）+ 恢复协调 mixin；`step_run_service.py` 723 → 437 行（`step/` 拆出状态适配 / 事件投影 / checkpoint 持久化）。详见 §8.14.3 | P1 |
| 编排层拆分 P3 | **已完成**：`temporal/activities.py`(610) 与 `temporal/logical_read_activities.py`(716) 改成**同名包**并按运行族分子模块（最大 349 / 217 行），包入口聚合再导出 ⇒ 调用点零改动；前置的"活动名清单"守卫 `test_temporal_activity_registry.py`（实现/注册/调用三份清单 + 冻结名单）。详见 §8.14.7 |
| 代码头清理 | **已完成**：50 个 `app/**` 模块头删掉"从 X 拆出（结构重构 Pn）"等迁移叙述，只留当前职责；历史留在本文档。详见 §8.14.8 |
| 入口导入根 | **已完成**：`app/main.py` 在第三方 import 前调用 `ensure_import_root()`（摘掉 `app/`、补仓库根），修复"脚本方式启动时 `app/platform` 遮蔽标准库 `platform`"；守卫 6 例（含"`app/` 顶层不得新增标准库同名名字"）。详见 §8.14.10 |
| 导入冒烟 | **已完成**：`tools/check_imports.py`（520 模块逐个 import，0 失败）+ CI 阻塞 job `import-smoke`。详见 §8.14.11 |
| 编排层拆分 P4 | **已完成（棘轮冻结）**：根目录名单只减不增、总行数只降不升（**1407**，模块 **5**）、>400 行必须登记为待拆对象、每模块必须有 docstring。详见 §8.14.4 |
| 编排层拆分 P5 | **已完成**：根目录 55 模块 / 11216 行 → **5 模块 / 1407 行**（46 个模块按职责迁入 admission / planning / preflight / runtime / execution / office / submission）。详见 §8.14.6 | — |
| 前端清单 | 本轮所有前端可见差异收在一处：`docs/FRONTEND_MIGRATION_CHECKLIST.md`（必须改 / 可以不改 / 可选跟进） | — |
| 现场事故修复 | **已完成**：入口 `sys.path` 遮蔽（§8.14.10）、`.env` 重复键导致的 401 风暴（密钥指纹 + 去重守卫）、缺模型凭据从 500 改为 400 `MODEL_API_KEY_MISSING`（三层修法 + 25 例回归）、refresh_token 轮换宽限期 + 401 机器可读错误码（修"客户端掉游客态→页面全空"，+7 例）、懒加载导入失效（`ensure_rag_index` / `tree_sitter_typescript` / 开发者插件）＋ 新增导入**符号**门禁（+7 例）。详见 §8.14.12 | — |
| 管理面授权简化 | **已完成**：`/api/v1/admin/**` 的二次管理员密码（`X-Admin-Token`）整体移除，授权只剩 `require_superadmin`；`verify-password` 端点保留但废弃（`deprecated: true`）；前端三个面板与服务层同步去掉密码框/令牌头。详见 §8.14.12（6） | — |
| 独立立项 | 审计语义统一（P2-B，含 `app/monitoring/` 与 `app/observability/` 归并）、降级熔断与运行时干预（`runtime_policy` 归属）、模型选路与能力选路合并 | — |

悬置决策（先对照后裁决）：`catalog.py`(572) vs `resource_catalog.py`(565)、
`dispatch.py`(485) vs `resource_dispatch.py`(256) —— 不按新旧判定胜负，先做行为对比
（能力条目/Provider 映射/预检结果/Broker 路由/测试覆盖差异），确认等价后才指定权威实现。
**对照已完成**（`docs/CAPABILITY_TWO_GENERATIONS.md`，4 个可对拍维度逐条相等、新代是严格超集）；
裁决的前置是缺口 2/3，见上表。

`packages/skills` 暂缓：skills 同时承载工具契约/注册/发现/路由/执行/审计/LLM 循环/文件加载，
`executor.py`(2549) 与 `base.py` 是全网最高频 import 源（133+46 次），过早抽包有
`lumi_skills → app.services → app.agents → lumi_skills` 循环风险。

`app/agents/orchestration/models.py` 不移动：将来若按域拆，走
"建新位置 → 旧路径转发 → 统计引用 → 最后删除"四步，任何情况下不允许直接 rename。
