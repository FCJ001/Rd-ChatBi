# ============================================================
# 应用入口
#
# 启动：uvicorn src.main:app --reload --port 8003
# 文档：http://localhost:8003/docs
# ============================================================

from contextlib import asynccontextmanager

from fastapi import FastAPI
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logger()
    logger.info(f"{settings.APP_NAME} 启动 env={settings.APP_ENV} port=8003")
    yield
    logger.info(f"{settings.APP_NAME} 关闭")


app = FastAPI(
    title=settings.APP_NAME,
    debug=settings.APP_DEBUG,
    lifespan=lifespan,
)

app.add_middleware(TraceLoggingMiddleware)
app.add_middleware(PrometheusMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
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
async def metrics():
    """Prometheus 指标暴露端点（默认 /metrics 文本格式）"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── 静态文件 & SPA ───────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="src/static"), name="static")


@app.get("/")
async def index():
    return FileResponse("src/static/chatbi.html")
