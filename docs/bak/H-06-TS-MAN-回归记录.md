# H-06 TS-MAN 手工回归执行记录

> 对应 [手工检查清单-P1.5-TraceSync-Postman-curl.md](./手工检查清单-P1.5-TraceSync-Postman-curl.md)  
> 自动化替代项见 `backend/scripts/run_h06_regression.ps1`

## 执行摘要（2026-05-29）

| TS-MAN | 项 | 自动化 | 结果 |
|--------|-----|--------|------|
| TS-MAN-01 | ~~Push 摄入物化~~（v1.23 已移除） | ~~`test_g02_push_e2e.py` + `verify_push_e2e.py`~~ | — |
| TS-MAN-01 | Agent 注册预检 | `test_agent_probe_service.py` | ✅ |
| TS-MAN-01 | Token 契约 / Builder 细分 | `test_runtime_fact_builder.py`, `test_eval_metric_sync_service.py` | ✅ |
| TS-MAN-01 | workspace 404 JSON | `test_workspace_http_routes.py`, `test_routes_stream.py` | ✅ |
| TS-MAN-02 | Trace Sync Worker | `test_trace_sync_worker.py` | ✅（需 `TRACE_SYNC_WORKER_ENABLED=true`） |
| TS-MAN-03 | 任务详情 UI 三列 Token | 浏览器手工 | ☐ 待 UI 复测 |
| TS-MAN-04 | Facets / PG 读路径 | `test_analytics_read_path.py` | ✅ |

## 复跑命令

```powershell
cd onto-eval/backend
./scripts/run_h06_regression.ps1
```

```bash
cd onto-eval/backend
# v1.23：verify_push_e2e.py 已删除，改用 pytest 验证 Trace Sync Worker
python -m pytest tests/test_trace_sync_worker.py -q
```
