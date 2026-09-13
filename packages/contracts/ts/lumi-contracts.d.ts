/**
 * 由 packages/contracts/scripts/export_ts.py 生成，请勿手工修改。
 *
 * 契约版本：ExecutionResult=lumi.execution_result@1, JobRunView=lumi.job_run_view@1, ResultRef=lumi.result_ref@1, RouteDecision=lumi.route_decision@1, StepCheckpoint=lumi.step_checkpoint@1, StreamEvent=lumi.stream_event@1, TaskProfile=lumi.task_profile@1, ToolRequest=lumi.tool_request@1
 * 后端模型：lumi_contracts（packages/contracts）
 * 重新生成：python packages/contracts/scripts/export_ts.py
 */


export type ActionIntent = "READ" | "SEARCH" | "CREATE" | "MODIFY" | "DELETE" | "EXECUTE" | "SEND" | "PUBLISH";

export type ApprovalDecision = "pending" | "approved" | "rejected" | "expired";

export type ApprovalScope = "call" | "task" | "workspace";

/** 一次审批的完整状态（可持久化、可恢复）。 */

export interface ApprovalState {
  call_id?: string;
  job_id?: string;
  tool_name?: string;
  scope?: ApprovalScope;
  decision?: ApprovalDecision;
  risk?: string;
  reason?: string;
  fingerprint?: string;
  requested_at?: number;
  resolved_at?: number;
  expires_at?: number;
  resolved_by?: string;
  details?: Record<string, unknown>;
}

/** 后端产物引用；不允许把宿主路径或凭据暴露给模型。 */

export interface ArtifactRef {
  ref_id: string;
  name?: string;
  media_type?: string;
  size?: number | null;
  schema_name?: string;
  internal_locator?: string;
  retention_class?: string;
  requested_expires_at?: string;
  effective_expires_at?: string;
  retention_policy_source?: string;
  retention_clamp_reason?: string;
}

/** 一个能力最终绑定到谁（按会话/工作区/设备维度）。 */

export interface CapabilityBinding {
  capability: string;
  provider_id?: string;
  deployment?: string;
  execution_plane?: string;
  runtime_kind?: string;
  executor_type?: string;
  device_id?: string;
  workspace_id?: string;
  contract_version?: number;
  data_locality?: string;
  routed_by_policy?: boolean;
}

/** 能力描述符（Provider 注册时提交，Broker 据此路由与校验）。 */

export interface CapabilityDescriptor {
  name: string;
  contract_version?: number;
  summary?: string;
  input_schema?: Record<string, unknown>;
  output_schema?: Record<string, unknown>;
  side_effects?: SideEffectKind[];
  data_locality?: DataLocality;
  execution_plane?: ExecutionPlane | null;
  runtime_kind?: RuntimeKind | null;
  required_permissions?: string[];
  needs_local_confirmation?: boolean;
  streamable?: boolean;
  sensitivity?: string;
  error_codes?: string[];
  artifact_when_large?: boolean;
}

export type CapabilityErrorCode = "PROVIDER_OFFLINE" | "PROVIDER_UNHEALTHY" | "CAPABILITY_UNAVAILABLE" | "CAPABILITY_MISSING" | "LEASE_EXPIRED" | "CONTRACT_VERSION_MISMATCH" | "DEPLOYMENT_NOT_ALLOWED" | "UNKNOWN_PLUGIN_KIND" | "APPROVAL_REQUIRED" | "APPROVAL_INVALID" | "APPROVAL_EXPIRED" | "POLICY_DENIED" | "PERMISSION_DENIED" | "LOCAL_POLICY_DENIED" | "SCOPE_DENIED" | "INVALID_ARGUMENTS" | "INVALID_RESULT" | "WORKSPACE_NOT_BOUND" | "WORKSPACE_DEVICE_OFFLINE" | "WORKSPACE_NOT_REGISTERED" | "WORKSPACE_ROOT_MISSING" | "WORKSPACE_PATH_NOT_DIRECTORY" | "TIMEOUT" | "CANCELLED" | "FAILED" | "RESOURCE_EXHAUSTED";

/** 一次能力调用（跨服务端 ↔ 客户端 Provider 的唯一请求形状）。 */

export interface CapabilityInvocation {
  capability: string;
  contract_version?: number;
  arguments?: Record<string, unknown>;
  scope?: Record<string, unknown>;
  deadline?: number;
  deployment?: string;
  request_id?: string;
  trace_id?: string;
  session_binding?: SessionBinding;
  job_id?: string;
  node_id?: string;
  approval_token?: string;
  idempotency_key?: string;
  stream_cursor?: string;
  timeout_seconds?: number;
}

