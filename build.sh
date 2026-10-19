#!/usr/bin/env bash
set -e

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

"${compose_cmd[@]}" -f docker-compose.yml build --no-cache
