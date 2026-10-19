#!/usr/bin/env bash
# Release Gate：冲突标记扫描 + compose 语法校验
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> 扫描 Git 冲突标记"
if git grep -n '^<<<<<<< ' -- . ':(exclude).git' 2>/dev/null; then
  echo "ERROR: 发现未解决的 Git 冲突标记 (<<<<<<<)" >&2
  exit 1
fi
if git grep -n '^>>>>>>> ' -- . ':(exclude).git' 2>/dev/null; then
  echo "ERROR: 发现未解决的 Git 冲突标记 (>>>>>>>)" >&2
  exit 1
fi
echo "[ok] 无冲突标记"

echo "==> 校验 docker-compose.yml"
python3 - <<'PY'
import pathlib
import sys

try:
    import yaml
except ImportError:
    yaml = None

path = pathlib.Path("docker-compose.yml")
text = path.read_text(encoding="utf-8")
if yaml is not None:
    yaml.safe_load(text)
else:
    for bad in ("<<<<<<<", "=======", ">>>>>>>"):
        if bad in text:
            print(f"ERROR: docker-compose 含冲突标记 {bad}", file=sys.stderr)
            sys.exit(1)
print("[ok] docker-compose.yml")
PY

echo "==> ci-check 通过"