/** 预检结论（可落 Job 快照、可下发给前端）。 */

export interface CapabilityPreflight {
  state?: PreflightState;
  error_code?: string;
  safe_message?: string;
  safe_next_action?: string;
  question?: string;
  options?: string[];
  tool_window?: string[];
  must_call_model?: boolean;
  checks?: Record<string, unknown>[];
  required_capabilities?: string[];
}

/** 一次能力调用的结果（客户端 Provider 与服务端 Provider 返回同一种）。 */

export interface CapabilityResult {
  status?: ExecutionStatus;
  payload?: unknown;
  schema_name?: string;
  schema_version?: number;
  artifact_refs?: ArtifactRef[];
  error?: ErrorEnvelope | null;
  sensitivity?: string;
  usage?: CapabilityUsage;
  request_id?: string;
  trace_id?: string;
  capability?: string;
  provider_id?: string;
  contract_version?: number;
  stream_cursor?: string;
  served_locally?: boolean;
  execution_plane?: ExecutionPlane | null;
  runtime_kind?: RuntimeKind | null;
}

export type CapabilityStatus = "idle" | "requested" | "waiting_provider" | "waiting_approval" | "running" | "completed" | "failed" | "denied" | "unavailable";

/** 用量（审计/计费/限流用；不含正文）。 */

export interface CapabilityUsage {
  started_at?: number;
  finished_at?: number;
  duration_ms?: number;
  queue_ms?: number;
  bytes_in?: number;
  bytes_out?: number;
}

export type Complexity = "ATOMIC" | "SEQUENTIAL" | "DYNAMIC";

export type ConfidenceSource = "llm" | "heuristic" | "rule_corrected";

/** 任务级控制信号（完成/失败/取消/暂停），取代各调用点自造错误结构。 */

export interface ControlPayload {
  state?: string;
  error_code?: string;
  reason_code?: string;
  reason?: string;
  next_action?: string;
  safe_next_action?: string;
  phase?: string;
  question?: string;
  options?: string[];
  required_capabilities?: string[];
  tool_window?: string[];
  must_call_model?: boolean | null;
}

export type DataLocality = "local_only" | "cloud" | "hybrid";

export type Deployment = "server" | "client" | "worker" | "local_dev";

/** 统一错误信封：稳定错误码 + 人类可读信息 + 重试与自我修正提示。 */

export interface ErrorEnvelope {
  code: string;
  message?: string;
  retryable?: boolean;
  suggested_action?: string;
  details?: Record<string, unknown>;
}

export type ExecutionPlane = "server" | "client";

/** 一次执行的请求：服务端上下文 + 指令 + 能力约束。 */

export interface ExecutionRequest {
  instruction?: string;
  context?: ServerContext;
  route?: RouteDecision | null;
  allowed_tools?: string[];
  denied_tools?: string[];
  max_steps?: number;
  max_chars?: number;
  data_sensitivity?: Sensitivity;
  idempotency_key?: string;
  approval_fingerprint?: string;
}

/** 一次工具/Skill/节点执行的统一结果。 */

export interface ExecutionResult {
  status?: ExecutionStatus;
  payload?: unknown | null;
  schema_name?: string;
  schema_version?: number;
  tool_name?: string;
  namespace?: string;
  trace_id?: string;
  request_id?: string;
  call_id?: string;
  job_id?: string;
  node_id?: string;
  error?: ErrorEnvelope | null;
  retryable?: boolean;
  partial?: boolean;
  timing?: ExecutionTiming;
  artifact_refs?: ArtifactRef[];
  sensitivity?: string;
  output?: string;
  content_type?: string;
  metadata?: Record<string, unknown>;
}

export type ExecutionStatus = "success" | "partial" | "empty" | "pending" | "pending_approval" | "uncertain" | "failed" | "cancelled";

export type ExecutionTarget = "SERVER" | "DESKTOP" | "SANDBOX" | "NONE";

/** 耗时统计（毫秒）；缺省 0 表示未采集，不表示"瞬间完成"。 */

export interface ExecutionTiming {
  started_at?: number;
  finished_at?: number;
  duration_ms?: number;
  queue_ms?: number;
}

export type InfoSource = "USER_PROVIDED" | "CONVERSATION_MEMORY" | "INTERNAL_KNOWLEDGE" | "WORKSPACE" | "ATTACHED_FILE" | "PUBLIC_WEB" | "PRIVATE_SERVICE" | "SYSTEM_STATE";

