# M1 Langfuse / Trace 测试

## 安装

```powershell
cd onto-eval\backend
pip install -r requirements-dev.txt
```

## 运行（默认：Mock，无需 Langfuse）

```powershell
cd onto-eval\backend
pytest tests -v -m "not integration"
```

或一键（Mock + 若存在配置文件则联调）：

```powershell
.\scripts\run_m1_tests.ps1
```

## 多项目联调（langfuse.projects.json，推荐）

1. 在 `onto-eval/` 根目录创建 `langfuse.projects.json`（可参考 `langfuse.projects.example.json`）
2. 填入 **3 个** Langfuse 项目各自的 `id` / `public_key` / `secret_key`
3. `.env` 配置 `LANGFUSE_HOST=...`（无需再设单项目 PUBLIC_KEY）

存在该文件时，**自动化脚本会自动运行** `test_langfuse_projects_live.py`，无需 `LANGFUSE_INTEGRATION=1`。

```powershell
cd onto-eval\backend
pytest tests/test_langfuse_projects_live.py -v
```

跳过联调：`$env:LANGFUSE_SKIP_LIVE = "1"`

调整期望项目数：`$env:LANGFUSE_EXPECT_PROJECTS = "3"`

## 可选：仅单 Key 联调（legacy）

```powershell
$env:LANGFUSE_INTEGRATION = "1"
# 且不要 langfuse.projects.json
pytest tests/test_integration_langfuse.py -m integration -v
```

## 用例覆盖

| 文件 | 内容 |
|------|------|
| `test_langfuse_project_registry.py` | 多项目配置加载、缺失文件回退、重复 id |
| `test_langfuse_project_meta.py` | Langfuse API 响应解析、名称同步/回退 |
| `test_langfuse_tracing.py` | v2.1 metadata / tags / session_id 规范 |
| `test_trace_filters.py` | BFF 筛选逻辑 |
| `test_trace_facets.py` | 问题/Agent/版本聚合 |
| `test_trace_routes.py` | `/api/trace/*` 契约（Mock） |
| `test_trace_sync_routes.py` | Trace Sync HTTP API（含 **drain 鉴权**） |
| `test_fact_ingest.py` | Push/Webhook 事实摄入 |
| `test_config_url_matrix.py` | DB URL 识别与 async/sync 驱动转换矩阵（P0） |
| `test_langfuse_projects_live.py` | **langfuse.projects.json 三项目联调（自动）** |
| `test_integration_langfuse.py` | 单 Key legacy 联调（可选） |
| `test_p2_models.py` | P2 ORM / snapshot 工具（SQLite 内存） |
| `test_span_tree_builder.py` | Span Tree 构建（C-02） |
| `test_p2_fact_pipeline.py` | Ingest → Builder → Span + run_metrics（I-02） |
| `test_alembic_pg_smoke.py` | **Alembic × PostgreSQL smoke**（需 PG + `PG_ALEMBIC_SMOKE=1`） |
| `test_migrate_sqlite_to_pg.py` | A-06 迁移服务单测（SQLite 内存） |
| `test_migrate_pg_smoke.py` | **A-06 SQLite→PG 迁移集成 smoke**（需 PG + `PG_ALEMBIC_SMOKE=1`） |
| `test_pg_integration_smoke.py` | **E2E**：Alembic → 迁移 → `v_run_analytics`（需 PG + `PG_ALEMBIC_SMOKE=1`） |

## P2：Alembic PostgreSQL smoke test

前置：PostgreSQL 可连（如 `docker compose up -d postgres`）。

```powershell
cd onto-eval\backend
$env:PG_ALEMBIC_SMOKE = "1"
# 可选：与 docker-compose 默认一致
# $env:POSTGRES_USER = "onto_eval"
# $env:POSTGRES_PASSWORD = "onto_eval_dev"
pytest tests/test_alembic_pg_smoke.py -v
```

默认 CI / 日常 pytest 会 **skip**（未设 `PG_ALEMBIC_SMOKE=1`）。

## P2：A-06 SQLite → PostgreSQL 迁移 smoke

与 Alembic smoke 共用 `PG_ALEMBIC_SMOKE=1` 与 Postgres 连接环境变量。

```powershell
cd onto-eval\backend
$env:PG_ALEMBIC_SMOKE = "1"
pytest tests/test_migrate_pg_smoke.py -v
```

覆盖：全表迁移、行数对账、抽样 `agent_run`、重复迁移幂等、`v_run_analytics` 宽表断言。

一键运行（Windows，自动 `docker compose up -d postgres`）：

```powershell
cd onto-eval\backend
.\scripts\run_pg_smoke.ps1
```

脚本会设置 `PG_SMOKE_REQUIRE=1`：PostgreSQL 未就绪时 pytest **失败退出**（非静默 skip）。

Linux / macOS：`./scripts/run_pg_smoke.sh`

E2E 集成（Alembic + 编排表 + 迁移 + 读视图）：

```powershell
$env:PG_ALEMBIC_SMOKE = "1"
pytest tests/test_pg_integration_smoke.py -v
```

日常 CI（无 PG）：

```powershell
pytest tests -v -m "not integration and not postgresql"
```

## P3：Diff / 版本实验 / 驾驶舱 / 场景子步骤（全量回归）

无需真实 Langfuse；使用内存 SQLite + Mock。

```powershell
cd onto-eval\backend
pytest -m p3 -q
```

verbose 与失败详情：

```powershell
pytest -m p3 -v --tb=short
```

**覆盖范围（`pytest -m p3`）**：实验服务与路由、对比路由、Diff 计算器与摘要、过程点结果聚合、版本驾驶舱（`version_cockpit_service`）、场景子步骤评测（`scenario_eval_service` / `scenario_routes`）、`test_p3_integration.py` 端到端流水线等（当前 **34** 项）。

与 M6 相关的增量用例：

| 文件 | 内容 |
|------|------|
| `test_version_cockpit_service.py` | L1/L2/L3 驾驶舱读模型 |
| `test_scenario_eval_service.py` | `corridor_defense` 8 步打分 |
| `test_scenario_routes.py` | `/api/scenario-eval/*` |
| `test_experiment_routes.py::test_task_version_cockpit` | `GET /experiments/task/{id}/cockpit` |
