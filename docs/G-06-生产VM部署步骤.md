# G-06 生产 VM 部署步骤

> **目标**：在 `/data/onto-eval`（OpenCloudOS / Linux）完成 **G-06 容器重建 + G-01 PG + G-02 ingest E2E** 签收。  
> **关联**：[P2.5-部署与CI.md](./P2.5-部署与CI.md) · [G-01-PG切换Runbook.md](./G-01-PG切换Runbook.md)

---

## 0. 部署前清单

| 项 | 要求 |
|----|------|
| 目录 | `cd /data/onto-eval` |
| Docker | `docker compose version` 可用 |
| onto-agent | 宿主机 `:5055` 已运行，`curl http://127.0.0.1:5055/health` → `{"status":"ok"}` |
| 密钥 | `.env` 中 `GLM_API_KEY`、`LANGFUSE_*`、`ONTO_AGENT_API_TOKEN` 已配置 |
| 维护窗口 | 暂停新评测任务（避免迁移/切换期间双写） |
| 磁盘 | 预留 PG 卷 + 镜像 build 空间（建议 ≥ 5GB） |

**镜像拉取失败（Hub EOF）**：在 `.env` 增加：

```bash
FRONTEND_BASE_IMAGE=docker.m.daocloud.io/library/node:20-alpine
PYTHON_BASE_IMAGE=docker.m.daocloud.io/library/python:3.11-slim
```

---

## 1. 一键部署（推荐）

SSH 登录生产 VM 后：

```bash
cd /data/onto-eval
git fetch origin
git log -1 --oneline   # 确认已到含 P2.5 / 包 A 的 master

chmod +x scripts/*.sh
bash scripts/prod-deploy.sh
```

可选完整 P2.5 门禁（含 PG smoke + Push simulate，不含 Playwright）：

```bash
bash scripts/prod-deploy.sh --full-gate
```

若已手动 `git pull`：

```bash
bash scripts/prod-deploy.sh --skip-pull
```

**预期输出末尾**：`P0 smoke passed.`；容器 `onto-eval-app`、`onto-eval-postgres` 均为 `Up`。

---

## 2. 手动分步（与脚本等价）

### Step 1 — 拉代码

```bash
cd /data/onto-eval
git pull origin master
```

### Step 2 — 备份 + 修正 .env

```bash
cp -a backend/data/evaluation.db backend/data/evaluation.db.bak.$(date +%Y%m%d) 2>/dev/null || true
cp .env.example .env   # 仅首次；已有 .env 则跳过

# 必须项（Docker 内访问宿主机 agent）
grep -q '^DATABASE_URL=postgresql' .env || \
  sed -i 's|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact|' .env
grep -q 'host.docker.internal' .env || \
  sed -i 's|^ONTO_AGENT_BASE_URL=.*|ONTO_AGENT_BASE_URL=http://host.docker.internal:5055|' .env
```

生产环境请将 `POSTGRES_PASSWORD` 改为强密码，并与 `DATABASE_URL` 一致。

### Step 3 — 构建并启动

```bash
docker compose build app
docker compose up -d
docker compose ps
```

**勿只 `docker compose up -d`**：代码变更后必须 **build**，否则仍是旧镜像。

### Step 4 — Alembic

```bash
docker exec onto-eval-app sh -c 'cd /app/backend && alembic upgrade head'
docker exec onto-eval-app sh -c 'cd /app/backend && alembic current'
# 期望 head: 009_scenario_step_enforce_order（以仓库 alembic 为准）
```

### Step 5 — SQLite 历史迁移（有旧数据时）

```bash
cd /data/onto-eval/backend
pip install -q psycopg2-binary   # 宿主机若无 venv
python scripts/migrate_runs_to_pg.py --dry-run \
  --source-url sqlite:///./data/evaluation.db \
  --target-url postgresql+psycopg2://onto_eval:YOUR_PASS@127.0.0.1:5433/onto_eval_fact

python scripts/migrate_runs_to_pg.py \
  --source-url sqlite:///./data/evaluation.db \
  --target-url postgresql+psycopg2://onto_eval:YOUR_PASS@127.0.0.1:5433/onto_eval_fact

python scripts/migrate_runs_to_pg.py --verify-only \
  --source-url sqlite:///./data/evaluation.db \
  --target-url postgresql+psycopg2://onto_eval:YOUR_PASS@127.0.0.1:5433/onto_eval_fact
```

