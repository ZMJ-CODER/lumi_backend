# ── Lumi Backend ──
# 仅复制 Docker CLI，运行时不带 daemon；Docker 沙箱仍由宿主 daemon 创建。
FROM docker:27-cli AS docker_cli

FROM python:3.13-slim

# Hugging Face 模型下载源。构建期和运行期共用同一默认值；海外/私有 Hub
# 可用 ``--build-arg HF_ENDPOINT=...`` 覆盖，而无需改 Dockerfile。
ARG HF_ENDPOINT=https://hf-mirror.com
ARG HF_HUB_DISABLE_XET=1

# Docling / HF 模型下载：走国内镜像的普通 HTTP（禁用 XET 协议，XET CAS 国内 401），
# 且必须在进程启动前设置（huggingface_hub 在 import 时冻结这些常量）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    HF_ENDPOINT=${HF_ENDPOINT} \
    HF_HUB_DISABLE_XET=${HF_HUB_DISABLE_XET} \
    HF_HUB_DISABLE_SYMLINKS_WARNING=1 \
    TQDM_DISABLE=1 \
    DOCLING_INFERENCE_COMPILE_TORCH_MODELS=false \
    HF_HOME=/app/.cache/huggingface

# 系统依赖：编译工具、时区、OpenMP（torch/sentence-transformers 需要）
# 国内构建默认走阿里云 apt 源（可传 --build-arg APT_MIRROR= 关闭）。
# 注意必须把 "http://deb.debian.org" 整体替换，否则会残留 http:// 前缀产生
# "http://https://mirrors.aliyun.com" 双重协议导致 apt 解析失败。
ARG APT_MIRROR=https://mirrors.aliyun.com
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        libgomp1 \
        tzdata \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 镜像源用 ARG 声明而非写死在 RUN 里。默认清华 PyPI（国内构建免传参）；
# CI/海外可传 --build-arg PIP_INDEX_URL=https://pypi.org/simple 直连官方源。
# 提前声明：torch CUDA wheel 的 nvidia-* 依赖也要从这里解析（国内镜像下载）。
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# ── CUDA 版 torch（可选：镜像默认装 CUDA 版；构建时不想要 GPU 可传
#    --build-arg ENABLE_CUDA_TORCH=false 退回 PyPI CPU 版） ──
# 默认走阿里云 PyTorch 镜像（国内直连官方 download.pytorch.org 会卡死/超时）。
# 阿里云 pytorch-wheels 是扁平目录（非 PEP 503 simple index），必须用
# --find-links 而非 --index-url；torch 的 nvidia-cuda-runtime 等依赖由清华源补齐。
# CI/海外可传 --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126
ARG ENABLE_CUDA_TORCH=true
ARG TORCH_INDEX_URL=https://mirrors.aliyun.com/pytorch-wheels/cu126
RUN if [ "$ENABLE_CUDA_TORCH" = "true" ]; then \
        pip install \
            --find-links ${TORCH_INDEX_URL} \
            --index-url ${PIP_INDEX_URL} \
            torch==2.13.0+cu126 torchvision==0.28.0+cu126; \
    fi

WORKDIR /app

# 先复制依赖清单，Docker 会据此识别依赖变化。
COPY pyproject.toml uv.lock ./
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

# Schema 迁移由部署阶段的单实例 ``migrate`` job 负责；应用副本只提供服务，
# 避免多副本启动时并发执行 DDL。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