export type IntentType = "GENERATE_ONLY" | "EXECUTE_ACTION";

export type IsolationLevel = "in_process" | "restricted_worker" | "sandboxed" | "client_device";

/** 一次运行的持久化快照。 */

export interface JobRunView {
  job_id?: string;
  conversation_id?: string;
  status?: RunState;
  next_action?: string;
  plan_text?: string;
  plan_revision?: number;
  steps?: StepView[];
  routing?: Record<string, unknown>;
  process_log?: ProcessLogEntry[];
  final_answer?: string;
  last_seq?: number;
  artifact_refs?: Record<string, unknown>[];
  views?: Record<string, unknown>[];
  error?: string | null;
  error_code?: string | null;
  updated_at?: number;
  version?: number;
  log_archive_ref?: string;
  log_archive_count?: number;
  truncated?: boolean;
}

/** 入口声明：按插件类型给出可执行入口（服务端/Worker 侧才是代码入口）。 */

export interface PluginEntrypoints {
  module?: string;
  callable?: string;
  capability?: string;
  view_type?: string;
  policy_id?: string;
  transport?: string;
}

/** 健康检查声明：怎么判断插件还活着（安装期 + 运行期共用）。 */

export interface PluginHealthcheck {
  kind?: string;
  target?: string;
  interval_seconds?: number;
  timeout_seconds?: number;
  failure_threshold?: number;
}

export type PluginKind = "skill_plugin" | "capability_provider" | "policy_pack" | "view_plugin" | "extension_handler";

/** 插件自述契约。``schema_version`` 是契约版本（不是插件版本）。 */

export interface PluginManifest {
  schema_version?: number;
  id: string;
  version: string;
  kind: PluginKind;
  deployment?: Deployment;
  name?: string;
  description?: string;
  requires?: PluginRequires;
  provides?: PluginProvides;
  entrypoints?: PluginEntrypoints;
  permissions?: PluginPermission[];
  data_locality?: DataLocality;
  isolation?: IsolationLevel;
  execution_plane?: ExecutionPlane | null;
  runtime_kind?: RuntimeKind | null;
  side_effects?: SideEffectKind[];
  resource_limits?: PluginResourceLimits;
  healthcheck?: PluginHealthcheck;
  signature?: PluginSignature;
  trust_level?: TrustLevel;
}

/** 一条权限声明：能做什么 + 作用域 + 是否需要本机确认。 */

export interface PluginPermission {
  name: string;
  scope?: string;
  required?: boolean;
  needs_local_confirmation?: boolean;
  reason?: string;
}

/** 产出声明：能力、策略、视图、技能入口。 */

export interface PluginProvides {
  capabilities?: string[];
  policies?: string[];
  views?: string[];
  skills?: string[];
}

/** 快照里的单个插件引用（只放恢复/审计需要的字段）。 */

export interface PluginRef {
  id: string;
  version: string;
  kind?: string;
  deployment?: string;
  digest?: string;
  trust_level?: string;
}

/** 依赖声明：能力、其他插件、最低 Lumi 版本、策略包。 */

export interface PluginRequires {
  capabilities?: string[];
  plugins?: string[];
  min_lumi_version?: string;
  policies?: string[];
}

/** 资源上限（服务端与客户端都据此拒绝，而不是"尽力而为"）。 */

export interface PluginResourceLimits {
  memory_mb?: number;
  cpu_seconds?: number;
  wall_seconds?: number;
  max_output_bytes?: number;
  max_concurrency?: number;
}

/** 签名声明与验签结果（``verified`` 只能由安装器写入）。 */

export interface PluginSignature {
  algorithm?: string;
  key_id?: string;
  digest?: string;
  value?: string;
  signed_at?: number;
  verified?: boolean;
  verified_at?: number;
  signer?: string;
}

/** 任务使用的插件/Provider/策略/能力绑定快照。 */

export interface PluginSnapshot {
  skills?: PluginRef[];
  providers?: ProviderRef[];
  policies?: PolicyRef[];
  capabilities?: CapabilityBinding[];
}

export interface PolicyRef {
  id: string;
  version?: string;
  source?: string;
}

export type PreflightState = "READY" | "DEPENDENCY_MISSING_WORKSPACE" | "CAPABILITY_UNAVAILABLE" | "PROVIDER_UNHEALTHY" | "PERMISSION_DENIED" | "TOOL_NOT_REGISTERED" | "NEEDS_CLARIFICATION" | "APPROVAL_REQUIRED" | "SECURITY_BLOCKED";

export type ProcessKind = "thinking" | "read" | "edit" | "command" | "tool" | "system";

