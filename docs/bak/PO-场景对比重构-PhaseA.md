# PO：场景对比重构 — P1 完成

## P1 交付项

| ID | 内容 | 状态 |
|----|------|------|
| P1-1 | 单任务场景矩阵 API + `ScenarioEvalPanel` 多列 | ✅ |
| P1-2 | L2「推理链 / Span 树对比」→ `RunComparePanel` | ✅ |
| P1-3 | Outcome 驾驶舱 API `deprecated=True` | ✅ |

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/scenario-eval/tasks/{id}/matrix` | 单任务 question×agent 场景矩阵 |
| POST | `/api/scenario-eval/from-tasks` | 跨任务场景矩阵（Phase A） |
| POST | `/api/comparisons/compare` | Span 树下钻前置（scenario_matrix scope） |
| ~~POST~~ | `/api/comparisons/from-tasks` | **deprecated** Outcome 驾驶舱 |
| ~~GET~~ | `/api/experiments/task/{id}/cockpit` | **deprecated** |

## 前端

- `EvalDetailModal` Tab：**质量指标** | **运行效能** | **场景子步骤**（跨任务对比仍在评估历史入口）
- `RuntimePerformanceReport`：任务汇总 + 单题 Run 明细与推理链下钻；同题多轮对比（条件展示）（`GET /performance-report`、`GET /run-step-compare`）
- ~~`ExperimentReportPanel` / 版本实验报告 Tab~~ **已移除**（2026-05-30）；Outcome 摘要见跨任务 `outcome_summaries`
- `CrossTaskOutcomeSummary`：评估历史 → 场景/版本对比 顶部任务级质量/效率表
- `ScenarioMatrixView`：L1/L2 矩阵 + Δ 列 + 判分证据 + semantic/structural diff + Span 对比
- `ScenarioEvalPanel`：消费 `/matrix`（含 `diff_highlight` per question）
- `CrossTaskScenarioMatrix`：复用 `ScenarioMatrixView`
- 任务详情 `/detail` 不再返回 `experiment_report`（diff 迁入 matrix API）

## P2 交付项

| ID | 内容 | 状态 |
|----|------|------|
| P2-1 | 质量指标 Tab 过程点区 `CheckpointRunCompare` 一键 Span 树 | ✅ |
| P2-2 | 删除 `VersionCompareWorkbench` + E2E cockpit 死代码 | ✅ |

## 前端（P2 新增）

- `CheckpointRunCompare`：同题 2+ Run → 基线/对比列选择 → 一键推理链
- `utils/runCompare.ts`：对比 API 共用封装

## 样例 Demo：子步骤 Trace 下钻（corridor_defense）

- API：`GET /api/scenario-eval/runs/{agent_run_id}/steps-detail?scenario_key=corridor_defense`
- 前端：任务详情 → **场景子步骤** → L2 矩阵点击单元格 → **子步骤 Trace 下钻**（input/output + Langfuse 链接）
- 样例题关键词：`带状防御`、`防御方案`、`快艇`（已写入模板 `question_match`）

## 测试

```bash
cd backend && python -m pytest -m p3 -q
cd backend && python -m pytest tests/test_scenario_step_trace.py -q
cd backend && python -m pytest tests/test_run_performance_report.py tests/test_task_performance_api.py tests/test_performance_report_routes.py -q
cd e2e && npx playwright test p1-scenario-span p2-checkpoint-compare p3-cross-task-compare
```
