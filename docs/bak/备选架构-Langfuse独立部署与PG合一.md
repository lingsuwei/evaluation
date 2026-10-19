# 备选架构方案 C — Langfuse 独立部署与 PostgreSQL 合一

> **地位**：相对 [onto-eval评测优化架构设计说明书.md](./onto-eval评测优化架构设计说明书.md) 的轻量备选方案。  
> **定位**：Langfuse 在应用层仍独立部署，但作为 onto-eval 的观测子模块纳入统一运维边界；PostgreSQL 从“两套实例”收敛为“同一实例、两个 database”。  
> **关联**：[设计范式.md](./设计范式.md) · [Langfuse-Webhook配置指南.md](./Langfuse-Webhook配置指南.md) · [备选架构-Langfuse内嵌与eval侧统一物化.md](./备选架构-Langfuse内嵌与eval侧统一物化.md)

| 项目 | 内容 |
|------|------|
| 文档名称 | 备选架构 C：Langfuse 独立部署 + PostgreSQL 合一 |
| 版本 | **v0.2** |
| 状态 | **草案 · 待评审** |
| 日期 | 2026-06-07 |
| 适用目标 | 最小部署改造、降低 Agent 适配争议、保留 Langfuse 现有运行方式 |

---

## 0. 一句话摘要

**Langfuse 服务仍然单独部署，onto-eval 服务也仍然单独部署；两者不合并进同一个应用进程或 compose 单元。短期只复用 Langfuse v3.173.0 OSS 栈中的 PostgreSQL 实例，为 onto-eval 新增独立 database `onto_eval_fact`。Langfuse 的 ClickHouse、Redis、MinIO/S3、worker 继续由 Langfuse 栈自己管理。**

该方案是“最小可落地”的过渡架构：它不要求重构 Langfuse，不要求第三方 Agent 实现 `onto_eval_push`，也不推翻现有 Runtime Fact Builder。

---

## 1. 方案边界

### 1.1 本方案做什么

| 项 | 说明 |
|----|------|
| **PG 合一** | 一个 PostgreSQL 实例，两个 database：`onto_eval_fact`、`langfuse` |
| **服务独立** | `onto-eval-app` 与 `langfuse-web/worker` 仍独立部署、独立升级 |
| **观测归属调整** | 产品口径上 Langfuse 作为 onto-eval 的观测子模块，而不是第三方系统 |
| **物化路径优化** | 推荐逐步从 Agent Push 优先，调整为 eval 侧 `internal_sync` 优先 |
| **兼容现状** | 保留 `LANGFUSE_HOST`、Langfuse SDK、Webhook、Push、legacy pull 等现有能力 |

> **版本说明（v0.2）**：当前 Langfuse 为 **v3.173.0 OSS**。本方案的“PG 合一”仅指 **复用 Langfuse PostgreSQL 实例承载 `onto_eval_fact`**；Trace / Observation / Score 的 Raw 存储仍按 Langfuse v3 架构由 Langfuse 自身组件管理（通常包括 ClickHouse、Redis、对象存储与 worker），onto-eval 不直接接管这些组件。

### 1.2 本方案不做什么

| 不做 | 原因 |
|------|------|
| 不把 Langfuse 应用合并进 onto-eval app | 避免应用耦合与升级风险 |
| 不把两套表混进同一个 database/schema | Langfuse 与 onto-eval 各自有迁移体系 |
| 不取消 Runtime Fact Builder | 评测分析域仍需 `run_span_nodes` / `run_metrics` / `checkpoint_results` |
| 不立即隐藏 Langfuse UI | 产品品牌化可后续做 |
| 不要求第三方 Agent 实现 Push | Push 仅作为 first-party / onto-agent 快路径 |

---

## 2. 目标架构