/** 一条执行过程记录（安全摘要 + 去重键 + 状态）。 */

export interface ProcessLogEntry {
  id?: string;
  entry_id?: string;
  kind?: ProcessKind;
  title?: string;
  summary?: string;
  detail?: string;
  status?: ProcessStatus | string;
  step_id?: string;
  call_id?: string;
  tool_name?: string;
  sequence?: number;
  occurred_at?: string;
  job_id?: string;
  capability?: string | null;
  resource_type?: string | null;
  provider_id?: string | null;
  provider_name?: string | null;
  display_name?: string | null;
}

export type ProcessStatus = "running" | "completed" | "failed" | "pending" | "uncertain" | "cancelled" | "expired";

export type ProviderHealth = "healthy" | "degraded" | "unhealthy" | "offline" | "unknown";

/** 一条能力租约（``POST /capabilities/register`` 建立，心跳续期）。 */

export interface ProviderLease {
  provider_id: string;
  capability: string;
  contract_version?: number;
  lease_id?: string;
  user_id?: string;
  device_id?: string;
  conversation_id?: string;
  workspace_id?: string;
  session_id?: string;
  deployment?: Deployment;
  execution_plane?: ExecutionPlane | null;
  runtime_kind?: RuntimeKind | null;
  trust_level?: TrustLevel;
  plugin_id?: string;
  plugin_version?: string;
  provider_version?: string;
  scope?: Record<string, unknown>;
  health_status?: ProviderHealth;
  issued_at?: number;
  expires_at?: number;
  last_heartbeat_at?: number;
}

/** 快照里参与过调用的 Provider。 */

export interface ProviderRef {
  id: string;
  version?: string;
  deployment?: string;
  execution_plane?: string;
  runtime_kind?: string;
  executor_type?: string;
  plugin_id?: string;
  device_id?: string;
  health_status?: string;
}

/** 统一结果引用（业务层唯一可见的结果定位符）。 */

export interface ResultRef {
  id: string;
  sha256?: string;
  storage_kind?: string;
  content_type?: string;
  size?: number;
  owner_id?: string;
  job_id?: string;
  step_id?: string;
  schema_name?: string;
  schema_version?: number;
  expires_at?: number;
  created_at?: number;
  artifact_refs?: ArtifactRef[];
}

export type ResultStorageKind = "redis" | "local" | "blob";

/** 路由决策：只描述"选了什么"，不携带工具实现。 */

export interface RouteDecision {
  mode?: RouteMode;
  reason?: string;
  profile?: TaskProfile | null;
  signals?: Record<string, unknown>;
  required_capabilities?: string[];
  blocked_reason?: string;
  schema_version?: number;
  route_mode?: string;
  intent_type?: string;
  action_intents?: string[];
  target_scope?: string;
  target_clarity?: string;
  approval_required?: boolean;
  needs_clarification?: boolean;
  confidence?: number;
  confidence_source?: string;
  decision_reason_code?: string;
  capability_preflight?: CapabilityPreflight | null;
}

export type RouteMode = "direct_chat" | "m1_atomic_read" | "single_action_skill" | "planner_dag" | "react" | "blocked";

export type RunState = "pending" | "planning" | "waiting_run" | "running" | "running_step" | "waiting_approval" | "waiting_next" | "completed" | "failed" | "cancelled" | "interrupted";

export type RuntimeKind = "in_process" | "worker" | "container" | "sandbox";

export type Sensitivity = "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "CREDENTIAL";

/** 服务端注入的执行上下文（冻结：插件不可改写身份字段）。 */

export interface ServerContext {
  user_id?: string;
  user_role?: string;
  conversation_id?: string;
  job_id?: string;
  workspace_id?: string;
  device_id?: string;
  data_sensitivity?: Sensitivity;
  approval_grant_hash?: string;
  office_doc_ids?: string[];
  authorized_project_ids?: string[];
  request_id?: string;
  trace_id?: string;
  extra?: Record<string, unknown>;
}

/** 能力查找的最小绑定集：不能只按 ``user_id`` 找 Provider。 */

export interface SessionBinding {
  user_id?: string;
  conversation_id?: string;
  workspace_id?: string;
  device_id?: string;
  session_id?: string;
}

export type SideEffectKind = "none" | "read" | "write" | "delete" | "execute" | "network" | "external";

/** 一个 Skill 的执行结果（统一控制信息 + 步骤账 + 业务 payload）。 */

