# ============================================================
# 全局配置
#
# 所有外部依赖的连接信息、模型密钥统一从这里读，来源是 .env。
# ★ 绝不在业务代码里硬编码密钥
#
# 用法：
#   from src.core.config import get_settings
#   settings = get_settings()        # lru_cache，全进程只解析一次 .env
# ============================================================

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---------------- 应用 ----------------
    APP_NAME: str = "rd-chatBI"
    APP_ENV: str = "dev"
    # 生产默认关 debug：debug=True 会改变 FastAPI 错误页行为
    APP_DEBUG: bool = False

    # CORS 允许来源，逗号分隔；"*" 时自动关闭 credentials（浏览器规范禁止二者同用）
    CORS_ORIGINS: str = "http://localhost:8003"

    # ---------------- 认证 ----------------
    # header：网关透传 X-User-* 头（开发/内网网关模式，网关必须剥离外部传入的身份头）
    # jwt：   解析 Authorization: Bearer <token>（HS256），身份只信 token claims
    AUTH_MODE: str = "header"
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"

    # ---------------- PostgreSQL（共享实例，独立库）----------------
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "rdagent"
    DB_PASSWORD: str = "rdagent123"
    DB_NAME: str = "rd_chatbi"
    # SQL 回显独立开关（echo 会把含参数的完整 SQL 打进日志，生产必须关）
    DB_ECHO: bool = False

    # ---------------- Redis Stack（会话历史 key 前缀隔离）----------------
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""
    REDIS_DB: int = 0

    # ---------------- Milvus ----------------
    MILVUS_HOST: str = "localhost"
    MILVUS_PORT: int = 19530

    # ---------------- Elasticsearch（列值召回）----------------
    ES_HOST: str = "localhost"
    ES_PORT: int = 9200

    # ---------------- 模型 ----------------
    DASHSCOPE_API_KEY: str = ""
    BASE_URL_CHAT: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    CHAT_MODEL: str = "qwen-max"
    EMBEDDING_MODEL: str = "text-embedding-v3"

    DEEPSEEK_API_KEY: str = ""
    DEEPSEEK_MODEL: str = "deepseek-chat"

    # LLM 请求超时/重试：不设上限的话 LLM 挂起会无限占用 SSE 连接与工作协程
    LLM_REQUEST_TIMEOUT: int = 60
    LLM_MAX_RETRIES: int = 1

    # ---------------- 模型定价（USD/1M tokens）----------------
    MODEL_PRICING_INPUT: float = 0.4   # qwen-max 输入 $0.4/1M
    MODEL_PRICING_OUTPUT: float = 1.2  # qwen-max 输出 $1.2/1M

    # ---------------- NL2SQL（查询 demo 医院运营库）----------------
    DEMO_DB_USER: str = "rdagent"
    DEMO_DB_PASSWORD: str = "rdagent123"
    DEMO_DB_NAME: str = "chatbi_demo"

    # ---------------- API 限流 ----------------
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_BACKEND: str = "redis"     # redis（ZSET 跨 worker）/ memory（进程内 deque）
    RATE_LIMIT_MAX_REQUESTS: int = 20     # 每个 user_id 每个滑动窗口内最多请求数
    RATE_LIMIT_WINDOW_SECONDS: int = 60   # 滑动窗口时长（秒）

    # ---------------- 对话上下文存储 ----------------
    # redis：跨 worker 共享、重启不丢（生产多实例必须用这个）；memory：进程内 dict
    CONVERSATION_BACKEND: str = "redis"
    # 会话历史 TTL（秒）。不设 TTL 的话 key 会无限堆积：header 认证模式下
    # user_id 可伪造，等于对外提供了一个无上限的写入口。
    CONVERSATION_MAX_AGE_SECONDS: int = 7 * 24 * 3600   # 7 天

    # ---------------- 数据库连接池 ----------------
    # ★ NullPool（默认）= 每请求新建连接，规避 asyncpg「连接绑定 event loop」
    #   的问题，但高并发下建连开销显著；按 event loop 隔离池化见 src/infra/pool.py
    # 单池上限（每个 event loop 各一份）
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    DB_POOL_TIMEOUT: int = 30             # 取连接等待上限（秒）
    DB_POOL_RECYCLE: int = 1800           # 连接最长存活（秒）

    # ---------------- LLM 熔断器 ----------------
    # 连续失败 N 次 → 打开熔断，冷却期直接快速失败，避免雪崩式重试把延迟和
    # 成本放大（LLM 挂起 → 每次请求都要等满 request_timeout）
    CIRCUIT_BREAKER_ENABLED: bool = True
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT: int = 30    # 秒；打开多久后放一个探针
    CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS: int = 1  # 半开态允许的并发探针数

    # ---------------- 日志 ----------------
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    AUDIT_LOG_RETENTION: str = "180 days"

    # ---------------- /metrics 保护 ----------------
    # 配置后 GET /metrics 要求 Authorization: Bearer <token>（Prometheus
    # bearer_token 原生支持）；留空 = 不鉴权（开发/内网抓取），
    # 生产环境务必配置或用网络隔离兜底
    METRICS_TOKEN: str = ""

    @property
    def DATABASE_URL(self) -> str:
        """本服务自有库 rd_chatbi（NL2SQL 元数据）"""
        return (
            f"postgresql+asyncpg://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    @property
    def DEMO_DATABASE_URL(self) -> str:
        """demo 医院运营库 chatbi_demo（NL2SQL 查询目标，只读）"""
        return (
            f"postgresql+asyncpg://{self.DEMO_DB_USER}:{self.DEMO_DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DEMO_DB_NAME}"
        )

    @property
    def REDIS_URL(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()
