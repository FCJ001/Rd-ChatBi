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
    APP_DEBUG: bool = True

    # ---------------- PostgreSQL（共享实例，独立库）----------------
    DB_HOST: str = "localhost"
    DB_PORT: int = 5432
    DB_USER: str = "rdagent"
    DB_PASSWORD: str = "rdagent123"
    DB_NAME: str = "rd_chatbi"

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

    # ---------------- 日志 ----------------
    LOG_LEVEL: str = "DEBUG"
    LOG_DIR: str = "logs"
    AUDIT_LOG_RETENTION: str = "180 days"

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