export interface SkillResult {
  skill_name?: string;
  namespace?: string;
  version?: string;
  status?: ExecutionStatus;
  payload?: unknown | null;
  steps?: SkillStep[];
  error?: ErrorEnvelope | null;
  retryable?: boolean;
  partial?: boolean;
  timing?: ExecutionTiming;
  artifact_refs?: ArtifactRef[];
  sensitivity?: string;
  trace_id?: string;
  request_id?: string;
  call_id?: string;
  job_id?: string;
  node_id?: string;
}

/** Skill 内部的一次工具调用（可审计、可回放，不含正文）。 */

export interface SkillStep {
  name?: string;
  index?: number;
  status?: ExecutionStatus;
  call_id?: string;
  tool?: string;
  duration_ms?: number;
  error_code?: string;
  detail?: string;
}

/** 一步的检查点记录（方案 §2.2 的全部字段）。 */

export interface StepCheckpoint {
  checkpoint_contract_version?: number;
  job_id?: string;
  step_id?: string;
  attempt?: number;
  tool_name?: string;
  step_type?: string;
  effect_type?: string;
  idempotency_key?: string;
  status?: StepCheckpointState;
  started_at?: number;
  finished_at?: number;
  input_digest?: string;
  output_summary?: string;
  result_ref?: Record<string, unknown> | null;
  artifact_refs?: Record<string, unknown>[];
  error_code?: string;
  effect_status?: string;
  checkpoint_version?: number;
  updated_at?: number;
}

export type StepCheckpointState = "planned" | "started" | "running" | "waiting_approval" | "completed" | "failed" | "cancelled" | "uncertain";

export type StepRuntimeStatus = "idle" | "running" | "waiting_approval" | "settled";

/** 单步展示与恢复信息。 */

export interface StepView {
  id?: string;
  title?: string;
  status?: string;
  runtime_status?: string;
  tool?: string;
  output?: string;
  display_summary?: string;
  error?: string | null;
  error_code?: string | null;
  depends_on?: string[];
  resource_claims?: string[];
  effect_status?: string | null;
  attempt?: number;
  started_at?: number | null;
  completed_at?: number | null;
  duration_ms?: number | null;
  result_ref?: Record<string, unknown> | null;
}

/** 统一流式事件（版本化 + 递增序列号）。 */

export interface StreamEvent {
  type: string;
  version?: number;
  seq?: number;
  job_id?: string;
  conversation_id?: string;
  call_id?: string;
  step_id?: string;
  data?: Record<string, unknown>;
}

export type TargetClarity = "KNOWN" | "UNKNOWN";

export type TargetScope = "WORKSPACE" | "ATTACHMENT" | "USER_INPUT" | "EXTERNAL_SERVICE" | "SYSTEM_STATE" | "NONE";

/** 任务画像：**唯一权威定义**（方案 §3.1；contracts 之外只保留适配器）。 */

export interface TaskProfile {
  goal?: string;
  complexity?: Complexity;
  side_effects?: boolean;
  info_sources?: InfoSource[];
  output_target?: string;
  execution_target?: ExecutionTarget;
  required_capabilities?: string[];
  risk_level?: string;
  confidence?: number;
  debug?: Record<string, unknown>;
  intent_type?: IntentType;
  action_intents?: ActionIntent[];
  target_scope?: TargetScope;
  target_clarity?: TargetClarity;
  has_dependency?: boolean;
  has_runtime_decision?: boolean;
  approval_required?: boolean;
  confidence_source?: ConfidenceSource;
  decision_reason_code?: string;
}

/** 工具调用命令：参数与身份分离，身份只来自服务端上下文。 */

export interface ToolRequest {
  tool_name: string;
  namespace?: string;
  arguments?: Record<string, unknown>;
  call_id?: string;
  request_id?: string;
  trace_id?: string;
  idempotency_key?: string;
  approval_fingerprint?: string;
  timeout_s?: number | null;
}

export type TrustLevel = "builtin" | "official" | "third_party" | "local_dev";

/** 一个声明式视图贡献（后端产出，前端用官方组件渲染）。 */

export interface ViewContribution {
  view_type: string;
  data?: Record<string, unknown>;
  schema_version?: number;
  permissions?: string[];
  title?: string;
  sensitivity?: string;
  source?: string;
  plugin_id?: string;
}

/** 已知流式事件类型；前端按 type 分派，**必须忽略未知类型**而不是白屏。 */
export type StreamEventType = "job" | "task_router" | "process" | "delta" | "tool_started" | "tool_completed" | "step" | "step_started" | "step_completed" | "plan_delta" | "plan_ready" | "waiting_next" | "approval_required" | "approval_resolved" | "task_completed" | "task_failed" | "done" | "error";
