"""应用配置，基于 pydantic-settings 从环境变量 / .env 加载."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings

# 项目根目录：app/core/config.py → 上溯三级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # ── 基础 ──
    PROJECT_NAME: str = "Lumi Backend"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

    # ── 数据库连接池（每进程） ──
    # 默认 10 + 20 溢出 = 30 连接/进程。多 worker 部署必须按
    # worker 数、Celery 进程和 PostgreSQL max_connections 共同预算，不能直接沿用。
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # 取连接时是否先 SELECT 1 探活。本地/局域网稳定环境下关闭可降低每请求 1 次
    # 额外 DB 往返（压测约 +20% 吞吐）；公网/容器网络抖动环境建议开启。
    DB_PRE_PING: bool = False

    # ── 计算密集型任务线程池（OCR / Embedding / TTS 等，每进程） ──
    # 独立于 asyncio 默认线程池，避免并发上传把 Web 服务后台线程占满。
    COMPUTE_THREADS: int = 4

    # ── 可观测性 ──
    SENTRY_DSN: str = ""            # 错误上报；为空则不启用 Sentry
    METRICS_ENABLED: bool = True    # /metrics Prometheus 指标

    # ── 用户高频只读视图缓存（Redis，严格按用户隔离） ──
    READ_VIEW_CACHE_ENABLED: bool = True
    READ_VIEW_USER_TTL_SECONDS: int = 5
    READ_VIEW_MEMORY_TTL_SECONDS: int = 15
    READ_VIEW_CONVERSATIONS_TTL_SECONDS: int = 10

    # ── 安全（全局限流） ──
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_GENERAL_PER_MINUTE: int = 300   # 通用接口：每 IP 每分钟（客户端轮询 1s/次，需留余量）
    RATE_LIMIT_AUTH_PER_MINUTE: int = 20       # 登录/注册/验证码：更严
    # 已登录用户的精细令牌桶：固定窗口仍保留作最外层 IP 防刷；以下限制按用户
    # （游客按 IP）生效，避免一个正常的办公长任务被其他会话的请求挤占。
    RATE_LIMIT_USER_ENABLED: bool = True
    RATE_LIMIT_CHAT_STREAM_CAPACITY: int = 12
    RATE_LIMIT_CHAT_STREAM_REFILL_PER_MINUTE: float = 12.0
    RATE_LIMIT_OFFICE_SUBMIT_CAPACITY: int = 4
    RATE_LIMIT_OFFICE_SUBMIT_REFILL_PER_MINUTE: float = 2.0
    RATE_LIMIT_UPLOAD_CAPACITY: int = 10
    RATE_LIMIT_UPLOAD_REFILL_PER_MINUTE: float = 10.0

    # 外部服务熔断：只统计网络、超时、429 和 5xx；认证/余额/参数错误必须直接反馈
    # 给用户，不能因单个用户的配置错误把整条服务链路熔断。
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_RECOVERY_SECONDS: float = 30.0
    CIRCUIT_BREAKER_HALF_OPEN_PROBE_SECONDS: float = 15.0

    # ── CORS ──
    CORS_ORIGINS: list[str] = ["*"]  # 生产环境收敛为具体域名（通配符时不允许带凭据）

    # ── JWT ──
    JWT_SECRET_KEY: str = "change-me-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_SECONDS: int = 3600          # 1 小时（短期，每次请求用）
    REFRESH_TOKEN_EXPIRE_SECONDS: int = 2592000      # 30 天（长期，滑动刷新）
    ADMIN_VERIFIED_TOKEN_EXPIRE_SECONDS: int = 300   # 5 分钟

    # ── 密码安全（服务端 argon2id，每用户独立盐，无需全局盐）──
    PASSWORD_MIN_LENGTH: int = 8          # 最少 8 位
    PASSWORD_REQUIRE_LETTER: bool = True  # 必须包含字母
    PASSWORD_REQUIRE_DIGIT: bool = True   # 必须包含数字

    # ── 安全策略（防暴力破解 / 限流）──
    # 验证码获取限流：同一 IP 每分钟最多 N 次
    CAPTCHA_RATE_LIMIT_PER_MINUTE: int = 10
    # 验证码连续错误锁定：连续输错 N 次锁定该 IP
    CAPTCHA_MAX_FAIL_COUNT: int = 5
    CAPTCHA_LOCK_MINUTES: int = 30
    # 登录失败锁定：单账号连续失败 N 次锁定
    LOGIN_MAX_FAIL_COUNT: int = 5
    LOGIN_LOCK_MINUTES: int = 15
    # 全局认证限流：单 IP 每分钟最多 N 次认证请求（注册+登录合计）
    AUTH_RATE_LIMIT_PER_MINUTE: int = 20

    # ── 数据库 ──
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/lumi_db"
    DATABASE_ADMIN_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"

    # ── Redis ──
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Celery ──
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    # Redis broker 至少一次投递：必须长于 Celery 硬超时，避免仍在执行的
    # 文档任务因 visibility timeout 被第二个 worker 重复领取。
    CELERY_TASK_TIME_LIMIT_SECONDS: int = 30 * 60
    CELERY_TASK_SOFT_TIME_LIMIT_SECONDS: int = 25 * 60
    CELERY_REDIS_VISIBILITY_TIMEOUT_SECONDS: int = 45 * 60
    CELERY_DOCUMENT_STALE_AFTER_SECONDS: int = 50 * 60

    # ── LLM: 千问 (Qwen) ──
    QWEN_API_KEY: str = ""
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_MODEL: str = "qwen-turbo"
    QWEN_VL_MODEL: str = "qwen-vl-plus"  # 多模态模型（普通聊天场景默认）
    QWEN_TURBO_MODEL: str = "qwen-turbo"  # 对话摘要等轻量任务

    # ── LLM: DeepSeek ──
    DEEPSEEK_API_KEY: str = ""
    # DeepSeek V4 官方 Chat Completions 客户端以根地址为 base_url，SDK 会自行
    # 拼接 ``/chat/completions``；不能沿用部分兼容网关的 ``/v1`` 前缀。
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com"
    # 办公/通用文本默认模型。V4 Flash / Pro 使用同一官方接口和密钥。
    DEEPSEEK_MODEL: str = "deepseek-v4-flash"
    # ── LLM: DS Flash（普通模式"快速"档 + 语音通话回复模型；支持 1M 上下文）──
    # 云端调用，省钱省时；空值自动复用 DEEPSEEK_* 配置
    DS_FLASH_BASE_URL: str = ""
    DS_FLASH_API_KEY: str = ""
    DS_FLASH_MODEL: str = ""       # 空则复用 DEEPSEEK_MODEL；如 deepseek-flash 等
    DS_FLASH_TIMEOUT: int = 180
    # ── LLM: 本地多模态描述（图片 → 文本）：qwen2.5vl:7b（Ollama）──
    # 快速/思考档主模型均为纯文本模型，图片先由本地 VL 模型描述成文字再发给主模型
    VL_BASE_URL: str = "http://localhost:11434/v1"
    VL_MODEL: str = "qwen2.5vl:7b"
    VL_API_KEY: str = "ollama"     # Ollama 不校验密钥，占位
    VL_TIMEOUT: int = 120
    # ── LLM: 普通模式"思考"档（强模型，价格适中）──
    # 默认 qwen-plus：与现有千问密钥共用，性价比高、中文对话与推理能力强
    CHAT_THINK_MODEL: str = ""  # 空则复用 QWEN_MODEL
    CHAT_THINK_BASE_URL: str = ""  # 空则复用 QWEN_BASE_URL
    CHAT_THINK_API_KEY: str = ""   # 空则复用 QWEN_API_KEY

    # ── LLM 默认选用 ──
    # 纯文本默认走 DeepSeek V4 Flash；本地 Ollama 的 qwen2.5vl 仅用于图像转文字。
    LLM_PROVIDER: str = "deepseek"  # qwen / deepseek
    LLM_FALLBACK_PROVIDER: str = ""  # 主供应商失败时自动切换（deepseek/qwen；空=不降级）
    # Python/OpenAI 客户端使用的显式 HTTP(S) 代理；为空时沿用系统环境变量。
    # 浏览器能访问而后端进程不能访问时，可填 http://127.0.0.1:端口。
    LLM_HTTP_PROXY: str = ""
    # 仅向明确验证过的 OpenAI-compatible 模型透传 reasoning_effort。
    # 多数兼容网关会拒绝未知字段；默认留空，格式为逗号分隔的模型 ID。
    LLM_REASONING_EFFORT_MODELS: str = ""

    # ── 嵌入模型（本地推理，sentence-transformers）──
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIMENSION: int = 1024  # bge-m3=1024（已从 bge-small-zh 迁移）
    EMBEDDING_BATCH_SIZE: int = 16
    EMBEDDING_DEVICE: str = "cpu"   # cpu / cuda
    EMBEDDING_CACHE_DIR: str = ""   # 模型缓存目录；为空用 HuggingFace 默认缓存
    # Hugging Face Hub 的下载端点由 Dockerfile/Compose 在进程启动前注入；
    # 同时在 Settings 声明，避免本地测试读取 .env 时被 Pydantic 视为未知配置。
    HF_ENDPOINT: str = ""
    HF_HUB_DISABLE_XET: bool = True
    # 检索指令前缀。bge 官方建议查询时附加，但本项目实测不加区分度更好，
    # 默认关闭；切换 bge-m3 后可重新开启对比效果。
    EMBEDDING_QUERY_INSTRUCTION: str = "为这个句子生成表示以用于检索相关文章："

    # ── 文件上传 ──
    UPLOAD_DIR: str = "data/uploads"
    UPLOAD_TOKEN_TTL_SECONDS: int = 3600  # 附件签名 URL 有效期（秒）

    # ── 办公文档临时会话（聊天框上传链路，短期保留） ──
    # TTL（小时）：会话被读取/分析/编辑时刷新；过期后由清理任务删除。
    # 前端另有"轮次上限"（OFFICE_DOC_MAX_ROUNDS），双重保险防会话无限堆积。
    OFFICE_SESSION_TTL_HOURS: int = 24
    # 办公文档分析：全文 ≤ 该字符数时直接注入 LLM（不依赖 RAG 阈值，小文档更可靠）
    OFFICE_DOC_FULL_TEXT_LIMIT: int = 20000

    # ── Docling 文档解析（PDF / Office / 图片等）──
    DOCLING_ENABLE_OCR: bool = True      # 扫描件/图片 OCR（依赖 RapidOCR，首次使用自动下载模型）
    DOCLING_OCR_ENGINE: str = "rapidocr" # rapidocr / easyocr / tesseract

    # ── 搜索工具: Tavily ──
    TAVILY_API_KEY: str = ""
    TAVILY_MAX_RESULTS: int = 5
    TAVILY_SEARCH_DEPTH: str = "basic"  # basic / advanced
    TAVILY_TIMEOUT_SECONDS: int = 15
    WEB_SEARCH_TOOL_ENABLED: bool = True  # 模型自主决策是否联网（总开关）

    # ── 角色提示词（可插拔：app/prompts/*.md，frontmatter 定义元信息）──
    PROMPTS_DIR: str = "app/prompts"

    # ── RAG 默认参数 ──
    RAG_TOP_K: int = 5
    RAG_SIMILARITY_THRESHOLD: float = 0.5  # 相关性硬门槛：低于此相似度的引用直接丢弃（宁缺毋滥）
    # 个人 RAG 检索是否包含公共空间内容（False=只检索自己的文件；
    # 公共空间仍可通过公共知识库接口显式检索，避免他人上传的内容混入个人检索）
    RAG_INCLUDE_PUBLIC_IN_PERSONAL: bool = False
    RAG_CHUNK_SIZE: int = 500
    RAG_CHUNK_OVERLAP: int = 50
    RAG_MIN_QUALITY_SCORE: float = 0.5  # 清洗后质量分低于该值不入库（status=error）
    RAG_HYBRID_VECTOR_TOP_K: int = 10   # 混合检索：向量相似度路召回数
    RAG_HYBRID_KEYWORD_TOP_K: int = 10  # 混合检索：关键词路召回数
    # ── 检索时效性（知识有生命周期，新文档应占一定优势）──
    RAG_RECENCY_WEIGHT: float = 0.3         # 时效性权重（0~1），与相关性加权
    RAG_RECENCY_QUERY_WEIGHT: float = 0.6   # 查询含时间意图（"最新/最近"等）时的时效性权重
    RAG_RECENCY_HALF_LIFE_DAYS: int = 90    # 时效性半衰期（天）：越新权重越高
    RAG_TIME_FILTER_DAYS: int | None = None # 可选硬过滤：只检索最近 N 天的文档（None=不过滤）
    # 思考档/复杂办公检索可选 cross-encoder 重排；默认关闭，避免快速路径加载模型和增加延迟。
    RAG_RERANK_ENABLED: bool = False
    RAG_RERANK_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RAG_RERANK_TOP_K: int = 20
    RAG_RERANK_FINAL_K: int = 5
    RAG_RERANK_DEVICE: str = "cpu"
    # BGE-M3 sparse 仅用于离线评测阶段；评测通过后再设计持久化/在线召回。
    RAG_SPARSE_EXPERIMENT_ENABLED: bool = False
    RAG_SPARSE_MODEL: str = "BAAI/bge-m3"
    RAG_SPARSE_DEVICE: str = "cpu"

    # ── 服务端查询扩写（思考档/办公检索；快速档不做任何前置 LLM 调用）──
    # 原 query 始终保留，扩写仅作为第二个召回来源；显式文件名/编号/日期禁用扩写。
    RAG_QUERY_REWRITE_ENABLED: bool = True
    RAG_QUERY_REWRITE_PROVIDER: str = "cloud"  # cloud（默认，qwen-turbo）/ local（服务端本地小模型插槽）
    RAG_QUERY_REWRITE_BASE_URL: str = "http://localhost:11434/v1"  # local 插槽：OpenAI 兼容端点（Ollama 等）
    RAG_QUERY_REWRITE_MODEL: str = ""          # local 插槽：模型名；配置后自动走本地
    RAG_QUERY_REWRITE_TIMEOUT_SECONDS: int = 15
    RAG_QUERY_REWRITE_MAX_VARIANTS: int = 2

    # ── 智能体技能与沙箱 ──
    AGENT_SKILLS_ENABLED: bool = True    # 技能调用开关（LLM 可请求调用技能；快速/思考档模型均支持 function calling）
    # docker 是生产默认值；Docker 未部署时 python_exec 会在能力目录中被隐藏。
    AGENT_SANDBOX_TYPE: str = "docker"   # 沙箱类型：local / docker / wasm（预留）
    # local 仅适合受信开发环境，并非安全边界；生产必须接入容器/WASM 沙箱。
    AGENT_ALLOW_UNSAFE_LOCAL_SANDBOX: bool = False
    AGENT_SANDBOX_TIMEOUT_SECONDS: int = 30
    AGENT_SANDBOX_MAX_OUTPUT_CHARS: int = 8000
    AGENT_SANDBOX_MAX_CODE_CHARS: int = 24000
    # 防止单次脚本把容器 tmpfs 当作大文件转储通道；文件仍须由技能层显式声明交付。
    AGENT_SANDBOX_MAX_ARTIFACT_FILES: int = 128
    AGENT_SANDBOX_MAX_ARTIFACT_BYTES: int = 32 * 1024 * 1024
    AGENT_SANDBOX_DOCKER_BINARY: str = "docker"
    AGENT_SANDBOX_DOCKER_IMAGE: str = "lumi-python-sandbox:latest"
    AGENT_SANDBOX_DOCKER_MEMORY: str = "512m"
    AGENT_SANDBOX_DOCKER_CPUS: float = 0.5
    AGENT_SANDBOX_DOCKER_PIDS_LIMIT: int = 64
    # 与 API 容器 appuser UID/GID 一致，确保唯一可写的输出 bind mount 不需 root。
    AGENT_SANDBOX_DOCKER_UID: int = 1000
    AGENT_SANDBOX_DOCKER_GID: int = 1000
    AGENT_SKILLS_MAX_ROUNDS: int = 5     # 技能调用循环最大轮数（防死循环）
    AGENT_CLIENT_TOOL_TIMEOUT_SECONDS: int = 45  # 客户端工具等待用户执行/确认的最长时间（过长会让任务卡在思维链）
    AGENT_REVIEW_ENABLED: bool = False   # activity 级质检开关：与 writer 自检 + reviewer 节点重复，
                                         # 默认关闭省一次 LLM 调用/节点；需要可改回 True
    SKILL_PLUGINS_DIR: str = "plugins/skills"     # 技能插件目录（Docker 挂载为 volume 支持热更新）
    # 合法 Skill 池内的向量排序；权限/场景过滤永远在它之前。语义索引在启动时
    # 预热；未就绪或嵌入异常时会以 ``lexical_fallback`` 显式记录，而不是静默混用。
    SKILL_SEMANTIC_ROUTING_ENABLED: bool = True
    SKILL_SEMANTIC_ROUTING_STARTUP_WARMUP: bool = True
    SKILL_ROUTING_SEMANTIC_WEIGHT: float = 35.0
    SKILL_ROUTING_RELIABILITY_WEIGHT: float = 12.0
    SKILL_ROUTING_COST_WEIGHT: float = 3.0
    # Candidate-routing observability: below this score a tool-intent request
    # is considered a possible recall miss and emits a monitor event.
    SKILL_CANDIDATE_LOW_CONFIDENCE_SCORE: float = 20.0
    # L2 混合分数的歧义门槛；分数差小于该值时视为近似并列，不能仅凭首名放行。
    SKILL_CANDIDATE_MARGIN_THRESHOLD: float = 3.0
    # Top-K 最后一名与下一名分数接近时允许有限溢出，降低措辞微调造成的 5/6 名跳变。
    SKILL_CANDIDATE_TIE_EPSILON: float = 3.0
    SKILL_CANDIDATE_MAX_OVERFLOW: int = 1
    SKILL_BOOTSTRAP_EXPIRING_DAYS: int = 3
    # 用户显式绑定的外部 MCP 走独立配额，避免其可用性或成本拖垮内置 Skill。
    # 部署可在 MCP_SERVERS 的单个 server 配置中用 mcp_daily_call_limit /
    # mcp_concurrency_limit 覆盖这些默认值。
    MCP_EXTERNAL_DEFAULT_DAILY_CALL_LIMIT: int = 100
    MCP_EXTERNAL_DEFAULT_CONCURRENCY_LIMIT: int = 2
    # 只有在部署方明确打开后，外部 MCP 用户绑定才需要管理员二次批准；默认保留
    # 用户显式绑定即可使用的低摩擦模式。
    MCP_EXTERNAL_REQUIRE_ADMIN_APPROVAL: bool = False
    SKILL_TELEMETRY_LOOKBACK_DAYS: int = 30
    SKILL_TELEMETRY_MIN_SAMPLES: int = 10
    # ── 多智能体协作编排 ──
    AGENT_JOBS_TTL_SECONDS: int = 86400           # 任务状态保留时间（24h，Redis appendonly 持久化）
    AGENT_NODE_CONCURRENCY: int = 2               # 同时执行的 DAG 节点数（资源协调上限）
    AGENT_NODE_MAX_RETRIES: int = 1               # 单节点失败最大重试次数（React 重试）
    AGENT_NODE_TIMEOUT_SECONDS: int = 120         # 单节点执行超时；避免一次失败拖成数分钟
    # 0 表示回退到 AGENT_NODE_TIMEOUT_SECONDS；可按通道收紧/放宽硬超时。
    AGENT_NODE_TIMEOUT_DIRECT_LLM_SECONDS: int = 0
    AGENT_NODE_TIMEOUT_SCRIPT_SECONDS: int = 0
    AGENT_NODE_TIMEOUT_RAG_SECONDS: int = 0
    AGENT_NODE_TIMEOUT_AGENT_SECONDS: int = 0
    # 可选 JSON，例如 {"send_email":30,"office_doc_read":45}。
    AGENT_NODE_TOOL_TIMEOUTS_JSON: str = "{}"
    # 写资源必须由 Redis 证明跨进程所有权；Redis 不可用时任务进入
    # waiting_resources，绝不退化为仅当前 API 进程可见的锁。
    AGENT_WRITE_RESOURCE_FAIL_CLOSED: bool = True
    # A write job waiting for Redis coordination is suspended after this
    # interval. Suspended/approval jobs do not consume active-job capacity.
    AGENT_WAITING_RESOURCES_TIMEOUT_SECONDS: int = 1800
    # Must exceed the longest node timeout plus its lease buffer; only then is
    # an intent-only row considered orphaned after a process crash.
    AGENT_EFFECT_INTENT_RECOVERY_GRACE_SECONDS: int = 900
    AGENT_APPROVAL_TIMEOUT_SECONDS: int = 86400
    # Result bodies used by fork/replay outlive the short job snapshot TTL.
    AGENT_RESULT_REF_TTL_SECONDS: int = 604800
    # 办公规划是控制面，只需产出短 JSON。收紧预算和超时，避免简单指令被模型推理占满。
    AGENT_PLANNER_MAX_TOKENS: int = 2048
    AGENT_PLANNER_TIMEOUT_SECONDS: int = 45
    AGENT_FINAL_ANSWER_MAX_TOKENS: int = 2500
    AGENT_MANIFEST_SUMMARY_MAX_TOKENS: int = 1200
    # 清单在执行前的保守 token 预算。超过时不启动，要求用户确认/拆分，
    # 防止一次上千项任务把模型与沙箱队列拖入雪崩。
    AGENT_MANIFEST_TOKEN_BUDGET: int = 80000
    # A complex ordinary task uses an external logical plan and materializes
    # only its ready frontier.  Small tasks keep the lower-overhead full DAG.
    AGENT_LOGICAL_PLAN_ENABLED: bool = True
    AGENT_LOGICAL_PLAN_MIN_NODES: int = 3
    AGENT_LOGICAL_PLAN_FRONTIER_SIZE: int = 4
    AGENT_LOGICAL_PLAN_TOKEN_BUDGET: int = 80000
    # 单次普通 DAG 的编译窗口上限；超过时转为逻辑计划/清单，不截断用户动作。
    AGENT_PLAN_MAX_NODES: int = 6
    # L3 needs a stateful graph that can mount a validated replacement
    # subgraph. The default remains the persisted DAG runtime until the
    # Temporal manifest workflow has completed its local rollout.
    AGENT_DYNAMIC_SUBGRAPH_ENABLED: bool = True
    AGENT_SUBGRAPH_MAX_REPLANS: int = 2
    # 通道级全局并发上限：真正的外部执行由 B/D 受控；A 通常在服务层直出。
    AGENT_CHANNEL_DIRECT_LLM_CONCURRENCY: int = 32
    AGENT_CHANNEL_SCRIPT_CONCURRENCY: int = 20
    AGENT_CHANNEL_RAG_CONCURRENCY: int = 12
    AGENT_CHANNEL_AGENT_CONCURRENCY: int = 2
    # 办公任务准入背压（Redis 原子集合，跨 API worker 共享）。它们是准入上限，
    # 不是内存排队：达到上限立即返回 429，让 Temporal/legacy 执行器自然消化任务。
    AGENT_GLOBAL_ACTIVE_JOB_LIMIT: int = 32
    AGENT_USER_ACTIVE_JOB_LIMIT: int = 2
    AGENT_SUBMISSION_MAX_INFLIGHT: int = 8
    AGENT_ADMISSION_LEASE_SECONDS: int = 7200
    # ── Temporal 编排（多智能体任务执行引擎）──
    # legacy remains the default. ``temporal`` moves only static read-only
    # DAGs to the external worker; dynamic/ReAct/write paths remain legacy.
    # ``manifest_temporal`` is the rolling task-manifest runtime.
    AGENT_ORCHESTRATION: str = "legacy"           # legacy / temporal / manifest_temporal
    # Frozen read-only DAGs have a separate Temporal limit. It does not alter
    # the generic planning window or cause dynamic/rolling plans to migrate.
    TEMPORAL_STATIC_MAX_NODES: int = 12
    # 超过普通静态窗口的 DAG 只允许进入长 DAG 专用路径：必须完全只读，
    # 以 Child Workflow 调度节点并周期性 Continue-As-New 控制 History。
    # 服务级演练通过前默认关闭；开启后仍只接纳资格策略明确判定的纯读 DAG。
    TEMPORAL_STATIC_LONG_DAG_ENABLED: bool = False
    TEMPORAL_STATIC_LONG_DAG_MAX_NODES: int = 64
    TEMPORAL_STATIC_CONTINUE_AS_NEW_AFTER_NODES: int = 20
    TEMPORAL_STATIC_CHILD_WORKFLOW_ENABLED: bool = True
    # 静态 Temporal 灰度：用户白名单非空时优先于比例；类型使用 agent 名或
    # route_channel，逗号分隔。默认 100 保持 temporal 模式的既有全量语义。
    TEMPORAL_STATIC_ALLOWLIST: str = ""
    TEMPORAL_STATIC_PERCENTAGE: int = 100
    TEMPORAL_STATIC_TASK_TYPES: str = ""
    TEMPORAL_STATIC_MAX_REPLANS: int = 1
    # 滚动逻辑计划另走独立纯读 Runtime；默认关闭，避免尚未服务级验收的
    # Redis 前沿推进改变现有 Legacy 语义。
    TEMPORAL_LOGICAL_READ_ENABLED: bool = False
    TEMPORAL_LOGICAL_READ_ALLOWLIST: str = ""
    TEMPORAL_LOGICAL_READ_PERCENTAGE: int = 0
    TEMPORAL_LOGICAL_READ_TASK_TYPES: str = ""
    TEMPORAL_LOGICAL_READ_TASK_QUEUE: str = "lumi-logical-read"
    TEMPORAL_LOGICAL_READ_CONTINUE_AFTER_FRONTIERS: int = 20
    TEMPORAL_LOGICAL_READ_MAX_REPLANS: int = 1
    # 已审批副作用逻辑计划使用另一条 Runtime；默认关闭，且禁止自动重规划。
    TEMPORAL_LOGICAL_EFFECTS_ENABLED: bool = False
    TEMPORAL_LOGICAL_EFFECTS_ALLOWLIST: str = ""
    TEMPORAL_LOGICAL_EFFECTS_PERCENTAGE: int = 0
    TEMPORAL_LOGICAL_EFFECTS_TASK_TYPES: str = ""
    TEMPORAL_LOGICAL_EFFECTS_TASK_QUEUE: str = "lumi-logical-effects"
    TEMPORAL_LOGICAL_EFFECTS_CONTINUE_AFTER_FRONTIERS: int = 20
    # 策略文件按部署版本加载；shadow 仅计算并审计差异，不影响当前路由。
    AGENT_ROUTING_POLICY_MODE: str = "shadow"     # legacy / shadow / enforce
    AGENT_ROUTING_POLICY_PATH: str = "config/agent_policies/routing_rules.yaml"
    # TCA policy can tune only bounded numeric weights/thresholds; patterns
    # and execution decisions remain code and do not become YAML expressions.
    AGENT_TCA_POLICY_PATH: str = "config/agent_policies/tca_rules.yaml"
    # Vocabulary is deployment data with a fixed, schema-validated action and
    # object set; matching semantics and safety checks stay in application code.
    AGENT_ROUTING_LEXICON_PATH: str = "config/agent_policies/routing_lexicon.yaml"
    AGENT_EXECUTION_DEFAULTS_PATH: str = "config/agent_policies/execution_defaults.yaml"
    AGENT_ROUTING_INTENT_PATTERN_PATH: str = "config/agent_policies/route_intent_patterns.yaml"
    AGENT_PLANNING_POLICY_PATH: str = "config/agent_policies/planning_rules.yaml"
    TEMPORAL_ADDRESS: str = "localhost:7233"      # Temporal 前端 gRPC 地址
    TEMPORAL_NAMESPACE: str = "default"           # Temporal namespace
    TEMPORAL_TASK_QUEUE: str = "lumi-agents"      # Temporal 任务队列（worker 与客户端必须一致）
    TEMPORAL_MANIFEST_TASK_QUEUE: str = "lumi-office-manifest"
    TEMPORAL_MANIFEST_CONTINUE_AS_NEW_BATCHES: int = 40
    TEMPORAL_ACTIVITY_HEARTBEAT_SECONDS: int = 15
    TEMPORAL_BYOK_TTL_SECONDS: int = 43200        # BYOK key 临时存放 TTL（12h；任务正常结束即删除）
    # 自定义 BYOK endpoint 默认只允许公网 http(s) 地址，避免云端部署被用作 SSRF。
    # 仅桌面/受信任自托管场景需要 Ollama/LM Studio 时再显式开启。
    BYOK_ALLOW_PRIVATE_BASE_URL: bool = False
    TEMPORAL_RUN_WORKER_INPROCESS: bool = True    # 后端进程内运行 Temporal Worker（IDE 一键运行后端即含 Worker）
    TEMPORAL_AUTO_START_SERVER: bool = True       # Worker 启动前自动拉起 Temporal 开发服务器（找不到 exe 则跳过）
    # 代码生成允许的最高推理档（渐进式：起始恒为 low，空内容/自检不过时自动升级到该档）。
    # 想强制全部 low 可设为 low；想允许复杂任务用高推理则保持 high。
    # 规划/审查/自检/标题等非产出型调用固定 low，不受此限制。
    AGENT_LLM_REASONING_EFFORT: str = "high"
    # 代码生成默认开启推理（先分析后写）：配合高输出上限避免"预算烧光返回空内容"
    AGENT_CODE_NO_REASONING: bool = False
    # 代码生成单次输出上限（取消小预算：推理强度高时几十万 token 属正常现象）
    AGENT_CODE_MAX_TOKENS: int = 65536
    # 限制用户指令/反馈输入长度（减少阅读与思考的 token 消耗）
    AGENT_CODE_MAX_INSTRUCTION_CHARS: int = 4000
    # 屏蔽的执行 agent（插件化注册时按 name 过滤）：写代码 agent 暂不使用，
    # 保留代码便于日后恢复（清空列表即可）
    AGENT_DISABLED: list[str] = [
        "code",
        "code_reader",
        "code_writer",
        "code_tester",
        "code_reviewer",
    ]
    # 渐进开放写工具：False 时向 LLM 隐藏写操作技能（只读先行，发消息/改文件/装依赖等）
    AGENT_TOOL_WRITE_ENABLED: bool = True
    # 混合架构：客户端技能通过 MCP 调用（可插拔）。配置：
    # [{"name": "lumi_client", "transport": "streamable-http", "url": "http://127.0.0.1:8765/mcp"}]
    MCP_SERVERS: list[dict] = []
    # MCP 客户端发现缓存；工具 schema 变化时由客户端重启或显式刷新。
    MCP_TOOLS_CACHE_TTL_S: float = 30.0
    MCP_SESSION_IDLE_TIMEOUT_S: float = 600.0
    MCP_MAX_SESSIONS_PER_SERVER: int = 8
    MCP_TOOL_TIMEOUT_S: float = 180.0

    # ── 文档类别与按类别半衰期（不同知识时效性不同）──
    RAG_DEFAULT_CATEGORY: str = "general"   # 默认类别
    RAG_CATEGORY_HALF_LIFE_DAYS: dict[str, int] = {
        "news": 14,      # 新闻：衰减快
        "general": 180,  # 通用/技术文档
        "history": 3650, # 历史：长期有效（10 年）
        "other": 365,
    }

    # ── 会话 ──
    # 0 表示不按轮次数裁剪 Redis 热窗口。陪伴型对话中短句很多，热窗口必须
    # 以 token 而不是轮次数为准；达到阈值后由后台滑动淘汰。
    CONVERSATION_CONTEXT_ROUNDS: int = 0
    # 普通模式每次请求仅注入最近的注意力质量窗口，并不发送整个热窗口。
    LLM_HISTORY_MAX_TOKENS: int = 12000
    # 办公模式短期记忆：只保证当次任务连贯，不需要长窗口
    LLM_HISTORY_MAX_TOKENS_WORK: int = 60000
    # 普通聊天热窗口：达到 25 万 token 后，摘要并物理淘汰最早部分，
    # 保留最近约 15 万 token 的服务端原文。客户端本地历史独立留存。
    CONVERSATION_SUMMARY_TRIGGER_TOKENS: int = 250000
    CONVERSATION_SUMMARY_KEEP_TOKENS: int = 150000
    # 仅兼容既有 .env；token 滑动窗口不再使用轮次数上限。
    CONVERSATION_SUMMARY_KEEP_ROUNDS: int = 0
    CONVERSATION_SUMMARY_CHUNK_TOKENS: int = 30000
    CONVERSATION_SUMMARY_MAX_CHARS: int = 20000
    CONVERSATION_SEGMENT_ROUNDS: int = 8
    CONVERSATION_GLOBAL_SUMMARY_MAX_CHARS: int = 1200
    CONVERSATION_SEGMENT_SUMMARY_MAX_CHARS: int = 600
    CONVERSATION_RECALL_SEGMENTS_FAST: int = 2
    CONVERSATION_RECALL_SEGMENTS_THINK: int = 4
    CONVERSATION_RECALL_RAW_MESSAGES_FAST: int = 0
    CONVERSATION_RECALL_RAW_MESSAGES_THINK: int = 4
    # ── 后端生成文件清理 ──
    GENERATED_FILES_TTL_DAYS: int = 7    # 通用脚本产物（office_outputs）保留天数，到期定时删除
    SANDBOX_TEMP_TTL_HOURS: int = 6      # 沙箱残留临时目录（lumi_sandbox_*）兜底清理时长

    # ── 语音（ASR + TTS）──
    WHISPER_MODEL: str = "base"        # openai-whisper：tiny/base/small/medium/large
    WHISPER_LANGUAGE: str = "zh"       # 转写语言；留空自动检测
    ASR_CORRECT_ENABLED: bool = True   # 转写后用 qwen-turbo 纠错（口音/同音字）
    TTS_ENABLED: bool = True           # AI 回复自动转语音（异步，可中断）
    TTS_PROVIDER: str = "dashscope"    # local_qwen3（本地/局域网 qwen3-tts）/ dashscope（千问 cosyvoice）/ edge
    TTS_MODEL: str = "cosyvoice-v1"    # 千问 TTS 模型（Dashscope 实际可用名）
    TTS_VOICE: str = "Cherry"          # 千问音色：Cherry / Serena / Ethan 等
    TTS_EDGE_VOICE: str = "zh-CN-XiaoxiaoNeural"  # edge-tts 兜底音色
    TTS_LOCAL_URL: str = "http://localhost:8765/tts"  # 本地 qwen3-tts 服务地址
    TTS_LOCAL_TIMEOUT: int = 180
    TTS_FORMAT: str = "mp3"
    TTS_SAMPLE_RATE: int = 24000

    # ── 长期记忆 ──
    MEMORY_ENCRYPTION_KEY: str = ""          # base64 编码的 32 字节主密钥；缺失时记忆加密不可用
    MEMORY_ENCRYPTION_KEY_VERSION: int = 1   # 密钥版本，轮换时 +1 并触发全量重加密

    # ── Docker secret 注入目录（容器内 /run/secrets；非 Docker 环境不存在则跳过） ──
    SECRETS_DIR: str = "/run/secrets"
    MEMORY_EXTRACTION_MODEL: str = "qwen-turbo"  # 记忆抽取/合并/画像聚合用轻量模型
    MEMORY_EXTRACTION_MIN_CONFIDENCE: float = 0.6  # 低于该置信度的事实不落库
    MEMORY_FACT_TOP_K: int = 5               # 每轮对话注入的记忆事实上限
    MEMORY_FACT_MIN_VECTOR_SIMILARITY: float = 0.75  # 独立记忆库的高精度注入门槛
    MEMORY_HYBRID_VECTOR_TOP_K: int = 10     # 记忆向量路召回数
    # 记忆默认只走向量检索，避免短词/人名 ILIKE 误命中。仅在完成评测后才可开启。
    MEMORY_FACT_KEYWORD_FALLBACK_ENABLED: bool = False
    MEMORY_HYBRID_KEYWORD_TOP_K: int = 10    # 启用关键词兜底时的候选数
    MEMORY_SIMILARITY_THRESHOLD: float = 0.72  # 去重/矛盾判定阈值
    MEMORY_CLEANUP_THRESHOLD: float = 0.3    # 过期且重要度低于该值 → 物理删除
    MEMORY_HALF_LIFE_DAYS: dict[str, int | None] = {
        "identity": None,     # 不过期，仅在被矛盾事实取代时失效
        "preference": 90,
        "experience": 45,
        "goal": 180,
    }
    MEMORY_PROFILE_BUILD_INTERVAL_HOURS: int = 24  # 画像重建间隔（小时）
    MEMORY_PROFILE_INJECT_ENABLED: bool = True     # 画像常驻注入开关
    MEMORY_EXTRACTION_MIN_MESSAGES: int = 5        # 对话攒满 N 条消息触发一次抽取（摘要路径之外；首条消息立即抽取）
    MEMORY_EXTRACTION_MAX_DIALOG_CHARS: int = 20000  # 单次抽取的对话文本上限（字符）
    MEMORY_EXTRACTION_MAX_TOKENS: int = 2048       # 抽取 LLM 输出 token 上限
    MEMORY_DECRYPT_ENABLED: bool = True            # L1 解密门总开关
    MEMORY_DECRYPT_LLM_CONFIRM_ENABLED: bool = True  # 关键词预筛后 LLM 二次确认

    # ── 聊天记录生命周期（旧的条数安全阀）──
    # token 热窗口负责正常淘汰；以下 0 值禁用旧的 UI 条数裁剪，避免短句聊天
    # 在达到 25 万 token 前被提前删除。
    CONVERSATION_MESSAGE_KEEP: int = 0
    CONVERSATION_MESSAGE_HARD_CAP: int = 0

    model_config = {
        # 固定从项目根加载 .env，避免 API/worker 从不同目录启动时读不到配置
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
    }

    @field_validator(
        "UPLOAD_DIR",
        "EMBEDDING_CACHE_DIR",
        "PROMPTS_DIR",
        "SKILL_PLUGINS_DIR",
        "AGENT_ROUTING_POLICY_PATH",
        "AGENT_TCA_POLICY_PATH",
        "AGENT_ROUTING_LEXICON_PATH",
        "AGENT_EXECUTION_DEFAULTS_PATH",
        "AGENT_ROUTING_INTENT_PATTERN_PATH",
        "AGENT_PLANNING_POLICY_PATH",
        mode="after",
    )
    @classmethod
    def _resolve_relative_paths(cls, value: str) -> str:
        """相对路径统一基于项目根解析为绝对路径.

        防止 API（uvicorn）与 Celery worker 工作目录不一致时，
        上传文件/模型缓存/提示词等相对路径各自解析到不同位置。
        """
        if value and not Path(value).is_absolute():
            return str(PROJECT_ROOT / value)
        return value

    # 支持从 Docker secret 文件注入的敏感字段（环境变量仍为默认来源，文件优先覆盖）
    _SECRET_FILE_FIELDS = (
        "JWT_SECRET_KEY",
        "MEMORY_ENCRYPTION_KEY",
        "DEEPSEEK_API_KEY",
        "QWEN_API_KEY",
        "TAVILY_API_KEY",
    )

    @model_validator(mode="after")
    def _load_docker_secrets(self):
        """Docker secret 注入：<SECRETS_DIR>/<字段名> 文件存在时用文件内容覆盖."""
        secrets_dir = Path(getattr(self, "SECRETS_DIR", "/run/secrets"))
        for field in self._SECRET_FILE_FIELDS:
            try:
                content = (secrets_dir / field).read_text(encoding="utf-8").strip()
                if content:
                    setattr(self, field, content)
            except Exception:  # noqa: BLE001
                pass
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
