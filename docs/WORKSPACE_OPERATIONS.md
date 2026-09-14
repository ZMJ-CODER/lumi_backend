# 工作区操作契约（Operation Contract）

> 后端实现说明 + 前端对接要点。落点：`app/contracts/operations/`（契约）、
> `app/workspace/write/operations.py`（执行网关）、
> `app/workspace/write/revision.py`（版本）、`app/workspace/write/trash.py`（回收站）。

## 1. 链路

```
Skill / DAG Node / 模型工具
  → Capability Broker / executor 操作分支（审批指纹校验）
  → WorkspaceOperationService（上下文注入 → 策略 → 版本 → 审批 → 幂等 → 读回校验）
  → 客户端原子工具（workspace_read / workspace_list / workspace_write / workspace_edit /
                    workspace_move / workspace_delete，
                    遗留路径：workspace_stage_write / workspace_stage_delete / workspace_commit）
  → OperationResult
  → ToolOutput 兼容投影（唯一执行信封）
  → Model / UI / Audit 投影
```

**没有第二套信封**：`OperationResult` 只提供"操作语义"这一层 payload，
经 `OperationResult.to_tool_output()` 折进既有 `ToolOutput`（`app/agents/skills/output_contract.py`），
下游投影链路不变。

## 2. 能力名与工具

| 能力 | 规范工具名 | 副作用 | 审批 | 说明 |
|---|---|---|---|---|
| `workspace.write@1` | `workspace_write` | write | 必须 | 覆盖已有文件必须带 `expected_revision` |
| `workspace.edit@1` | `workspace_edit`（别名 `code.edit`） | write | 必须 | 默认要求 `old_str` 唯一匹配 |
| `workspace.move@1` | `workspace_move` | write+delete | 必须 | 目录移动/跨设备当前不支持 |
| `workspace.delete@1` | `workspace_delete` | delete | 必须 | 默认进回收站；`permanent=true` 高危审批 |

`workspace_write/edit/move/delete` 是**内部 Tool**（`public=False`，服务端执行）：它们不进
`ToolRegistry.list()` 的公开面，但在**写阶段窗口**里由
`app/agents/orchestration/react_runner.py::_maybe_inject_workspace_stage_window` 显式注入
模型 function calling 池（读阶段仍然只给聚合入口 `workspace_navigator`）。两条 Workflow
路径（`workspace_operation` / `workspace_code_change`）同样把四个原子工具列入
`allowed_tools`，并按可选依赖登记，老客户端只广告暂存对时工作流不会判为不可用。

## 3. 状态词表（`error` 只表达错误）

```text
success        真的改动了工作区
no_change      内容/目标状态已一致（写入同样内容、edit 的 old==new、dry_run 预览）
pending_approval 已准备就绪，等本机/服务端审批（不是错误！）
denied         被策略或用户拒绝（不是"执行失败"）
failed         执行失败，看 error.code
already_absent 删除目标本来就不存在（幂等成功态）
```

映射到既有 `ToolOutput.status`：`success/no_change/already_absent → success`、
`pending_approval → pending_approval`、`denied/failed → failed`（`metadata.status` 保留原值）。

## 4. 版本（revision）唯一算法

```text
文件：sha256:<16 位内容哈希>:<字节数>
目录：dir1:<16 位条目摘要>:<条目数>     # 条目=名字/类型/大小/子版本，排序后哈希
```

* 内容变了版本必变；**不**引入 mtime（服务端拿不到可信 mtime）；
* 匹配接受：完整串 / 裸哈希 / ≥8 位前缀；
* 读取入口在"整篇读完"时给出 `meta.revision` + `expected_revision_hint`，
  行区间读取也会给出整文件版本（基于整篇原文），分页中间页**不给**；
* `write/edit/move/delete` 全部用同一算法，因此 write 返回的版本可以直接喂给 edit/delete。

## 5. 回收站 `.lumi_trash`

| 约束 | 实现位置 |
|---|---|
| 默认不出现在 `list` | 客户端 navigator 跳过；后端 `NavigatorListHandler` 过滤 + `meta.trash_hidden` |
| 不能被 `read` 读取 | `NavigatorReadHandler` → `TRASH_PATH_FORBIDDEN` |
| 不参与 `search`/`scan` | 同上（search 还会过滤命中） |
| 只能由恢复/清理接口访问 | `WorkspaceOperationService.list_trash/restore/purge` + 客户端 IPC `workspaceTrash:*` |
| 记录原始路径/任务/删除时间/恢复信息 | 客户端 `.lumi_trash/trash.json`（`trash_id/logical_path/kind/bytes/files/dirs/deleted_at/task_id/expires_at`） |
| 保留期与配额 | 7 天 / 500 条（客户端 `TRASH_RETENTION_DAYS` / `TRASH_QUOTA_ENTRIES`，与本文件 `TrashPolicy` 默认值一致） |
| 移动失败不得报告删除成功 | 客户端 `TRASH_MOVE_FAILED` → `TRASH_UNAVAILABLE`；后端再读回校验源是否真的消失 |

## 6. 事件与 Job 快照

