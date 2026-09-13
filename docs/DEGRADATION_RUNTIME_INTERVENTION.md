# 降级熔断与运行时干预（后端实现说明）

本文件对应方案《降级熔断与运行时干预》的 6 项。**全部默认关闭**：关掉时行为与改造前
逐字一致（沿用本仓库既有的灰度风格：开关只决定"新行为是否生效"，不改契约）。

| # | 主题 | 代码入口 | 开关 |
| --- | --- | --- | --- |
| 1 | 策略热更新 | `app/services/runtime_policy.py`、`app/api/v1/admin_policies.py` | `RUNTIME_POLICY_OVERRIDE` |
| 2 | AST 静态门禁 | `scripts/check_unsafe_calls.py`、`tests/test_unsafe_call_gate.py` | 无（CI 阻塞作业） |
| 3 | 统一绝对截止时间 | `app/core/deadline.py` | 无（未设截止时间=不限制） |
| 4 | 写操作代际校验 | `app/services/write_gate.py` | `WRITE_GATE_ENFORCEMENT` |
| 5 | 降级成本监控 | `app/core/observability.py`、`app/services/usage.py` | `METRICS_ENABLED` |
| 6 | SSE 快照真空期 | `app/services/resume_snapshot.py`、`GET /agents/jobs/{id}/resume` | 无 |

## 评审后的 P0 收口（第二轮）

第一轮实现里有几处"平时看不出来、竞态/降级时才致命"的问题，已按下表收口。

### P0-1 策略发布竞态 → 原子发布

旧顺序是"``INCR`` epoch → 写桶"，中间窗口里 **所有 Worker 都会读到空桶**——表现为
"改一条策略把线上全部覆盖清空"。现在固定为：

```
以 Redis 当前 epoch 为基准读全桶
  → 应用本次改动
  → 写新桶（hset + expire）并 hlen 校验条数
  → CAS 发布（Lua：仅当指针仍等于基准时才切换）
```

* 基准取**Redis 的当前 epoch**，不是本地缓存 epoch（用本地 epoch 会让两个 Worker 各自
  基于旧状态生成新桶并互相覆盖）；
* CAS 失败 → 抛 `PolicyPublishConflict`，管理 API 返回 **409 POLICY_PUBLISH_CONFLICT**
  （400 是"Redis 挂了"，两者客户端动作不同，不能混）；
* 不支持 `eval` 的旧客户端/替身退回 GET+SET 的**非原子**路径（单 worker 场景够用，
  多 worker 必须升级 Redis 客户端）。

本地缓存现在记 `loaded_epoch` / `loaded_at` / `last_success_at`，并把"没有可信策略"
拆成五类（`ResolvedPolicy.policy_state`，面板可见）：
`never_configured`（从未配置）/ `active` / `cache_expired`（超过 max_ttl）/
`redis_unavailable` / `explicitly_disabled`（明确 `enabled=false`）/ `policy_data_corrupt`。

### P0-2 写闸 Fail-Closed 补齐

| 场景 | 旧行为 | 现行为 |
| --- | --- | --- |
| `generation == 0`（授权时 Redis 恰好不可用） | 跳过代际校验并**放行** | `WRITE_GATE_GENERATION_UNKNOWN` 阻断 |
| Redis 代际键**过期/被删** | 当成"没变"放行 | 同上阻断（权威版本已消失） |
| 代际键值 `<= 0` | 放行 | 阻断 |
| scope 不一致（`default` vs `workspace`） | 存在"续签一个、检查另一个"的风险 | 统一常量 `WRITE_SCOPE`，执行点与面板都用它 |
| Redis 读失败 | 已阻断 | 不变（保持） |

### P0-3 Provider 停用逐个过滤

旧写法只判断"是否**所有** Provider 都被停用"——三个停掉两个，剩下那个照样被选中。
现在在**候选构造循环里逐个过滤**（`CapabilityBroker.select`）：停用即从 `candidates`
剔除，诊断用的 `registered` 仍保留，失败原因沿用既有事实词 `provider_unhealthy`
（前端映射不变）。

### P0-4 LLM 超时顺序

必须是 **模型默认 → 运行时策略覆盖 → 与剩余 deadline 取 min**。旧顺序先取 min 再套策略，
于是一个**更大的**策略值会把已经收紧的超时重新放宽（"请求只剩 3 秒"却给了模型 120 秒）。

### P0-5 时间轴统一 monotonic