```text
┌────────────────────────────── 部署与运维边界 ──────────────────────────────┐
│                                                                            │
│   ┌────────────────────┐                         ┌────────────────────┐   │
│   │ onto-eval app       │                         │ Langfuse app        │   │
│   │ API / UI / Builder  │                         │ Web / Worker / SDK  │   │
│   └─────────┬──────────┘                         └─────────┬──────────┘   │
│             │ DATABASE_URL                                  │ DATABASE_URL │
│             ▼                                               ▼              │
│   ┌────────────────────────────────────────────────────────────────────┐   │
│   │ PostgreSQL 单实例                                                   │   │
│   │                                                                    │   │
│   │   database: onto_eval_fact                                          │   │
│   │     - evaluation_tasks                                              │   │
│   │     - agent_runs                                                    │   │
│   │     - run_span_nodes                                                │   │
│   │     - run_metrics / checkpoint_results                              │   │
│   │                                                                    │   │
│   │   database: langfuse                                                │   │
│   │     - Langfuse 元数据 / 项目 / Key / migration 管理                    │   │
│   └────────────────────────────────────────────────────────────────────┘   │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────┘

Agent → Langfuse SDK → Langfuse app
Agent / eval → 推理与评测 → onto-eval app
onto-eval app → Langfuse API / Webhook / internal_sync → Runtime Fact Builder

注：Langfuse v3 的 trace / observation Raw 数据通常不在 PostgreSQL 中作为唯一主存储；本图只表达 PostgreSQL 运维合一，不表示 onto-eval 直接读取 Langfuse 内部数据库表。
```

### 2.1 与完整内嵌方案的区别

| 维度 | 方案 B：完整内嵌 | 方案 C：服务独立 + PG 合一 |
|------|------------------|---------------------------|
| Langfuse 应用 | 纳入 onto-eval 同栈部署 | **仍独立部署** |
| PG | 可同实例 | **同实例两个 database** |
| Compose / K8s | 需要合并或 profile 化 | **基本不动** |
| 运维改造 | 中等 | **最小** |
| 产品边界 | Langfuse 隐藏为内置模块 | 先在口径上归属 onto-eval |
| Agent Push | 可选 | **可选** |

---

## 3. PostgreSQL 合一策略

### 3.1 推荐形态

```text
postgres:5432
  ├── onto_eval_fact   # onto-eval Alembic 管理
  └── langfuse         # Langfuse 自己管理
```

**推荐使用一个 PostgreSQL 实例、两个 database，不推荐一个 database 下混 schema。**

原因：

- Langfuse 与 onto-eval 都有自己的 migration，database 隔离最稳。
- 权限可以分开：`onto_eval_user` 只访问 `onto_eval_fact`，`langfuse_user` 只访问 `langfuse`。
- 备份恢复可按 database 粒度执行。
- 后续如果要拆回独立 PG 实例，迁移边界清晰。

### 3.2 不推荐形态

| 形态 | 判断 | 原因 |
|------|------|------|
| 同 database、不同 schema | 不推荐 | migration、扩展、权限、备份恢复边界复杂 |
| 把 Langfuse 表迁入 `onto_eval_fact` | 禁止 | 破坏两个系统升级边界 |
| onto-eval 直接读写 Langfuse 内部表作为业务模型 | 禁止 | 绑定 Langfuse 私有 schema，升级风险高 |

### 3.3 初始化示例

```sql
CREATE DATABASE onto_eval_fact;
CREATE DATABASE langfuse;

CREATE USER onto_eval_user WITH PASSWORD '***';
CREATE USER langfuse_user WITH PASSWORD '***';

GRANT ALL PRIVILEGES ON DATABASE onto_eval_fact TO onto_eval_user;
GRANT ALL PRIVILEGES ON DATABASE langfuse TO langfuse_user;
```

连接串示例：

```env
# onto-eval
DATABASE_URL=postgresql+asyncpg://onto_eval_user:***@postgres:5432/onto_eval_fact

# Langfuse
DATABASE_URL=postgresql://langfuse_user:***@postgres:5432/langfuse
```

---

## 4. 物化层是否保留

**保留。**

PG 合一只改变数据库运维形态，不改变数据语义分层。Langfuse 保存的是 Raw Trace / Observation；onto-eval 需要的是评测分析事实。

