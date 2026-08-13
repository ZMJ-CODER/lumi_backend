"""应用配置，基于 pydantic-settings 从环境变量 / .env 加载."""

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings

# 项目根目录：app/core/config.py → 上溯三级
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    # ── 基础 ──
    PROJECT_NAME: str = "Lumi Backend"
    VERSION: str = "0.1.0"
    DEBUG: bool = False

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
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIMENSION: int = 1024  # bge-m3=1024（已从 bge-small-zh 迁移）
    EMBEDDING_BATCH_SIZE: int = 16
    EMBEDDING_DEVICE: str = "cpu"   # cpu / cuda
    EMBEDDING_CACHE_DIR: str = ""   # 模型缓存目录；为空用 HuggingFace 默认缓存
    # 检索指令前缀。bge 官方建议查询时附加，但本项目实测不加区分度更好，
    # 默认关闭；切换 bge-m3 后可重新开启对比效果。
    EMBEDDING_QUERY_INSTRUCTION: str = "为这个句子生成表示以用于检索相关文章："

    # ── 文件上传 ──
    UPLOAD_DIR: str = "data/uploads"
    UPLOAD_TOKEN_TTL_SECONDS: int = 3600  # 附件签名 URL 有效期（秒）

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

    # ── 服务端查询重写（仅办公模式启用；默认云端 qwen-turbo）──
    # 优先级：客户端 retrieval_query > 服务端重写 > 原始 content。
    # 其他场景不做改写，保证回复速度；本地小模型为预留插槽（见下）。
    RAG_QUERY_REWRITE_ENABLED: bool = True
    RAG_QUERY_REWRITE_PROVIDER: str = "cloud"  # cloud（默认，qwen-turbo）/ local（服务端本地小模型插槽）
    RAG_QUERY_REWRITE_BASE_URL: str = "http://localhost:11434/v1"  # local 插槽：OpenAI 兼容端点（Ollama 等）
    RAG_QUERY_REWRITE_MODEL: str = ""          # local 插槽：模型名；配置后自动走本地
    RAG_QUERY_REWRITE_TIMEOUT_SECONDS: int = 15

    # ── 智能体技能与沙箱（预留，默认关闭）──
    AGENT_SKILLS_ENABLED: bool = False   # 技能调用开关（默认关闭，开启后 LLM 可请求调用技能）
    AGENT_SANDBOX_TYPE: str = "local"    # 沙箱类型：local / docker / wasm（预留）
    AGENT_SANDBOX_TIMEOUT_SECONDS: int = 30
    AGENT_SANDBOX_MAX_OUTPUT_CHARS: int = 8000
    AGENT_SKILLS_MAX_ROUNDS: int = 5     # 技能调用循环最大轮数（防死循环）
    AGENT_CLIENT_TOOL_TIMEOUT_SECONDS: int = 120  # 客户端工具等待用户执行/确认的最长时间
    AGENT_REVIEW_ENABLED: bool = True    # 质检层开关（code 任务 LLM 审查；失败自动放行）
    SKILL_PLUGINS_DIR: str = "plugins/skills"     # 技能插件目录（Docker 挂载为 volume 支持热更新）
    # ── 多智能体协作编排 ──
    AGENT_JOBS_TTL_SECONDS: int = 86400           # 任务状态保留时间（24h，Redis appendonly 持久化）
    AGENT_NODE_CONCURRENCY: int = 2               # 同时执行的 DAG 节点数（资源协调上限）
    AGENT_NODE_MAX_RETRIES: int = 2               # 单节点失败最大重试次数（React 重试）
    AGENT_NODE_TIMEOUT_SECONDS: int = 300         # 单节点执行超时（5 分钟，对应断网策略）

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
    MEMORY_EXTRACTION_MODEL: str = "qwen-turbo"  # 记忆抽取/合并/画像聚合用轻量模型
    MEMORY_EXTRACTION_MIN_CONFIDENCE: float = 0.6  # 低于该置信度的事实不落库
    MEMORY_FACT_TOP_K: int = 5               # 每轮对话注入的记忆事实上限
    MEMORY_HYBRID_VECTOR_TOP_K: int = 10     # 记忆混合检索：向量路召回数
    MEMORY_HYBRID_KEYWORD_TOP_K: int = 10    # 记忆混合检索：关键词路召回数
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

    # ── 聊天记录生命周期（消息上限裁剪，物理删除）──
    CONVERSATION_MESSAGE_KEEP: int = 50      # 每会话保留最近 N 条
    CONVERSATION_MESSAGE_HARD_CAP: int = 70  # 超过该条数触发异步裁剪（回到 KEEP）

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
