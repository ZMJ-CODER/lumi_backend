# ── Lumi Backend ──
# 仅复制 Docker CLI，运行时不带 daemon；Docker 沙箱仍由宿主 daemon 创建。
FROM docker:27-cli AS docker_cli

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    # Docling / HF 模型下载：走国内镜像的普通 HTTP（禁用 XET 协议，XET CAS 国内 401），
    # 且必须在进程启动前设置（huggingface_hub 在 import 时冻结这些常量）
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_DISABLE_XET=1 \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    TQDM_DISABLE=1 \
    DOCLING_INFERENCE_COMPILE_TORCH_MODELS=false \
    HF_HOME=/app/.cache/huggingface

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

# ── CUDA 版 torch（可选：镜像默认装 CUDA 版；构建时不想要 GPU 可传
#    --build-arg ENABLE_CUDA_TORCH=false 退回 PyPI CPU 版） ──
ARG ENABLE_CUDA_TORCH=true
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126
RUN if [ "$ENABLE_CUDA_TORCH" = "true" ]; then \
        pip install --index-url ${TORCH_INDEX_URL} torch==2.13.0+cu126 torchvision==0.28.0+cu126; \
    fi

WORKDIR /app

# 先复制依赖清单，Docker 会据此识别依赖变化。
COPY pyproject.toml uv.lock ./
# 镜像源用 ARG 声明而非写死在 RUN 里（默认 PyPI，CI/海外可直连）：
# 国内构建时传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
# ``pip install -e .`` 不能在源码缺失时运行：冷构建会因 setuptools 找不到 app 包失败。
# 因此先复制源码，再用项目清单安装运行时依赖。
COPY app ./app
COPY celery_app ./celery_app
COPY scripts ./scripts
COPY tools ./tools
COPY plugins ./plugins
COPY alembic ./alembic
COPY alembic.ini ./

# BuildKit 缓存挂载：下载好的 wheel 跨构建持久化。``pip install .`` 会安装
# pyproject 的完整运行时集合；导入检查让 temporalio / striprtf 遗漏在构建期失败。
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install . \
    && python -c "import striprtf; import temporalio; import app.main; print('runtime dependency check passed')"

# ── Docling 模型预热（可选，默认关闭）──
# 默认不预下载：构建期拉取约 1GB 模型容易卡住/失败；首次解析时按需下载。
ARG PRELOAD_DOCLING_MODELS=false
RUN if [ "$PRELOAD_DOCLING_MODELS" = "true" ]; then \
        docling-tools models download \
        || echo "Docling 模型预热失败（构建继续，运行时按需下载）"; \
    fi

# Docker 脚本沙箱通过 CLI 连接宿主 Docker daemon。没有 socket 时能力层会安全隐藏
# python_exec，不会退回为后端本地执行。
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker

# 非 root 用户运行
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# API: 8000 / TTS: 8765
EXPOSE 8000 8765

# 默认启动：先跑数据库迁移（幂等），再启动 FastAPI（worker/tts 由 docker-compose 覆盖）
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
