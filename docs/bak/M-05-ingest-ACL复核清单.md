# M-05 ingest 端点 ACL 复核清单

> ⚠️ **本文档已归档（v1.23）**：Webhook/Push 摄入端点已全部移除，`FACT_INGEST_ALLOW_CIDRS` 配置项已取消，`ingest_acl.py` 已删除，nginx ingest ACL location 已移除。本文档仅供历史参考。

> 对应架构说明书 TD-SEC-01；v1.16 产品决策：**ingest 不设 HTTP Token**，依赖网络隔离 + 反向代理 ACL。
> v1.23 起：ingest 端点已不存在，此复核清单不再适用。

## 1. 复核项

| # | 项 | 位置 | 期望 |
|---|-----|------|------|
| 1 | Nginx 私网白名单 | `nginx.conf` `location ^~ /api/trace/ingest/` | 仅 RFC1918 + 127.0.0.1 |
| 2 | 应用层 CIDR（可选） | `FACT_INGEST_ALLOW_CIDRS` + `services/ingest_acl.py` | 生产设置与 nginx 一致 |
| 3 | 公网探测 | 运维 | 公网 IP `curl POST /api/trace/ingest/push` 应 **403** |
| 4 | 内网 Push | onto-agent | `ONTO_EVAL_BASE_URL` 经内网可达；Push 200 |
| 5 | UI 与 ingest 分离 | 部署 | 若必须公网暴露 UI，ingest 走独立内网 listener 或 VPN |

## 2. 环境变量

```bash
# 逗号分隔 CIDR；留空则应用层不拦截（仅依赖 nginx）
FACT_INGEST_ALLOW_CIDRS=10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8
```

## 3. 验证命令

```bash
# 自动化（随 p25-gate 执行）
cd onto-eval/backend
python -m pytest tests/test_m_features.py::test_ingest_acl_denies_when_cidr_configured \
  tests/test_m_features.py::test_ingest_acl_allows_private_ip -q

# 应被拒绝（若从公网或不在白名单）
curl -X POST http://<host>/api/trace/ingest/push -H "Content-Type: application/json" -d "{}"
```

## 4. 签收

| 角色 | 日期 | 结果 |
|------|------|------|
| 运维 | | ☐ |
| 安全 | | ☐ |
| eval 研发 | 2026-05-29 | nginx + ingest_acl 代码 ✅ |
| 自动化验收 | 2026-06-02 | `scripts/p25-gate.ps1` M-05 pytest + nginx 检查 ✅ |