| onto-eval 物化产物 | 是否仍需要 | 原因 |
|--------------------|------------|------|
| `agent_runs` | 是 | 评测 Run 锚点，关联 task / dataset / agent version |
| `run_span_nodes` | 是 | Span Tree、Structural Diff、过程点证据 |
| `run_metrics` | 是 | tokens、latency、call count 等报表口径 |
| `checkpoint_results` | 是 | 过程点合规批量结果 |
| `run_diffs` / 实验聚合 | 是 | 多版本对比与报告 |

设计范式仍然适用：

- Diff / Analytics 读 `run_span_nodes`、`run_metrics`、`agent_runs`。
- Langfuse 仅作为 Raw Trace 证据下钻。
- 不把全量 observation 正文长期双写进 `onto_eval_fact`。

---

## 5. 事实摄入路径调整

本方案建议分两步走。

### 5.1 阶段一：部署合一，不改业务路径

保持现状：

```text
Agent → Langfuse
Agent → 可选 Push → onto-eval /trace/ingest/push
Langfuse → 可选 Webhook → onto-eval /trace/ingest/webhook
onto-eval → Runtime Fact Builder → onto_eval_fact
```

这个阶段只验证：

- 两个 database 都在同一 PG 实例内可正常迁移。
- Langfuse 正常写 trace。
- onto-eval 正常写评测与物化事实。
- 现有 Push / Webhook / Pull 不受影响。

### 5.2 阶段二：eval 侧 internal_sync 优先

将默认摄入策略从“Agent Push 优先”改成：

```text
Pipeline 拿到 trace_id
  → 写 agent_run trace_sync_status = pending_sync
  → 入队 trace_sync_jobs
  → Worker 从 Langfuse API 读取 trace
  → Runtime Fact Builder
  → trace_sync_status = synced
```

Agent Push 保留为可选快路径：

```text
onto-agent / first-party Agent
  → 可继续 POST /trace/ingest/push
  → 提前完成 synced
```

第三方 Agent 不需要实现 Push。

### 5.3 配置建议

```env
# 阶段一：兼容现状
FACT_INGEST_MODE=push
TRACE_SYNC_WORKER_ENABLED=false

# 阶段二：推荐目标
FACT_INGEST_MODE=internal_sync
TRACE_SYNC_WORKER_ENABLED=true
```

如果暂不新增 `internal_sync` 枚举，也可以先复用：

```env
FACT_INGEST_MODE=legacy_pull
TRACE_SYNC_WORKER_ENABLED=true
```

但文档与代码命名上建议把 `legacy_pull` 重命名或别名化为 `internal_sync`，避免评审误解为“非生产路径”。

---

## 6. 对 Agent 的影响

### 6.1 第三方 Agent

最低接入要求：

| 能力 | 要求 |
|------|------|
| 推理接口 | 返回答案 |
| Trace | 写入 Langfuse，或返回 eval 可关联的 `trace_id` |
| Push | 不要求 |
| ST-3 registry | 不要求 |
| `agent.*` metadata | 推荐，不强制 |

### 6.2 onto-agent / first-party Agent

现有能力可保留：

- `onto_eval_push.py`
- `agent.*` attributes
- ST-3 `agent.step.id`

但这些应降级为 **L3 深度集成能力**，不是所有 Agent 的准入条件。

---

## 7. 工程部署工作量

### 7.1 最小部署改造

| 工作项 | 估算 |
|--------|------|
| 准备单 PG 实例与两个 database | 0.5 天 |
| 迁移 onto-eval PG 数据 | 0.5～1 天 |
| 迁移 Langfuse PG 数据 | 0.5～1 天 |
| 修改两边 `DATABASE_URL` | 0.5 天 |
| 备份与回滚脚本 | 0.5～1 天 |
| 冒烟验证 | 0.5 天 |

合计：**1～3 天**，主要取决于现有 Langfuse 数据量和允许停机窗口。

### 7.2 internal_sync 改造