* `deadline.py` 内部全部改为 `time.monotonic()`（Broker 的
  `CapabilityInvocation.deadline` 契约本就是 monotonic 秒，两套轴不能直接比较）；
  跨进程传输用 `wall_clock_deadline()` 显式换算；
* `set_deadline()` 返回 `DeadlineToken`，`reset()` **同时**还原 `deadline_var` 与
  `deadline_source_var`（只还原前者会让来源标签泄漏到后续请求）；
* Broker 的 `_timeout_for` 在"调用方没给显式预算"时用剩余预算收紧默认值（min）。

### P0-6 恢复链路：防回退 + 区分"读失败"

新增字段（`GET /jobs/{id}/resume` 与 `/events` 都有）：

| 字段 | 含义 / 客户端动作 |
| --- | --- |
| `snapshot_applicable` | **只有 true 才允许用 `snapshot` 覆盖本地视图**；false = 快照落后于你的水位，只能追加增量 |
| `event_log_available` | false = Redis 读失败/降级，此时"没有事件"**没有结论意义**，必须稍后重试 |
| `oldest_seq` | 日志最早一条；`> client_seq + 1` 说明中间被裁剪，事件已经不存在 |
| `gap_detected` | 明确缺口（配合上一条）；此时即使 `events` 非空也不是连续续传 |
| `head_seq` / `caught_up` | `caught_up` 仅在日志可读且 `head_seq <= last_seq` 时为 true |
| `retry_after_ms` | 截断 / 读失败 / 有缺口 都 > 0；三者都不是才是 0（别再轮询） |

`resume_mode` 语义相应收紧：快照落后时不再返回 `snapshot_only`；日志不可读时也不会返回
`snapshot_only`（那等于谎报"已追平"）。

### P0-7 指标防爆炸 + SCAN 有界

* label 只允许 `scene / provider / model / fallback_from / fallback_to / is_fallback /
  success / result / direction`（`_LLM_LABELS`）；传进来的自由文本按维度上限截断，
  空值 → `unknown`。**禁止** user_id / job_id / conversation_id / prompt；
* 在途任务 SCAN 三重护栏：标记键 7 天 TTL、单次扫描上限
  `_ACTIVE_JOB_SCAN_LIMIT=2000`、时间预算 `_ACTIVE_JOB_SCAN_BUDGET_SECONDS=0.25`。
  命中上限/超时 → 本轮**不更新**主 Gauge（避免采样被当全量）并置
  `lumi_agent_jobs_active_scan_info{degraded="true"}`。

### P0-8 数据库迁移补齐

本地库一直停在 `0011`，`llm_usage.model_role` 缺列 → 每次 LLM 调用都在刷
`UndefinedColumnError`。两个真实原因（线上只要有历史数据同样会踩）：

1. `alembic_version.version_num` 是 Alembic 建的 `VARCHAR(32)`，而
   `0015_result_checkpoint_projection` 是 **33 字符** → 所有 DDL 跑完、写版本号时才报
   `StringDataRightTruncationError`（现场看起来像"某条 DDL 出错"）。0015 里先
   `ALTER COLUMN version_num TYPE VARCHAR(64)` 修掉；
2. 给**已有数据**的表加 `NOT NULL` 列不给 `server_default` → `NotNullViolationError`。
   0014 给 `fallback_used` / `duration_ms` / `tool_calls` 补上默认值。

已实际执行 `alembic upgrade head` 到 0015；`tests/test_migration_executability.py` 把
这两条做成静态守卫（revision 长度、NOT NULL + server_default、单 head 链）。

### P0-9 读接口解耦共享连接池

（本轮未改动 `/jobs` 读接口的池行为——它已在既有 `read_view_cache` 体系内；
若后续出现"读接口拖垮写路径"，按评审建议把读视图拆到独立连接池。）

---

## 1. 策略热更新：Redis Key + TTL + 本地轮询

**不引入 ConfigServer**。三层结构（**代码默认 < `.env` < 运行时覆盖**）里只有第三层需要
运行时可变，而它只需要两个 Redis 键：

```
policy:epoch        # 单调递增字符串；Worker 只比较它，不变则零传输
policy:<epoch>      # Hash：field = provider:<id> / model:<name> / default
                    #        value = {"timeout_seconds":30,"max_concurrent":4,"enabled":true}
```

为什么不原地改同一个 Hash 而是按 epoch 分桶：**读到一半的 Worker 不会被写坏**。旧桶带
TTL（`POLICY_BUCKET_TTL_SECONDS`）自然过期，读到的永远是自洽的一份。

Worker 侧（`PolicyStore`）：

