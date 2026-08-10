"""应用配置，基于 pydantic-settings 从环境变量 / .env 加载."""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── 基础 ──
    PROJECT_NAME: str = "Lumi Backend"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

    # ── CORS ──
    CORS_ORIGINS: list[str] = ["*"]

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

    # ── LLM: 千问 (Qwen) ──
    QWEN_API_KEY: str = ""
    QWEN_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    QWEN_MODEL: str = "qwen-plus"
    QWEN_VL_MODEL: str = "qwen-vl-plus"  # 多模态模型（普通聊天场景默认）
    QWEN_TURBO_MODEL: str = "qwen-turbo"  # 对话摘要等轻量任务

    # ── LLM: DeepSeek ──
    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_BASE_URL: str = "https://api.deepseek.com/v1"
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # ── LLM 默认选用 ──
    LLM_PROVIDER: str = "deepseek"  # qwen / deepseek

    # ── 嵌入模型（本地推理，sentence-transformers）──
    EMBEDDING_MODEL: str = "BAAI/bge-small-zh-v1.5"
    EMBEDDING_DIMENSION: int = 512  # bge-small-zh-v1.5=512；切换 bge-m3 时改为 1024 并迁移数据库向量列
    EMBEDDING_BATCH_SIZE: int = 16
    EMBEDDING_DEVICE: str = "cpu"   # cpu / cuda
    EMBEDDING_CACHE_DIR: str = ""   # 模型缓存目录；为空用 HuggingFace 默认缓存
    # 检索指令前缀。bge 官方建议查询时附加，但本项目实测不加区分度更好，
    # 默认关闭；切换 bge-m3 后可重新开启对比效果。
    EMBEDDING_QUERY_INSTRUCTION: str = ""

    # ── 文件上传 ──
    UPLOAD_DIR: str = "data/uploads"

    # ── Docling 文档解析（PDF / Office / 图片等）──
    DOCLING_ENABLE_OCR: bool = True      # 扫描件/图片 OCR（依赖 RapidOCR，首次使用自动下载模型）
    DOCLING_OCR_ENGINE: str = "rapidocr" # rapidocr / easyocr / tesseract

    # ── 搜索工具: Tavily ──
    TAVILY_API_KEY: str = ""

    # ── RAG 默认参数 ──
    RAG_TOP_K: int = 5
    RAG_SIMILARITY_THRESHOLD: float = 0.45  # 按 bge-small-zh 实测校准；切换模型后需重新校准
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

    # ── 服务端查询重写（可插拔，默认关闭）──
    # 手机端等没有本地小模型的客户端，由服务端小模型完成提问精炼。
    # 优先级：客户端 retrieval_query > 服务端重写 > 原始 content。
    RAG_QUERY_REWRITE_ENABLED: bool = False
    RAG_QUERY_REWRITE_BASE_URL: str = "http://localhost:11434/v1"
    RAG_QUERY_REWRITE_MODEL: str = ""
    RAG_QUERY_REWRITE_TIMEOUT_SECONDS: int = 15

    # ── 智能体技能与沙箱（预留，默认关闭）──
    AGENT_SKILLS_ENABLED: bool = False   # 技能调用开关（默认关闭，开启后 LLM 可请求调用技能）
    AGENT_SANDBOX_TYPE: str = "local"    # 沙箱类型：local / docker / wasm（预留）
    AGENT_SANDBOX_TIMEOUT_SECONDS: int = 30
    AGENT_SANDBOX_MAX_OUTPUT_CHARS: int = 8000

    # ── 文档类别与按类别半衰期（不同知识时效性不同）──
    RAG_DEFAULT_CATEGORY: str = "general"   # 默认类别
    RAG_CATEGORY_HALF_LIFE_DAYS: dict[str, int] = {
        "news": 14,      # 新闻：衰减快
        "general": 180,  # 通用/技术文档
        "history": 3650, # 历史：长期有效（10 年）
        "other": 365,
    }

    # ── 会话 ──
    CONVERSATION_CONTEXT_ROUNDS: int = 200
    # 发送给模型的最近历史 token 预算（Qwen-VL-Plus 上下文 131k，给历史留 32k，
    # 其余留给 system prompt / RAG 上下文 / 输出；可按需调整）
    LLM_HISTORY_MAX_TOKENS: int = 32768
    # 对话摘要（qwen-turbo）：上下文接近发送预算时，把旧消息压缩成摘要，
    # 既省 token 又保留整段对话的记忆；触发阈值、保留轮数、摘要长度上限
    CONVERSATION_SUMMARY_TRIGGER_TOKENS: int = 24576
    CONVERSATION_SUMMARY_KEEP_ROUNDS: int = 20
    CONVERSATION_SUMMARY_MAX_CHARS: int = 2000

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