| 工作项 | 估算 |
|--------|------|
| 新增/别名化 `FACT_INGEST_MODE=internal_sync` | 0.5 天 |
| Pipeline 推理结束后自动入队 `trace_sync_jobs` | 1～2 天 |
| Worker 生产化：重试、状态、日志、告警 | 1～2 天 |
| Trace 详情与 facets 默认优先 fact 层 | 1～2 天 |
| 回归测试 | 1～2 天 |

合计：**1～2 周**。

---

## 8. 运维与备份

### 8.1 备份策略

| 对象 | 备份方式 | 频率建议 |
|------|----------|----------|
| `onto_eval_fact` | `pg_dump` / PITR | 每日 + 发布前 |
| `langfuse` | `pg_dump` / PITR | 每日，保持 Langfuse 原策略 |
| PG 实例 WAL | 归档 | 生产建议开启 |
| Langfuse ClickHouse | 按 Langfuse v3 运维方案备份 / 快照 | 与 Langfuse 原策略一致 |
| Langfuse 附件/对象存储 | 独立备份 | 与 Langfuse 原策略一致 |

### 8.2 恢复策略

推荐支持两类恢复：

1. **整实例恢复**：灾难恢复，恢复同一时间点的两个 database。
2. **单 database 恢复**：Langfuse 或 onto-eval 单边误操作时，仅恢复对应 database。

### 8.3 监控项

| 指标 | 说明 |
|------|------|
| PG CPU / IO / connection | 两个服务共用实例，连接数需单独限额 |
| `onto_eval_fact` 表增长 | `run_span_nodes`、`run_metrics` |
| `langfuse` 表增长 | trace / observation 数据量 |
| `trace_sync_jobs` 堆积 | internal_sync 健康度 |
| `agent_runs.trace_sync_status` | `pending_sync` / `synced` / `failed` 分布 |

---

## 9. 风险与缓解

| 风险 | 说明 | 缓解 |
|------|------|------|
| PG 单点压力变大 | Langfuse trace 写入与 eval 分析共用实例 | 分 database、分用户、连接池限额；必要时后续拆分 |
| 误以为 PG 合一覆盖 Langfuse 全量数据 | Langfuse v3 Raw trace 仍由 ClickHouse / 对象存储等组件管理 | 文档明确“只复用 PG 实例，不接管 Langfuse 内部存储” |
| internal_sync 被误解为旧 legacy pull | 名称问题 | 增加 `internal_sync` 配置枚举与文档说明 |
| 直接查 Langfuse DB 表 | 绑定私有 schema | 只通过 Langfuse API 或稳定 Reader 抽象 |
| 备份恢复互相影响 | 单 PG 实例包含两个业务库 | 明确 database 粒度备份与恢复演练 |

---

## 10. 分阶段落地计划

| 阶段 | 目标 | 工作 |
|------|------|------|
| **C0** | 架构口径确认 | 本文评审；明确 Langfuse 是观测子模块但服务独立 |
| **C1** | PG 合一 | 单 PG 实例两个 database；迁移连接串与数据 |
| **C2** | 摄入降耦 | Agent Push 降为可选；internal_sync 成为默认主路径 |
| **C3** | 产品归属 | Trace 页面与文档弱化“第三方 Langfuse”表述 |
| **C4** | 深化演进 | 评估是否升级到完整内嵌方案 B，或继续保持 C |

---

## 11. 评审结论建议

本方案适合作为当前阶段的**首选过渡方案**：

- 它的部署工作量最小。
- 它不破坏现有 Langfuse 服务。
- 它能缓解“其他 Agent 必须适配 onto-eval Push/ST-3”的质疑。
- 它为后续完整内嵌方案保留路径。

一句话对外口径：

> **短期不合并 Langfuse 应用，只合并 PostgreSQL 运维底座；Langfuse 服务仍独立运行，但作为 onto-eval 的观测子模块使用。第三方 Agent 只需写 Trace，不需要实现 eval Push。**

---

## 12. 变更记录

