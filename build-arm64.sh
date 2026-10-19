#!/usr/bin/env bash
# ============================================================
# 跨架构构建脚本：在 x86 上构建 arm64 的 eval-agent 镜像
# 用法: bash build-arm64.sh
# 产物: eval-agent-arm64-legacy.tar.gz（兼容旧版 Docker 的传统格式）
# ============================================================
set -euo pipefail

IMAGE_NAME="eval-agent"
IMAGE_TAG="arm64"
TAR_NAME="eval-agent-arm64-legacy.tar.gz"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$SCRIPT_DIR"

# ---- 步骤1: 注册 QEMU 模拟器（已注册则跳过）----
if ! ls /proc/sys/fs/binfmt_misc/qemu-aarch64 >/dev/null 2>&1; then
  echo "[1/3] 注册 QEMU binfmt 模拟器..."
  docker run --privileged --rm tonistiigi/binfmt --install arm64
else
  echo "[1/3] QEMU 已注册，跳过"
fi

# ---- 步骤2: 创建/复用 buildx builder（带镜像加速）----
BUILDER_NAME="arm64builder"
if ! docker buildx ls 2>/dev/null | grep -q "$BUILDER_NAME"; then
  echo "[2/3] 创建 buildx builder（含镜像加速配置）..."
  # 生成 buildkitd.toml（阿里云/DaoCloud 镜像加速）
  cat > /tmp/buildkitd-arm64.toml <<'TOML'
[registry."docker.io"]
  mirrors = ["docker.m.daocloud.io", "docker.1ms.run"]
TOML
  docker buildx create --name "$BUILDER_NAME" --use --driver docker-container --config /tmp/buildkitd-arm64.toml
else
  echo "[2/3] buildx builder 已存在，复用"
  docker buildx use "$BUILDER_NAME"
fi
docker buildx inspect --bootstrap >/dev/null 2>&1

# ---- 步骤3: 构建 arm64 镜像并直接导出为 tar ----
echo "[3/3] 构建 arm64 镜像并导出（QEMU 模拟，预计 5-15 分钟）..."
# 使用 --output type=docker 直接导出为 docker save 兼容格式 tar
# 避免 --load 经过 BuildKit gRPC session 导出到 Docker daemon 时超时
# （QEMU 交叉编译耗时长，容易触发 session deadline exceeded）
docker buildx build \
  --platform linux/arm64 \
  -t "${IMAGE_NAME}:${IMAGE_TAG}" \
  --output "type=docker,dest=/tmp/${TAR_NAME%.gz}" \
  -f Dockerfile .

# gzip 压缩
gzip -f "/tmp/${TAR_NAME%.gz}" && mv "/tmp/${TAR_NAME}" "$SCRIPT_DIR/${TAR_NAME}"

echo ""
echo "============================================"
echo "构建完成！"
echo "  镜像: ${IMAGE_NAME}:${IMAGE_TAG} (arm64)"
echo "  产物: ${TAR_NAME} ($(ls -lh "$TAR_NAME" | awk '{print $5}'))"
echo ""
echo "目标系统部署命令:"
echo "  docker load -i ${TAR_NAME}"
echo "  docker tag ${IMAGE_NAME}:${IMAGE_TAG} ${IMAGE_NAME}:latest"
echo "============================================"
