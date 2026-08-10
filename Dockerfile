# ── Lumi Backend ──
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 系统依赖：编译工具、时区、OpenMP（torch/sentence-transformers 需要）
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libgomp1 \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖文件与源码（Docker 会缓存已安装依赖层）
COPY pyproject.toml uv.lock ./
COPY app ./app
COPY celery_app ./celery_app
COPY scripts ./scripts
COPY tools ./tools

# 安装 Python 依赖（含项目本身），使用清华镜像加速
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .

# 非 root 用户运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# API: 8000 / TTS: 8765
EXPOSE 8000 8765

# 默认启动 FastAPI（worker/tts 由 docker-compose 的 command 覆盖）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]