| 日期 | 版本 | 说明 |
|------|------|------|
| 2026-06-07 | v0.2 | 补充 Langfuse v3.173.0 OSS：PG 合一仅复用 PostgreSQL 实例，ClickHouse/Redis/对象存储保持 Langfuse 栈管理 |
| 2026-06-05 | v0.1 | 初稿：Langfuse 独立部署、PG 合一、internal_sync 过渡路径 |

---

## 附录 A — P0/P1 执行 Runbook

本附录用于执行 **P0 现状确认与迁移准备**、**P1 PostgreSQL 合一迁移**。命令中的主机、端口、用户、密码请按生产环境替换。

### A.1 P0-01 确认 Langfuse v3.173.0 OSS 拓扑

在 Langfuse 部署机执行：

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
docker compose ps
```

确认至少记录：

| 项 | 记录 |
|----|------|
| Langfuse web 容器 / 镜像 | `langfuse/langfuse:3.173.0` 或等价镜像 |
| Langfuse worker 容器 / 镜像 | 按实际记录 |
| PostgreSQL 容器 / 实例 | `<langfuse-pg-host>:5432` |
| ClickHouse | v3 栈保持原样，不在本次迁移范围 |
| Redis | v3 栈保持原样，不在本次迁移范围 |
| MinIO/S3 | v3 栈保持原样，不在本次迁移范围 |

### A.2 P0-02 确认 PostgreSQL database 与权限

```bash
psql -h <langfuse-pg-host> -p 5432 -U <pg-admin-user> -d postgres -c '\l'
psql -h <langfuse-pg-host> -p 5432 -U <pg-admin-user> -d postgres -c '\du'
```

目标状态：

- 已有 Langfuse database（示例：`langfuse` 或 `postgres`，以实际部署为准）。
- 新增 `onto_eval_fact` database。
- 新增 `onto_eval_user`，只授权 `onto_eval_fact`。

### A.3 P0-03 盘点旧 onto-eval PostgreSQL

当前仓库 `docker-compose.yml` 默认使用独立 `onto-eval-postgres`：

```env
DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@postgres:5432/onto_eval_fact
POSTGRES_PORT=5433
```

在旧 onto-eval PG 上执行：

```bash
psql -h <old-eval-pg-host> -p <old-eval-pg-port> -U <old-eval-user> -d onto_eval_fact -c '\dt'
psql -h <old-eval-pg-host> -p <old-eval-pg-port> -U <old-eval-user> -d onto_eval_fact -c "select count(*) as evaluation_tasks from evaluation_tasks;"
psql -h <old-eval-pg-host> -p <old-eval-pg-port> -U <old-eval-user> -d onto_eval_fact -c "select count(*) as evaluation_results from evaluation_results;"
psql -h <old-eval-pg-host> -p <old-eval-pg-port> -U <old-eval-user> -d onto_eval_fact -c "select count(*) as agent_runs from agent_runs;"
psql -h <old-eval-pg-host> -p <old-eval-pg-port> -U <old-eval-user> -d onto_eval_fact -c "select count(*) as run_span_nodes from run_span_nodes;"
```

如部分表不存在，以实际 Alembic 版本为准记录。

### A.4 P0-04 停机窗口

迁移窗口内执行：

1. 暂停创建新评测任务。
2. 停止 `onto-eval-app`，避免旧库继续写入。
3. 保持 Langfuse 栈运行；本次迁移不停止 Langfuse，除非运维要求统一维护窗口。

示例：

```bash
docker stop onto-eval-app
```

### A.5 P0-05 回滚方案

回滚条件：

- 新库恢复失败。
- onto-eval 启动失败。
- 历史任务 / Run / Span 校验失败且无法快速修复。

回滚步骤：

1. 停止新 `onto-eval-app`。
2. 将 `DATABASE_URL` 改回旧 onto-eval PG。
3. 启动旧配置的 `onto-eval-app`。
4. 确认历史任务与新增任务写回旧库。

回滚前不得删除旧 `onto_eval_fact` database 或旧 PG volume。

### A.6 P1-01 备份旧 onto-eval 数据库

```bash
mkdir -p backups
pg_dump -Fc \
  -h <old-eval-pg-host> \
  -p <old-eval-pg-port> \
  -U <old-eval-user> \
  -d onto_eval_fact \
  -f backups/onto_eval_fact_$(date +%Y%m%d_%H%M%S).dump
