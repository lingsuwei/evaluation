# 多阶段构建（层顺序：先依赖清单 → 安装依赖 → 再 COPY 源码，便于 docker compose build 复用缓存）
ARG FRONTEND_BASE_IMAGE=node:20-alpine
ARG PYTHON_BASE_IMAGE=python:3.11-slim

# 阶段1: 构建前端
FROM ${FRONTEND_BASE_IMAGE} AS frontend-builder

# 设置上海时区
RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone
ENV TZ=Asia/Shanghai

WORKDIR /app/frontend
# 仅 lock 变化时重建 node_modules
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com && npm ci
# 源码变化时重建 dist，但不触发 npm ci
COPY frontend/ ./
RUN npm run build


# 阶段2: 最终镜像
FROM ${PYTHON_BASE_IMAGE}

# 设置上海时区
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && \
    ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && \
    echo "Asia/Shanghai" > /etc/timezone && \
    rm -rf /var/lib/apt/lists/*
ENV TZ=Asia/Shanghai

WORKDIR /app

# ===== 阿里云 APT 镜像源开始 =====
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g; s|http://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g; s|http://security.debian.org/debian-security|https://mirrors.aliyun.com/debian-security|g' /etc/apt/sources.list; \
    fi
# ===== 阿里云 APT 镜像源结束 =====

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    net-tools \
    htop \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖：仅 requirements.txt 变化时重建（改 .py 不会触发 pip install）
COPY backend/requirements.txt ./backend/requirements.txt
WORKDIR /app/backend
RUN pip install --no-cache-dir --break-system-packages \
    -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com \
    -r requirements.txt

# 前端构建产物（仅前端 stage 重建时更新）
COPY --from=frontend-builder /app/frontend/dist /app/frontend/dist

# 配置 Nginx
COPY nginx.conf /etc/nginx/nginx.conf

WORKDIR /app

# 暴露端口
EXPOSE 80

# 环境变量
ENV ENABLED_METRICS="faithfulness,answer_relevancy,correctness"

# 启动
# ⚠️ 注意：不能添加 --workers 参数！SSE 事件流基于进程内内存存储，
# 多 Worker 会导致前端无法收到评测进度更新。如需多 Worker 请先改用 Redis Pub/Sub。
CMD ["sh", "-c", "nginx && cd /app/backend && exec uvicorn main:app --host 0.0.0.0 --port 8000"]
