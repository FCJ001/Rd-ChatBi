# ============================================================
# 应用入口
#
# 启动：uvicorn src.main:app --reload --port 8003
# 文档：http://localhost:8003/docs
# ============================================================

import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from src.core.base_schema import ResponseSchema
from src.core.config import get_settings
from src.core.exceptions import register_exception_handlers
from src.core.logger import logger, setup_logger
from src.core.metrics import PrometheusMiddleware
from src.middlewares.logging import TraceLoggingMiddleware

settings = get_settings()


def _cors_origins(raw: str) -> list[str]:
    origins = [o.strip() for o in (raw or "").split(",") if o.strip()]
    return origins or ["*"]


_CORS_ORIGINS = _cors_origins(settings.CORS_ORIGINS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logger()
    logger.info(f"{settings.APP_NAME} 启动 env={settings.APP_ENV} port=8003")
    # jieba 词典/TF-IDF 首次加载约 1s：预热放启动，别让第一个请求背延迟
    import jieba.analyse
    jieba.analyse.extract_tags("预热", topK=1)
    logger.info("jieba 预热完成")
    # 注册 jieba 用户词典：表名/字段别名/指标名先进分词器，业务词（如「门诊量」）
    # 从一开始就被正确切分，而不是靠 TF-IDF 碰运气 + LLM 扩展兜底
    try:
        from src.nl2sql.dict_loader import load_jieba_userdict
        n = await load_jieba_userdict()
        logger.info(f"jieba 用户词典加载完成，{n} 个业务词")
    except Exception as e:
        # 词典加载失败只降低召回质量，不该拦住服务启动
        logger.warning(f"jieba 用户词典加载失败（降级为默认词典）: {e}")
    yield
    # 关闭时释放连接池：不 dispose 的话池里连接要等服务端超时才回收
    try:
        from src.infra.pool import dispose_all_pools
        await dispose_all_pools()
    except Exception as e:
        logger.warning(f"关闭连接池失败: {e}")
    logger.info(f"{settings.APP_NAME} 关闭")


app = FastAPI(
    title=settings.APP_NAME,
    debug=settings.APP_DEBUG,
    lifespan=lifespan,
)

app.add_middleware(TraceLoggingMiddleware)
app.add_middleware(PrometheusMiddleware)
# ★ allow_origins="*" 与 allow_credentials=True 不能同用（浏览器规范拒绝带凭证的
#   通配源），配置了具体白名单才开 credentials
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials="*" not in _CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)
register_exception_handlers(app)

# ── 注册路由 ────────────────────────────────────────────────────────────

from src.nl2sql.router import router as bi_router

app.include_router(bi_router)


@app.get("/health", response_model=ResponseSchema[dict])
async def health():
    return ResponseSchema(data={"app": settings.APP_NAME, "env": settings.APP_ENV})


@app.get("/metrics", include_in_schema=False)
async def metrics(authorization: str = Header("")):
    """Prometheus 指标暴露端点（默认 /metrics 文本格式）。

    METRICS_TOKEN 配置后要求 Bearer 鉴权（Prometheus 的 bearer_token
    抓取配置原生支持）；未配置则开放，靠内网隔离兜底 —— 指标里的
    路由/状态码分布属于内部拓扑信息。compare_digest 防时序侧信道。"""
    token = get_settings().METRICS_TOKEN
    if token:
        supplied = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="metrics 鉴权失败")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── 静态文件 & SPA ───────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="src/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("src/static/chatbi.html")