* 启动时拉一次（不留"启动后第一个 10 秒"的策略真空期），随后每
  `POLICY_POLL_INTERVAL_SECONDS` 查一次 epoch；
* epoch 没变 → 只刷新缓存时刻（说明这份缓存仍然可信），**不重载内容**；
* epoch 变了 → 全量替换（不做增量合并，避免脏残留）；
* **Redis 挂了 → 继续用本地缓存；但每条策略带 `max_ttl_seconds`（默认
  `POLICY_CACHE_MAX_TTL_SECONDS=600`），超过它必须退回代码默认值。**
  这是 Fail-Open 的边界：不能因为"读不到新策略"就把旧策略无限期当权威。

生效点（唯一收口处，不新增并行开关）：

| 关注点 | 接入位置 |
| --- | --- |
| 模型超时 / 启停 | `app/core/llm.py::LLMClient._model`（所有 chat/stream/tools/embed 的唯一解析点） |
| 能力 Provider | `app/agents/capabilities/broker.py::select` 的候选过滤（复用 `provider_unhealthy` 事实，用户可见映射不变） |

优先级：`default` → `model` → `provider`，**后者覆盖前者**（越具体越优先；运维要干预的
通常是具体执行方）。单层内逐字段合并，所以"只想改超时"不会顺手把并发或启停改掉。

管理入口（简易运维面板，够用即可）：

```
GET    /api/v1/admin/policies                现状 + 缓存新鲜度 + 轮询状态（superadmin）
PUT    /api/v1/admin/policies                写入/覆盖一条并推进 epoch
DELETE /api/v1/admin/policies/{field}        删除覆盖，回落 .env / 代码默认值
POST   /api/v1/admin/policies/refresh        立刻轮询一次（排障：验证"所有 Worker 都换了"）
POST   /api/v1/admin/policies/write-lease    续签写租约（§4 用）
```

写操作要 **superadmin JWT + `X-Admin-Token`**。这里**刻意没有**沿用
`POST /capabilities/admin/revoke` 的写法——那个端点此前只校验 `require_auth`，
任何登录用户知道 `provider_id` 就能撤销别人的能力租约；本次一并修成
`require_superadmin + X-Admin-Token`。

数值校验在上限层就拦住：`timeout_seconds ∈ [0.1, 3600]`、`max_concurrent ∈ [1, 256]`。
一条手滑写成的 `timeout_seconds=0.001` 会让线上全部 LLM 调用瞬间超时，不能等到生效才发现。

## 2. AST 静态门禁：允许清单 + 禁止调用

```bash
python scripts/check_unsafe_calls.py          # 扫默认根；违规退出码 1
python scripts/check_unsafe_calls.py --list-rules
bash scripts/install_git_hooks.sh             # 装本地 pre-commit（可选）
```

CI：`.github/workflows/ci.yml` 的 **unsafe-calls 作业（阻塞）**。覆盖面比现有 ruff 作业更宽
（额外含 `plugins/`、`scripts/`、`celery_app/`、`tools/`）。

**为什么必须是 AST 而不是 grep**（本仓库实测）：

* `compile(` 命中 176 行，**没有一行**是内建：158 个 `re.compile`、14 个测试里的
  `def _compile`、4 个 `graph.compile(`；
* `eval(` 命中 19 行，全是 `redis.eval(`（Lua）与测试替身；
* `app/services/rag/cleaner.py` 的安全规则表里就有**字符串形式**的 `"os.system("`；
* `shutil.rmtree` 在 `app/api/v1/user.py` 是**裸引用**传给 `asyncio.to_thread`，
  只按 `Call` 扫会漏（门禁按 `Attribute` 也扫裸引用）。

门禁还解析导入别名（`import subprocess as sp` / `from subprocess import run`），
因此改名字绕过无效。禁止清单含 `asyncio.create_subprocess_exec`（两个沙箱的**主**执行路径）
与 `Path.unlink/rmdir`。

允许清单（**受控执行面**，每条都写了理由）：

```
app/services/safe_delete.py                    受控删除的唯一实现
app/agents/sandbox/local.py                    本地沙箱运行器
app/agents/sandbox/docker.py                   Docker 沙箱运行器
app/services/plugins/quota.py                  插件配额进程 worker（超时 kill / 计量 / rlimit）
app/agents/orchestration/temporal/runtime.py   内置 Temporal 开发服务引导
scripts/check_unsafe_calls.py                  门禁自身
tests/test_unsafe_call_gate.py                 门禁的对抗性测试
tests/test_office_docs.py                      内联执行受测的办公转换脚本（脚本生成器是受测对象）
```

