# P2 ADR：数据库分库策略（A-02）

**状态**：已采纳（P2-A）  
**日期**：2026-05-23

## 背景

P2 目标是将 **Execution Fact Layer**（`agent_runs` 扩展、`run_span_nodes`、`run_metrics`、`checkpoint_results`）物化到 **PostgreSQL**。编排表（`evaluation_tasks`、`agents`、`datasets` 等）当前在 SQLite。

## 决策

### P2-A～P2-B（当前阶段）

- **单 `DATABASE_URL`**：开发可继续 SQLite；启用 P2 时切换为 **同一 PostgreSQL 实例** 承载编排表 + 事实表。
- **理由**：降低双库联调成本；Alembic 与 ORM 已统一 `Base.metadata`；CI 仍用 SQLite in-memory 跑单测（B-06）。
- **SQLite 开发**：`init_db()` 保留 PRAGMA 增量迁移；新表由 `create_all` 创建。

### P2 后期（可选）

- 事实表与编排表若需分库，再引入 `FACT_DATABASE_URL`；**不在 P2-A 实现**。

## 后果

- 切 PG 时需一次性迁移编排数据（A-06 可选脚本）或从新环境起步。
- Analytics 读路径（G-*）只需面对单一 PG，简化 P2 垂直切片演示。

## 验收

- [x] 文档结论明确（本文）
- [ ] 生产环境连接串与备份策略（运维，P2-B）
