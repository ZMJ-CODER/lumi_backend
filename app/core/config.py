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

    # ── 会话 ──
    CONVERSATION_CONTEXT_ROUNDS: int = 10

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
