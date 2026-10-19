#!/bin/bash

echo "========================================="
echo "      本体评测平台 - 停止服务脚本"
echo "========================================="

PROJECT_NAME="onto-eval-app"
IMAGE_NAME="eval-agent:latest"

if ! command -v docker >/dev/null 2>&1; then
    echo "未检测到 Docker，请先安装 Docker 后再执行本脚本。"
    exit 1
fi

container_id=$(docker ps -aq -f "name=^/${PROJECT_NAME}$")
running_container_id=$(docker ps -q -f "name=^/${PROJECT_NAME}$")

echo ""
echo "[1/2] 检查并停止和删除容器..."

if [ -n "$running_container_id" ]; then
    echo "检测到运行中的容器 $PROJECT_NAME，正在停止..."
    docker stop "$PROJECT_NAME" >/dev/null
    echo "✓ 容器已停止"
fi

if [ -n "$container_id" ]; then
    echo "检测到容器 $PROJECT_NAME，正在删除..."
    docker rm "$PROJECT_NAME" >/dev/null
    echo "✓ 容器已删除"
else
    echo "✓ 容器 $PROJECT_NAME 不存在，跳过删除操作"
fi

echo ""
echo "[2/2] 检查并删除镜像..."

if docker images -q "$IMAGE_NAME" | grep -q .; then
    echo "检测到镜像 $IMAGE_NAME，正在删除..."
    docker rmi "$IMAGE_NAME"
    echo "✓ 镜像已删除"
else
    echo "✓ 镜像 $IMAGE_NAME 不存在，跳过删除操作"
fi

echo ""
echo "========================================="
echo "            操作完成"
echo "========================================="