为了让清单尽量短，**删除类调用已收口**到 `app/services/safe_delete.py`：6 个业务模块
（账号注销、工作区注销、办公文档清理、制品保留、Celery 清理、文档渲染临时文件）
原先各自 `shutil.rmtree` / `Path.unlink`，现在统一走 `remove_tree` / `remove_file`，
并且**边界校验只有一份实现**（root 必须绝对路径且存在、目标必须在 root 之内、拒绝删根本身）。
测试目录只放宽"删除类"（`tmp_path` 清理），`exec/eval/compile` 在测试里同样禁止。

## 3. 统一绝对截止时间（contextvars）

```python
from app.core.deadline import set_deadline, ensure_budget, get_remaining_budget

token = set_deadline(source="orchestrator.stream")   # 入口设一次
...
deadline_var.reset(token)                            # 退出还原（流式端点必须还原）
```

* 存的是**绝对时刻**（`time.time() + budget`），不是"剩余秒数"——链路上每层各减一次会在
  并发/重试下漂移；
* **没设置时返回 `float("inf")`**：忘记设截止时间退化为旧行为，而不是把所有老路径一次性掐死；
* `with_deadline(...)` 只**缩窄**（`min(现有, 新的)`）：子路径不能把上游给的更紧预算放宽；
* `DeadlineExceeded` 继承 `TimeoutError`：既有"捕获超时"的调用方不用改就能接住。

已接入：`Orchestrator.handle_message_stream`（入口设/还原）、`LLMClient._model`
（`min(模型超时, 剩余预算)` + 预算不足直接抛）。注意 `ainvoke/astream` 外面**没有**
`asyncio.wait_for`，SDK 的 `timeout=` 就是唯一边界，所以必须把剩余预算压进超时值。

### 3.1 任务级预算（后台 Job）——与请求预算**解耦**

`install_job_deadline()`（`JOB_DEADLINE_SECONDS`，默认 1800；`0` = 关闭）。

修的是两个方向相反的毛病：

| 问题 | 症状 |
| --- | --- |
| 后台任务**继承**请求预算 | Job 协程在请求上下文里 `create_task`，contextvars 会被复制。SSE 断开后请求预算可能只剩几秒 → 长任务跑到一半被请求预算掐死（表现为"任务莫名超时"） |
| 后台任务**完全没有**上限 | 没有请求上下文时 `get_remaining_budget()` 是 `inf` → 一个卡住的 Provider 调用能把 worker 永久占住 |

因此语义是**替换 + 显式**：

* 装上任务自己的绝对截止时间，同时**脱离**请求作用域（不是 `with_deadline` 的"取更紧"）；
* 配置为 `0` 时仍然 `clear_deadline()`——"不限制"必须是明确的，而不是继承来的；
* **"一段执行"** 是刻意口径：每次 `ExecutionLoopService.run` 重新锚定，
  暂停/等待审批**不消耗**任务预算（否则审批慢一点就把任务判死），但每一段都有上限；
* 收尾余量按作用域区分（`REQUEST_DEADLINE_MIN_BUDGET_SECONDS` vs
  `JOB_DEADLINE_MIN_BUDGET_SECONDS`），`budget_snapshot()["scope"]` 回报
  `job` / `request` / `unbounded`；
* 预算耗尽的错误码是**稳定的** `JOB_DEADLINE_EXCEEDED` + 可行动文案
  （"拆小重试 / 调大 `JOB_DEADLINE_SECONDS`"），不落成泛化的"任务执行超时"——
  否则运维分不清"上游慢"和"这个任务本来就该被预算停掉"。

接入点：

* `ExecutionLoopService.run`（覆盖 submit / resume / approve / fork 所有进程内路径）；
* Temporal `execute_node_activity`——预算与 `start_to_close_timeout` **同源**（都来自
  节点超时），装在并发闸门**之外**（等锁不算预算）：让内部 LLM/MCP 调用先一步收尾，
  而不是被 Temporal 硬掐后留下半个节点与不一致的副作用状态；
* **单步/自动运行**（`step_run_service`）刻意**不装**：它是请求内同步动作，
  请求预算就是它正确的边界。

## 4. 写操作代际校验：读 Fail-Open，写 Fail-Closed

`app/services/write_gate.py`。本地缓存结构（方案原样）：`lease_granted_at` + `lease_ttl`，
外加 `generation`。判定顺序：