```

校验 dump：

```bash
pg_restore -l backups/onto_eval_fact_*.dump | head
```

### A.7 P1-02 / P1-03 创建目标 database 与账号

```sql
CREATE DATABASE onto_eval_fact;
CREATE USER onto_eval_user WITH PASSWORD '<strong-password>';
GRANT ALL PRIVILEGES ON DATABASE onto_eval_fact TO onto_eval_user;
```

PostgreSQL 15+ 如需 schema 权限：

```sql
\c onto_eval_fact
GRANT ALL ON SCHEMA public TO onto_eval_user;
ALTER SCHEMA public OWNER TO onto_eval_user;
```

### A.8 P1-04 恢复数据到 Langfuse PG 实例

```bash
pg_restore \
  -h <langfuse-pg-host> \
  -p 5432 \
  -U onto_eval_user \
  -d onto_eval_fact \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  backups/onto_eval_fact_<timestamp>.dump
```

如目标库为空，也可去掉 `--clean --if-exists`。

### A.9 P1-05 修改 onto-eval DATABASE_URL

`.env`：

```env
DATABASE_URL=postgresql+asyncpg://onto_eval_user:<strong-password>@<langfuse-pg-host>:5432/onto_eval_fact
POSTGRES_DB=onto_eval_fact
```

如果继续使用现有 `docker-compose.yml`，需要注意当前 compose 会覆盖容器内 `DATABASE_URL`：

```yaml
DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER:-onto_eval}:${POSTGRES_PASSWORD:-onto_eval_dev}@postgres:5432/${POSTGRES_DB:-onto_eval_fact}
```

因此生产切换时二选一：

1. 使用外部 PG override 文件覆盖 `DATABASE_URL`。
2. 修改 compose，允许 `DATABASE_URL` 从 `.env` 直接注入。

推荐新增 override：

```yaml
# docker-compose.pg-shared.override.yml
services:
  app:
    depends_on: []
    environment:
      - DATABASE_URL=${DATABASE_URL}
```

启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.pg-shared.override.yml up -d app
```

### A.10 P1-06 执行 Alembic

```bash
docker exec onto-eval-app sh -c 'cd /app/backend && alembic upgrade head'
docker exec onto-eval-app sh -c 'cd /app/backend && alembic current'
```

### A.11 P1-07 历史数据验收 SQL

```sql
select count(*) from evaluation_tasks;
select count(*) from evaluation_results;
select count(*) from agents;
select count(*) from agent_runs;
select count(*) from run_span_nodes;
select count(*) from run_metrics;
select count(*) from checkpoint_results;

select id, trace_id, trace_sync_status, created_at
from agent_runs
order by created_at desc
limit 10;

select agent_run_id, count(*) as spans
from run_span_nodes
group by agent_run_id
order by spans desc
limit 10;
```

验收通过标准：

- 历史评测任务可查。
- 历史结果可查。
- `agent_runs.trace_id` 未丢失。
- `run_span_nodes`、`run_metrics`、`checkpoint_results` 数据存在。
- Langfuse 原 `langfuse` database 未做结构修改。

### A.12 默认配置下的执行命令示例

以下命令假设均使用默认值：

| 项 | 默认值 |
|----|--------|
| 旧 onto-eval PG Host | `127.0.0.1` |
| 旧 onto-eval PG Port | `5433` |
| 旧 onto-eval DB | `onto_eval_fact` |
| 旧 onto-eval User / Password | `onto_eval` / `onto_eval_dev` |
| Langfuse PG Host | `<langfuse-pg-host>`，若在 Langfuse compose 网络内通常为 `postgres` |
| Langfuse PG Port | `5432` |
| Langfuse PG Admin User / Password | `postgres` / `postgres` |
| 新 onto-eval DB | `onto_eval_fact` |
| 新 onto-eval User / Password | `onto_eval` / `onto_eval_dev` |

