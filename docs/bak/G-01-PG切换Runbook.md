# G-01 PostgreSQL 生产切换 Runbook

> **门禁 ID**：G-01  
> **目标**：将 onto-eval Runtime Fact Layer + 编排表从 SQLite 切换到 PostgreSQL，并通过 PG smoke 6 项验收。  
> **依据**：[P2-ADR-数据库策略.md](./P2-ADR-数据库策略.md)、Alembic `001`～`006`、`scripts/migrate_runs_to_pg.py`

---

## 1. 适用范围

| 环境 | 仓库默认（v1.19） | 切换后 / 生产目标 |
|------|-------------------|-------------------|
| 本地 Docker | `postgresql+asyncpg://onto_eval:***@postgres:5432/onto_eval_fact`（`docker-compose.yml` 默认） | 同左；SQLite 仅作 `.env` 调试回退 |
| 本地非 Docker | 宿主机 `127.0.0.1:${POSTGRES_PORT}`（默认 5433） | `postgresql+asyncpg://...@127.0.0.1:5433/onto_eval_fact` |
| 预发/生产 VM | 可能仍为旧 SQLite 镜像 | **单一 PG 实例**（编排 + 事实表）；执行本 Runbook **G-01** |

**不在本 Runbook**：Langfuse 自身数据库、onto-agent 存储。

---

## 2. 前置条件

- [ ] PostgreSQL **16+** 已部署（Docker：`docker compose up -d postgres`）
- [ ] 网络：应用容器能解析 `postgres` 服务名（Compose 内）或 `127.0.0.1:5433`（宿主机）
- [ ] 备份：切换前完整备份 SQLite 文件 `backend/data/evaluation.db`（及 Docker 卷 `/app/data/evaluation.db`）
- [ ] 维护窗口：切换期间暂停新评测任务（避免双写）
- [ ] 凭据：`POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` 已写入密钥管理（勿提交 git）

**Alembic 当前 head**：`009_scenario_step_enforce_order`（以 `alembic current` 为准）

---

## 3. 连接串约定

### 3.1 Docker Compose（推荐）

`.env` 或 `docker-compose.yml` 中 **app 服务**：

```bash
# 容器内必须用服务名 postgres，勿用 127.0.0.1
DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact
POSTGRES_USER=onto_eval
POSTGRES_PASSWORD=onto_eval_dev
POSTGRES_DB=onto_eval_fact
POSTGRES_PORT=5433   # 宿主机映射端口，供本地脚本/Alembic 使用
```

### 3.2 宿主机直连（开发 / smoke）

```bash
DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@127.0.0.1:5433/onto_eval_fact
```

### 3.3 迁移 CLI（同步 psycopg2 URL）

```bash
PG_TARGET_URL=postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:5433/onto_eval_fact
SQLITE_SOURCE_URL=sqlite:///d:/work/.../onto-eval/backend/data/evaluation.db
```

---

## 4. 一键验收（P2.5 推荐）

首次部署或空库初始化：

```bash
cd onto-eval
bash scripts/p0-bootstrap.sh    # 或 powershell -File scripts/p0-bootstrap.ps1
bash scripts/p0-smoke.sh        # 健康 + PG 连通
```

完整 P2.5 门禁：

```powershell
cd onto-eval
powershell -File scripts/p25-gate.ps1
```

Linux / WSL：

```bash
cd onto-eval
bash scripts/p25-gate.sh
```

---

## 5. 切换步骤（标准流程，手动）

### Phase A — 准备 PG 空库

```bash
cd onto-eval
docker compose up -d postgres
# 等待 healthy
docker compose ps postgres
```

```bash
cd backend
pip install -r requirements.txt
set DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@127.0.0.1:5433/onto_eval_fact
python -c "from services.pg_bootstrap import bootstrap_postgresql_schema; bootstrap_postgresql_schema()"
python -m alembic current   # 应显示 009_scenario_step_enforce_order (head)
```

> **说明**：空库使用 `pg_bootstrap`（ORM `create_all` + `v_run_analytics` + `alembic stamp head`）；已有 `alembic_version` 时自动 `upgrade head`。

**验收**：PG 中存在 `run_span_nodes`、`run_metrics`、`v_run_analytics`、`experiments` 等对象。

### Phase B — 迁移 SQLite 数据（有历史数据时）

```bash
cd backend

# 1) 干跑统计
python scripts/migrate_runs_to_pg.py --dry-run \
  --source-url sqlite:///./data/evaluation.db \
  --target-url postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:5433/onto_eval_fact

# 2) 正式迁移
python scripts/migrate_runs_to_pg.py \
  --source-url sqlite:///./data/evaluation.db \
  --target-url postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:5433/onto_eval_fact

# 3) 对账
python scripts/migrate_runs_to_pg.py --verify-only \
  --source-url sqlite:///./data/evaluation.db \
  --target-url postgresql+psycopg2://onto_eval:onto_eval_dev@127.0.0.1:5433/onto_eval_fact
```

**新环境无历史数据**：可跳过 Phase B，仅 `alembic upgrade head` + 应用启动后 `init_db` 补全编排表。

### Phase C — 切换应用 DATABASE_URL

