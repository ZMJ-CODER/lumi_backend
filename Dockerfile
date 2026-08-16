# syntax=docker/dockerfile:1

# ── Lumi Backend ──
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

# 先只复制依赖清单并安装项目（editable 模式），
# 该层只要 pyproject.toml / uv.lock 不变就永远命中缓存
COPY pyproject.toml uv.lock ./
# 镜像源用 ARG 声明而非写死在 RUN 里（默认 PyPI，CI/海外可直连）：
# 国内构建时传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
# BuildKit 缓存挂载：下载好的 wheel 跨构建持久化。
# 即使依赖层因 pyproject/uv.lock 变化或缓存失效而重建，
# 也直接复用本地缓存安装，不用重新下载（torch 等大包从"半天"降到秒级）。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e .

# 源码最后复制 —— 源码改动只触发本层及以下层，依赖层不受影响
COPY app ./app
COPY celery_app ./celery_app
COPY scripts ./scripts
COPY tools ./tools
COPY plugins ./plugins
COPY alembic ./alembic
COPY alembic.ini ./

# 非 root 用户运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# API: 8000 / TTS: 8765
EXPOSE 8000 8765

# 默认启动：先跑数据库迁移（幂等），再启动 FastAPI（worker/tts 由 docker-compose 覆盖）
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