> **生产提醒**：默认密码仅适合受控内网/测试环境。若用于生产，应先改强密码，再更新 `DATABASE_URL`。

#### A.12.1 暂停 onto-eval 写入

```bash
docker stop onto-eval-app
```

#### A.12.2 备份旧 onto-eval PG

```bash
mkdir -p backups
PGPASSWORD=onto_eval_dev pg_dump -Fc \
  -h 127.0.0.1 \
  -p 5433 \
  -U onto_eval \
  -d onto_eval_fact \
  -f backups/onto_eval_fact_$(date +%Y%m%d_%H%M%S).dump

pg_restore -l backups/onto_eval_fact_*.dump | head
```

#### A.12.3 在 Langfuse PG 创建 `onto_eval_fact`

如果命令在 Langfuse PostgreSQL 容器内执行：

```bash
docker exec -it <langfuse-postgres-container> psql -U postgres -d postgres
```

进入 `psql` 后执行：

```sql
CREATE DATABASE onto_eval_fact;
CREATE USER onto_eval WITH PASSWORD 'onto_eval_dev';
GRANT ALL PRIVILEGES ON DATABASE onto_eval_fact TO onto_eval;
\c onto_eval_fact
GRANT ALL ON SCHEMA public TO onto_eval;
ALTER SCHEMA public OWNER TO onto_eval;
```

如果 `onto_eval` 用户已存在，则改用：

```sql
ALTER USER onto_eval WITH PASSWORD 'onto_eval_dev';
GRANT ALL PRIVILEGES ON DATABASE onto_eval_fact TO onto_eval;
\c onto_eval_fact
GRANT ALL ON SCHEMA public TO onto_eval;
ALTER SCHEMA public OWNER TO onto_eval;
```

#### A.12.4 恢复旧数据到 Langfuse PG

如果 Langfuse PG 从宿主机可访问：

```bash
PGPASSWORD=onto_eval_dev pg_restore \
  -h <langfuse-pg-host> \
  -p 5432 \
  -U onto_eval \
  -d onto_eval_fact \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  backups/onto_eval_fact_<timestamp>.dump
```

如果 Langfuse PG 只在 Docker 网络内可访问，建议将 dump 拷入 PG 容器或使用临时 `postgres` 客户端容器执行恢复。

#### A.12.5 修改 onto-eval `.env`

```env
DATABASE_URL=postgresql+asyncpg://onto_eval:onto_eval_dev@<langfuse-pg-host>:5432/onto_eval_fact
POSTGRES_USER=onto_eval
POSTGRES_PASSWORD=onto_eval_dev
POSTGRES_DB=onto_eval_fact
```

若 `onto-eval-app` 与 Langfuse PG 不在同一 Docker 网络，`<langfuse-pg-host>` 应填写宿主机可达地址或内网 IP。

#### A.12.6 使用 override 启动 onto-eval

当前 `docker-compose.yml` 会把容器内 `DATABASE_URL` 固定到 `postgres` 服务，因此默认迁移到 Langfuse PG 后，需要新增 override：

```yaml
# docker-compose.pg-shared.override.yml
services:
  app:
    depends_on: []
    environment:
      - DATABASE_URL=${DATABASE_URL}
```

启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.pg-shared.override.yml up -d app
```

#### A.12.7 Alembic 与验收

```bash
docker exec onto-eval-app sh -c 'cd /app/backend && alembic upgrade head'
docker exec onto-eval-app sh -c 'cd /app/backend && alembic current'

PGPASSWORD=onto_eval_dev psql \
  -h <langfuse-pg-host> \
  -p 5432 \
  -U onto_eval \
  -d onto_eval_fact \
  -c "select count(*) as agent_runs from agent_runs;"
```

继续执行 A.11 的历史数据验收 SQL。
