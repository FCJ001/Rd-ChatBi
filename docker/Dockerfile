# ============================================================
# rd-chatBI 应用镜像
#
# 构建：docker build -t rd-chatbi:latest .
# 运行：docker compose up -d（见 docker-compose.yml）
# ============================================================

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖（利用镜像层缓存）
COPY requirements.txt .
RUN pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 再拷代码
COPY conf/ conf/
COPY prompts/ prompts/
COPY scripts/ scripts/
COPY src/ src/

EXPOSE 8003

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8003"]