* 事件（Redis 流，`app/services/operation_events.py`）：
  `operation_started` / `operation_preview` / `approval_required` /
  `operation_completed` / `operation_failed` / `operation_rolled_back`；
  字段：`job_id / step_id / operation_id / operation / logical_path / status /
  revision / changed_files / approval_state / rollback_available / error_code /
  safe_next_action / sequence`；
* Job 快照（`app/services/operation_snapshots.py`，挂在 `run_view.operation_summary`）：
  `operations[] / latest / changed_files / approval_state / rollback_available / no_change`；
* **不塞正文**：完整 Diff 或文件内容需走受权限保护的接口按需获取。

## 7. 客户端实现与诚实边界（与前端 `CAPABILITY_BRIDGE.md` §11.5 对齐）

| 能力 | 客户端实现 | 后端如实标注 |
|---|---|---|
| `workspace.write@1` | 暂存 → 审批 → **同目录临时文件 + fsync + rename** 原子替换 | `atomic_replace: true`；原子替换会换 inode ⇒ `permissions_preserved: false`（结果带 warning） |
| `workspace.edit@1` | 服务端读→版本校验→严格单点匹配，提交复用客户端原子写入 | `atomic_replace: true`；`old_revision`/`new_revision` 为内容哈希 |
| `workspace.move@1` | **同一文件系统内单次 `rename`**（文件与目录都原子） | `atomic: true`；目录移动返回目录快照版本；跨设备 `CROSS_DEVICE_UNSUPPORTED`（绝不先复制再删）；目标已存在 `ALREADY_EXISTS`（rename 不覆盖） |
| `workspace.delete@1` | 默认 **同盘 `rename` 移入 `.lumi_trash/<trash_id>`**（原子、可恢复，不读内容）；`permanent=true` 才 `rm` | `reversible: true` + `trash_id`；目录/二进制同样支持；非空目录未递归 → `NOT_EMPTY_DIRECTORY`；永久删除服务端 + 客户端双重确认 |

回收站由**客户端**维护（`.lumi_trash/<trash_id>` + `.lumi_trash/trash.json`，保留 7 天 / 上限 500 条）：
后端只读它的索引；`restore` 用 `workspace_move` rename 回原路径（支持目录与二进制），
`purge` 用"暂存删除 + 重写索引 + 一次提交"（客户端把 `.lumi_trash` 视为受保护路径，
不允许 `workspace_delete` 直接删它）。

仍保留的边界：跨设备移动默认拒绝；完整 Diff 仍走既有 `workspace_diff`，操作结果只给
`added_lines` / `removed_lines` / 文件数 / 目录数 / 字节数；受保护路径（`.git` /
`.lumi_trash` / `.env` / `.env.local`）在工具层与能力层双重拒止。

`NOT_SUPPORTED_BY_PROVIDER` 与 `TRASH_CONTENT_UNREADABLE` 仍是合法错误码，
保留给**真正做不到的 Provider**（例如未实现 rename 的实现、必须读内容才能归档的实现）。

## 8. 前端对接要点（本轮未改前端）

1. 只消费统一结果：`operation / status / logical_path / revision / changes /
   approval_state / rollback_available / error`；
2. `no_change` / `already_absent` **不要**弹确认框（它们不是失败，也不需要审批）；
3. `pending_approval` 走既有确认流程；`denied` 与 `failed` 必须分开渲染；
4. 删除面板显示：路径、文件数/目录数/大小、是否递归、是否永久、是否可撤销；
   进回收站成功时显示"已移入回收站 + 撤销入口"，而不是"已永久删除"；
5. 移动面板显示源 → 目标、目标冲突、`error.safe_next_action`；
6. 事件里 `status` 是操作状态词表，过程状态在 `process_status`（不要用 `status` 判终态）；
7. `run_view.operation_summary` 用于刷新后恢复操作面板。

## 9. 本轮需要前端确认的两点（后端不改前端）

1. **写阶段窗口现在最多 5 项**：`workspace_navigator` + `workspace_write` / `workspace_edit` /
   `workspace_move` / `workspace_delete`。后端按阶段注入，不再只给暂存对。
   如果客户端把 `permission_profile.model_visible_tools`（默认只有 `['workspace_navigator']`、
   描述里写"阶段化写入最多 3 项"，见 `CAPABILITY_BRIDGE.md` §2 末）当成**调用期**白名单，
   请把上限放宽到容纳这 4 个原子工具；若它只是上报字段、后端不读（当前后端确实只解析
   `approval_mode` / `policy_version` / `issued_at` / `expires_at`），则只需更新文档。
2. **解答正文的换行与转义**：`SseEventEncoder` 用 `json.dumps(..., ensure_ascii=False)` 单行
   发帧，增量里的真实换行在 `data` 里是标准 JSON 转义 `\n`。前端必须
   `JSON.parse` 之后取 `content` **原样**累加，再交给 Markdown 渲染；不要把 `\n` 当字面量
   显示，也不要在渲染前自行转义 `_` / `*` / 反引号（后端不做 Markdown 转义，
   `\_\_name\_\_` 这类转义只可能来自前端渲染前的处理）。
