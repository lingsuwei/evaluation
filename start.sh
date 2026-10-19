#!/bin/bash

TAG=$1
MODE=${2:-standalone}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "========================================="
echo "      本体评测平台 - 服务启动脚本"
echo "========================================="
echo ""

if [ -z "$TAG" ]; then
    echo "提示: 未指定镜像标签，将使用默认配置"
else
    echo "镜像标签: $TAG"
fi

# 启动模式说明
#   standalone  - 独立模式（自带 PostgreSQL，默认）
#   langfuse    - Langfuse 联通模式（共享 Langfuse 的 PostgreSQL 和网络）
echo "启动模式: $MODE"
echo ""

ensure_env_file() {
    local env_file="$1"
    local example_file="$2"
    if [ ! -f "$env_file" ]; then
        if [ -f "$example_file" ]; then
            cp "$example_file" "$env_file"
            echo "已自动创建 $(basename "$env_file")（来自 $(basename "$example_file")），请按需修改配置后重启。"
        else
            echo "错误: 缺少 $(basename "$env_file")，且未找到 $(basename "$example_file")"
            exit 1
        fi
    fi
}

ensure_env_file "$SCRIPT_DIR/.env" "$SCRIPT_DIR/.env.example"
# Langfuse 项目配置文件位于 backend/data/（docker-compose.yml 挂载 ./backend/data → /app/data）
mkdir -p "$SCRIPT_DIR/backend/data"
ensure_env_file "$SCRIPT_DIR/backend/data/langfuse.projects.json" "$SCRIPT_DIR/langfuse.projects.example.json"

if command -v docker-compose >/dev/null 2>&1 && docker-compose --version >/dev/null 2>&1; then
    compose_cmd=(docker-compose)
elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    compose_cmd=(docker compose)
else
    echo "未检测到可用的 Docker Compose，请先安装或修复后再执行本脚本。"
    echo
    echo "Ubuntu 安装命令："
    echo "  sudo apt-get update"
    echo "  sudo apt-get install -y docker-compose-plugin"
    echo
    echo "CentOS 安装命令："
    echo "  sudo yum install -y docker-compose-plugin"
    echo
    echo "安装完成后可执行以下命令验证："
    echo "  docker compose version"
    echo "  或 docker-compose --version"
    exit 1
fi

echo ""
echo "开始启动服务..."
echo ""

COMPOSE_FILES="-f $SCRIPT_DIR/docker-compose.yml"

if [ "$MODE" = "langfuse" ]; then
    COMPOSE_FILES="$COMPOSE_FILES -f $SCRIPT_DIR/docker-compose.langfuse.override.yml"
fi

"${compose_cmd[@]}" $COMPOSE_FILES up -d

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ 服务启动完成（模式: $MODE）"
    echo ""
    echo "访问地址:"
    echo "  - Web界面: http://localhost:45500"
    echo "  - API接口: http://localhost:3001"
    echo ""
    echo "========================================="
    echo "            启动成功"
    echo "========================================="
else
    echo ""
    echo "✗ 服务启动失败"
    echo ""
    echo "========================================="
    echo "            启动失败"
    echo "========================================="
    exit 1
fi