1. 本地**没有**缓存 → 阻断（"没缓存"绝不等于"默认允许"）；
2. 本地缓存**超过 lease_ttl** → 阻断；
3. Redis **不可达 / 报错** → 阻断；
4. Redis 可达但**代际不一致** → 阻断（说明有新状态没同步到本进程，例如刚被撤销）；
5. 只有"缓存新鲜 **且** 代际一致"才放行，并刷新确认时刻。

接入点：`execute_tool_call` 的写类技能（`_skill_is_write` 判定），位置在**审批之后、
执行之前**——审批回答"用户是否同意"，写闸回答"本进程此刻是否仍被授权写"，两者都不能少。

代际推进 (`POST /admin/policies/write-lease` / `write_gate.bump_generation`) 让"撤销"
立刻对所有 Worker 生效，而不需要额外的广播通道。

## 5. 降级成本监控：算"率"而不是只记"数"

新增指标（label 设计按方案）：

```
lumi_llm_usage_tokens_total{model, fallback_from, is_fallback, success, direction}
lumi_llm_cost_usd_total{model, fallback_from, is_fallback}
lumi_llm_calls_total{model, fallback_from, is_fallback, success}
lumi_agent_jobs_active{status}          # 在途任务 Gauge（单任务平均成本的分母）
```

Grafana 直接对比"降级那条线是不是更贵"：

```promql
sum(rate(lumi_llm_cost_usd_total[5m])) by (is_fallback)
/
sum(rate(lumi_agent_jobs_total[5m])) by (is_fallback)
```

成本是**内置价目估算**（`MODEL_PRICE_USD_PER_MTOK`），未知模型返回 0（= 没算出来，不是免费），
因此只用于相对对比。

在途任务 Gauge 的数据源是已有的 `obs:job:{id}` 标记键（值改成状态字符串），只在进行
`/metrics` 抓取时 SCAN 一次——不引入新表、不引入心跳。

## 6. SSE 快照真空期：先读快照，再补增量

```
GET /api/v1/agents/jobs/{job_id}/resume?after_seq=<客户端水位>
```

返回自洽恢复包（**不做全量重放、不重建视图**）：

```json
{
  "resume_mode": "snapshot_delta | snapshot_only | events_only | full_refetch",
  "snapshot": { ...JobRunView 快照（含 last_seq）... },
  "baseline_seq": 7,
  "head_seq": 12,
  "events": [ ...seq > baseline_seq 的标准帧... ],
  "truncated": false,
  "retry_after_ms": 0
}
```

恢复 = 一次快照读 + 一次有界 `LRANGE`，**毫秒级**。`JobRunView.last_seq` 天然就是
"某个事件水位上的检查点"，因此不需要重建快照。`GET /jobs/{id}/events` 也补了
`snapshot_seq` / `head_seq` / `caught_up`，让客户端能区分"追平了"与"被自己的水位过滤光了"。

快照键与序列化只属于 `app/services/job_snapshot_store.py`（新增 `read_snapshot_payload`
作为恢复路径的唯一入口）——这条契约由 `tests/test_job_snapshot_write_guard.py` 静态守着，
多一个模块拼同一个键就多一条可能绕过体积收缩的路径。

---

## 测试

| 文件 | 覆盖 |
| --- | --- |
| `tests/test_runtime_policy_and_deadline.py` | epoch 刷新、不抹掉其它主体、max_ttl 退回默认值、轮询启停、脏数据容错；截止时间默认不限制/只缩窄/任务隔离/TimeoutError；写闸四种阻断 + 一种放行；成本估算与 label |
| `tests/test_job_deadline.py` | 任务预算**替换**（而非取 min）请求预算、关闭时显式不限制、每段执行重新锚定（审批不消耗）、子路径只能缩窄、作用域化收尾余量、`budget_snapshot.scope`、执行循环装上并还原、预算耗尽 → `JOB_DEADLINE_EXCEEDED`、Temporal 预算与节点超时同源、并发任务各拿一份 |
| `tests/test_admin_policy_api.py` | 权限与校验、写后本进程立即生效、删除回落默认值、Redis 不可达时租约签发报错 |
| `tests/test_unsafe_call_gate.py` | 对抗性：真违规必拦（别名/裸引用/异步子进程/pathlib）、合法写法零误报（`re.compile`/`redis.eval`/字符串）、真实仓库 0 违规 |
| `tests/test_resume_snapshot_gap.py` | 水位只扫尾部；快照覆盖过的事件不再返回；快照缺失/损坏/超大 gap 的降级与截断 |