1. 更新 `.env` / 部署密钥中的 `DATABASE_URL` 为 PG 连接串（见 §3）
2. **重建并重启** eval 容器：

```powershell
cd onto-eval
docker compose build app
docker compose up -d --force-recreate app
docker exec onto-eval-app printenv DATABASE_URL
# 期望 postgresql+asyncpg://...@postgres:5432/...
```

3. 确认应用日志无 DB 连接错误；访问 `http://127.0.0.1:45000/api/trace/health` 返回 200

### Phase D — 联调 Ingest 物化（G-02 门禁）

```bash
cd backend
python scripts/verify_push_e2e.py simulate --eval-url http://127.0.0.1:45000
```

> **v1.22 说明**：`verify_push_e2e.py simulate` 验证 eval ingest API + Builder 逻辑，脚本名为历史命名。Agent Push 已废弃，无需 `live` 步骤和 `ONTO_EVAL_PUSH_ENABLED`。事实物化由 `internal_sync` Worker 从 Langfuse API 拉取完成。

---

## 5. PG smoke 验收（6 项）

在 **Phase C 完成后** 执行：

**Windows**：

```powershell
cd onto-eval/backend
.\scripts\run_pg_smoke.ps1
```

**Linux / WSL**：

```bash
cd onto-eval/backend
./scripts/run_pg_smoke.sh
```

脚本覆盖的 pytest 模块（合计 6 项场景）：

| # | 测试文件 | 验收点 |
|---|----------|--------|
| 1 | `test_alembic_pg_smoke.py` | `alembic upgrade head` 创建 P2～P5 表/视图 |
| 2 | `test_alembic_pg_smoke.py` | 重复 upgrade 幂等 |
| 3 | `test_alembic_pg_smoke.py` | downgrade base + 再 upgrade 可恢复 |
| 4 | `test_migrate_pg_smoke.py` | SQLite → PG 迁移 + `v_run_analytics` 行可读 |
| 5 | `test_migrate_pg_smoke.py` | 迁移对账 `verify_migration` |
| 6 | `test_pg_integration_smoke.py` | 端到端：Alembic + 迁移 + 宽表读路径 |

**通过标准**：上述 pytest **全部 green**；输出 `PG smoke 全部通过`。

---

## 6. 切换后功能抽检

| 项 | 命令/操作 | 期望 |
|----|-----------|------|
| 任务列表 | `GET /api/tasks` | 200，历史任务可见 |
| 任务详情 | 打开已完成任务 EvalDetailModal | 物化状态、Token 列正常 |
| Facets | `GET /api/trace/facets?task_id=...` | `source=agent_runs`，非 Langfuse 降级 |
| Push 物化 | live E2E 或在线评测一题 | `trace_sync_status=synced`，`run_span_nodes` 有数据 |
| 场景/版本对比 | 评估历史勾选 2+ 同评测集任务 →「场景/版本对比」 | Outcome 摘要表 + 场景子步骤矩阵可加载 |

---

## 7. 回滚方案

若切换后出现阻塞性问题：

1. **停止** eval 应用容器
2. 将 `DATABASE_URL` **改回 SQLite** 路径（保留 Phase A 前备份的 `evaluation.db`）
3. `docker compose up -d --force-recreate app`
4. 验证 `GET /api/tasks` 与一题评测可用
5. PG 库保留不动，待根因修复后再次执行 Phase B～D

> PG 切换后新产生的 Run 不会自动回写 SQLite；回滚会丢失切换期间 PG 独有数据。维护窗口内应禁止长时间双轨写入。

---

## 8. 常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| `connection refused 127.0.0.1:5432` | 容器内误用 localhost | 改用 `@postgres:5432` |
| `alembic.util.exc.CommandError` | PG 未启动或密码错误 | `docker compose logs postgres` |
| Push `no_matching_run` | 脚本与 API 不同库 | 统一 `DATABASE_URL`；Docker app 用 `/app/data` 卷 |
| Facets 仍扫 Langfuse | 无 PG Run 或 task_id 缺失 | 确认迁移含 `agent_runs` |
| WSL 5432 占用 | 端口冲突 | `.env` 设 `POSTGRES_PORT=5433` |

---

## 9. 签收清单

| 步骤 | 执行人 | 日期 | 结果 |
|------|--------|------|------|
| Phase A Alembic upgrade head | | | ☐ |
| Phase B 数据迁移 + verify | | | ☐ / N/A |
| Phase C DATABASE_URL 切换 + 容器重建 | | | ☐ |
| Phase D Push live E2E | | | ☐ |
| PG smoke 6 项 | | | ☐ |
| §6 功能抽检 | | | ☐ |
| 备份与回滚演练记录 | | | ☐ |

---

## 10. 相关文档

- [P2-开工进度.md](./P2-开工进度.md) §本地启用 PostgreSQL
- [P2-用户待办汇总.md](./P2-用户待办汇总.md) G-01
- [P2.5-部署与CI.md](./P2.5-部署与CI.md)
- [架构设计说明书 §21](./onto-eval评测优化架构设计说明书.md#21-cto-审视gap-与待办-v119)
- [M-05 ingest ACL 复核清单](./M-05-ingest-ACL复核清单.md)（PG 切换后一并复核 nginx）

---

**文档版本**：v1.1 · 2026-06-01