**新环境无 SQLite 历史**：跳过本步。

### Step 6 — P0 冒烟

```bash
cd /data/onto-eval
bash scripts/p0-smoke.sh
```

---

## 3. G-02 Trace Sync Worker E2E

> **v1.23 变更**：Webhook/Push 摄入端点已全部移除，`FACT_INGEST_MODE` 配置项已取消。事实物化统一由 Trace Sync Worker（`TRACE_SYNC_WORKER_ENABLED=true`，默认开启）从 Langfuse API 拉取完成。Agent 只需保证 Langfuse `flush()` 成功。

### 3.1 eval 侧（通常已满足）

- `TRACE_SYNC_WORKER_ENABLED=true`（默认，`.env` 中无需显式设置）
- `GET http://127.0.0.1:45000/api/trace/health` → 200

### 3.2 onto-agent 侧

Agent 无需配置 `ONTO_EVAL_BASE_URL` 或 `ONTO_EVAL_PUSH_ENABLED`。Agent 只需保证 Langfuse `flush()` 成功。

重建 agent 后确认 workspace 404 返回 JSON（G-05）。

### 3.3 验证命令

```bash
cd /data/onto-eval/backend

# 验证 Trace Sync Worker 物化逻辑
python -m pytest tests/test_trace_sync_worker.py -q
```

**通过标准**：pytest 全部通过；PG 中对应 `run_id` 的 `run_span_nodes` 有行。

```bash
docker exec -it onto-eval-postgres psql -U onto_eval -d onto_eval_fact \
  -c "SELECT count(*) FROM run_span_nodes;"
```

---

## 4. 功能抽检（§6 对照）

| 项 | 操作 | 期望 |
|----|------|------|
| 健康 | `curl -s http://127.0.0.1:45000/api/health` | `"status":"ok"` |
| 任务列表 | 浏览器或 `curl http://127.0.0.1:45000/api/tasks` | 200，历史任务可见 |
| Facets | 任务详情 / Trace 页 | 优先 PG Run，非 Langfuse 全量扫描 |
| 在线评测 | 跑 1 题 zc 场景 | 完成且无 500 |
| 场景对比 | 评估历史勾选 2+ 任务 →「场景/版本对比」 | 矩阵可加载 |

---

## 5. 故障排查

| 现象 | 原因 | 处理 |
|------|------|------|
| 容器 Restarting | 镜像旧 / Python 语法错误 | `docker logs onto-eval-app --tail 80`；**必须 rebuild** |
| `YAML parse error` / `<<<<<<<` | 合并冲突残留 | `bash scripts/ci-check.sh`；修复后重新 build |
| `connection refused postgres` | PG 未起或 URL 错误 | `docker compose up -d postgres`；URL 用 `@postgres:5432`（容器内） |
| eval 连不上 agent | Docker 内 localhost | `.env` → `ONTO_AGENT_BASE_URL=http://host.docker.internal:5055` |
| `Cannot connect to host localhost:5055` | 同上 | 改 URL 后 `docker compose up -d --force-recreate app` |
| build 拉镜像 EOF | Docker Hub 不可达 | `.env` 设置 DaoCloud 基础镜像（见 §0） |
| Push `no_matching_run` | agent/eval 不同库 | 统一 PG；确认 `DATABASE_URL` 一致 |
| alembic 失败 | PG 密码/端口 | `docker compose logs postgres`；核对 `.env` POSTGRES_* |

**查看应用是否能 import**：

```bash
docker exec onto-eval-app python -c "import main; print('ok')"
```

---

## 6. 回滚

```bash
cd /data/onto-eval
docker compose stop app
# 恢复 .env 中 DATABASE_URL 为 SQLite（需保留切换前 evaluation.db 备份）
docker compose up -d --force-recreate app
```

PG 切换后新 Run 不会自动回到 SQLite；仅在阻塞故障时短期回滚。

---

## 7. 签收（复制到 G-01 Runbook §9）

| 步骤 | 日期 | 执行人 | 结果 |
|------|------|--------|------|
| G-06 prod-deploy / build + up | | | ☐ |
| G-01 PG + alembic head | | | ☐ |
| p0-smoke | | | ☐ |
| G-02 Trace Sync Worker E2E | | | ☐ |
| §4 功能抽检 | | | ☐ |

---

**文档版本**：v1.0 · 2026-06-01
