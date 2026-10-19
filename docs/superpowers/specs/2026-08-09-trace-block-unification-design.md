# Trace 展示公共模块设计

> 日期：2026-08-09
> 状态：已与用户对齐方案 B，待审查

## 背景与动机

评估历史详情页有 3 个右侧抽屉组件各自实现 trace 展示，逻辑碎片化：

| 抽屉 | 文件 | trace 展示现状 |
|---|---|---|
| 指标 | `EvalDetailPage.tsx:1655-1671` | 内联，有 `langfuse_trace_url` + `trace_id`，有"仅 id 无 url"fallback，无复制按钮 |
| 场景交付 | `ScenarioDeliveryView.tsx:754-760` | 内联，只有 `traceUrl` 链接，**无 trace_id**，无 fallback，无复制 |
| 子步骤 | `ScenarioStepDrilldown.tsx:302-334,457-486` | 本地 `LangfuseLink` 组件（带复制），**无 trace_id**，两处重复展示 |

同时 `langfuseHost` 的拉取逻辑（`fetch(/api/trace/health)` + 缓存 + 构造 URL）在 4+ 处重复：`CrossTaskQuestionRuntime:67-91`、`RunComparePanel:62-111`、`RunSpanTreePanel`、`LangfuseTraceExplore:442,855`。

诉求：抽公共模块，让 trace 展示（链接 + trace id + 复制 + fallback）统一，支撑指标/场景交付/子步骤/失败题目各抽屉复用。

## 关键发现（探索阶段）

### 后端 trace URL 真实格式

`backend/services/langfuse_link_service.py:83-85`：

```python
if langfuse_project_id:
    trace_url = f"{effective_host}/project/{langfuse_project_id}/traces/{trace_id}"
else:
    trace_url = f"{effective_host}/trace/{trace_id}"
```

前端 `CrossTaskQuestionRuntime:91` 的简化构造 `${host}/trace/${id}` **漏了 project 段**，在有 project_id 的场景下 fallback 链接会 404——这是已知 bug，本次统一治理。

### 后端场景交付 `langfuse.trace_id` 已存在

- `scenario_step_trace_service.py:1596` 的 `rep["langfuse"] = run_links`，`run_links` 来自 `build_langfuse_trace_urls` → `build_trace_urls_sync`
- `build_trace_urls_sync` 返回的 dict 含 `trace_id`（`langfuse_link_service.py:77`）
- 普通模式（`score_run_against_scenario`）：`cell.langfuse` 是完整 dict，**已含 `trace_id`**
- checkpoint 模式（`_score_checkpoint_cell:261`）：`cell.langfuse = None`，无 trace（过程一致性走另一套，符合预期）

**结论：后端零改动**。前端 `ScenarioDeliveryView:29` 接口定义 `langfuse?: { trace_url?: string | null }` 类型不全，补 `trace_id` 字段即可。

## 方案

抽 3 个公共件 + 迁移调用方。范围中等，治理真实的 host 重复与 fallback 能力不统一。

### 新建文件

| 文件 | 职责 |
|---|---|
| `frontend/src/components/DrawerShell.tsx` | 公共抽屉外壳（mask + 容器 + 头部 + 关闭 + 内容区） |
| `frontend/src/components/LangfuseTraceBlock.tsx` | trace 展示块（链接 + trace id + 复制 + fallback） |
| `frontend/src/utils/useLangfuseHost.ts` | 封装 `/api/trace/health` 拉取 + 缓存，返回 host 字符串 |

### `DrawerShell` 接口

```tsx
interface DrawerShellProps {
  title: React.ReactNode   // 头部标题，支持节点（带状态徽章等）
  onClose: () => void
  children: React.ReactNode
  width?: number           // 默认 440，max-width 92vw 不变
}
```

渲染 `.edp-drawer-mask.show` + `.edp-drawer.show` + `.edp-dh`(标题 + ×) + `.edp-db`(children)。
现有 3 个抽屉手写的这段结构全部替换为 `<DrawerShell>`：
- `EvalDetailPage.tsx:1631-1638`（指标抽屉外壳）
- `ScenarioDeliveryView.tsx:639-646`（场景交付抽屉外壳）
- `ProcessConsistencyView.tsx:336-343`（过程一致性抽屉外壳）

### `LangfuseTraceBlock` 接口

```tsx
interface LangfuseTraceBlockProps {
  traceUrl?: string | null       // 完整 URL，优先用
  traceId?: string | null        // 用于展示 + fallback 构造
  observationUrl?: string | null // 可选：观测级链接（子步骤场景有）
  langfuseProjectId?: string | null  // fallback 构造时的 project 段
  label?: string                 // 默认"在 Langfuse 中查看完整 Trace ↗"
  className?: string             // 外层，默认 "edp-sect"
}
```

内部逻辑：
1. `const host = useLangfuseHost()`
2. fallback 构造（对齐后端 `langfuse_link_service.py:83-85`）：
   ```ts
   const finalUrl = traceUrl || (traceId && host
     ? langfuseProjectId
       ? `${host}/project/${langfuseProjectId}/traces/${traceId}`
       : `${host}/trace/${traceId}`
     : null)
   ```
3. 四分支渲染：
   - `finalUrl && traceId` → 链接 + `Trace ID: <code>` + 复制 ID 按钮
   - `finalUrl && !traceId` → 链接 + 复制链接按钮
   - `!finalUrl && traceId` → `Trace ID: <code>（Langfuse host 未配置，可复制此 ID 到 Langfuse 搜索）`
   - `!finalUrl && !traceId` → 返回 null（不渲染）
4. 吸收 `ScenarioStepDrilldown:302-334` 本地 `LangfuseLink` 的复制按钮逻辑，删除原组件

### `useLangfuseHost` 接口

```ts
function useLangfuseHost(): string  // 返回 host，未加载完返回 ''
```

内部 `useState('')` + `useEffect` fetch `/api/trace/health` → `setHost(data.host)`，带 `alive` flag 防竞态。
替换 4 处重复：`CrossTaskQuestionRuntime:67-91`、`RunComparePanel:62-111`、`RunSpanTreePanel`、`LangfuseTraceExplore:442,855`。

放置位置：`frontend/src/utils/useLangfuseHost.ts`。虽打破 `utils/` 纯函数惯例，但保持扁平结构，且与 `langfuseReadiness.ts` 语义聚类。

## 数据流

```
后端场景交付 API
  └─ rep.langfuse = run_links (build_trace_urls_sync 返回的 dict)
     └─ { trace_url, trace_id, langfuse_host, langfuse_project_id, observation_url }
        ↓
ScenarioDeliveryView.openDrawer (418行)
  └─ drawer.traceId = rd?.langfuse?.trace_id || cell.langfuse?.trace_id || null  ← 新增
  └─ drawer.langfuseProjectId = rd?.langfuse?.langfuse_project_id || null        ← 新增
     ↓
<LangfuseTraceBlock
  traceUrl={drawer.traceUrl}
  traceId={drawer.traceId}
  langfuseProjectId={drawer.langfuseProjectId}
/>
  └─ useLangfuseHost() → host
     └─ finalUrl = traceUrl || (traceId && host ? `${host}/project/${pid}/traces/${id}` : `${host}/trace/${id}`)
```

指标/子步骤抽屉数据流不变，把字段传给 `LangfuseTraceBlock`。

## 各抽屉调用方式

| 抽屉 | 调用 |
|---|---|
| 指标（EvalDetailPage） | `<LangfuseTraceBlock traceUrl={result.langfuse_trace_url} traceId={result.trace_id} />` 替换 `1655-1671` 内联 |
| 场景交付（ScenarioDeliveryView） | `<LangfuseTraceBlock traceUrl={drawer.traceUrl} traceId={drawer.traceId} langfuseProjectId={drawer.langfuseProjectId} />` 替换 `754-760`；`DrawerState` 加 `traceId` + `langfuseProjectId` 字段 |
| 子步骤（ScenarioStepDrilldown） | `<LangfuseTraceBlock traceUrl={data.langfuse?.trace_url} traceId={data.langfuse?.trace_id} observationUrl={expandedStep?.trace_binding?.observation_url} />` 替换 `457-486` 两处重复 + 删本地 `LangfuseLink`(302-334) |
| 过程一致性（ProcessConsistencyView） | checkpoint 模式 `langfuse=None`，本次不动；如后续补 trace，直接 `<LangfuseTraceBlock>` |
| 失败题目 | 复用指标抽屉，无独立组件，自动覆盖 |

## 样式（遵循 onto-eval-ucd）

- **`DrawerShell`**：复用 `.edp-drawer-mask` / `.edp-drawer` / `.edp-dh` / `.edp-db`（`index.css:6814-6828` 已定义），零新 CSS。`width` prop 通过 inline `style={{ width }}` 覆盖默认 440px。
- **`LangfuseTraceBlock`**：外层 `<div className="edp-sect">`，标题 `<h4>🔗 Langfuse Trace</h4>`。链接 `var(--edp-primary)`，trace id `<code>` + `var(--edp-text3)`，复制按钮 `.edp-btn`。
- **硬编码 hex 清零**：`ScenarioStepDrilldown:416,419,452,464,475` 现有 `#666`/`#dc3545`/`#856404`/`#f0f4ff` 迁移时替换为 `var(--edp-text3)`/`var(--edp-red)`/`var(--edp-amber)`/`var(--edp-blue-soft)`。
- **删除旧类**：`ScenarioStepDrilldown` 本地 `LangfuseLink` 用的 `btn btn-secondary` 旧类替换为 `.edp-btn`。

## 错误处理

- `useLangfuseHost`：fetch 失败 → `host=''`，`LangfuseTraceBlock` 走"仅 traceId"分支，不抛错（与指标抽屉现有行为一致）
- `LangfuseTraceBlock`：`traceUrl` 和 `traceId` 都空 → 返回 null，不渲染整块（与场景交付抽屉现有 `{traceUrl && ...}` 行为一致）
- `DrawerShell`：`children` 为空数组/null → 渲染 `.edp-empty` 占位

## 测试

### 后端
无改动（`trace_id` 已在 `rep.langfuse` dict 中）。现有 `test_scenario_step_trace.py` 已覆盖 `build_langfuse_trace_urls`。

### 前端
- `LangfuseTraceBlock` 四分支渲染快照测试：给定不同 prop 组合（url+id / 仅 url / 仅 id / 都无），断言渲染出的链接/trace id/提示文案/null
- `LangfuseTraceBlock` fallback 构造测试：给定 `traceId` + mock host + 有/无 `langfuseProjectId`，断言构造的 URL 格式正确（含/不含 `/project/{pid}/traces/` 段）
- `useLangfuseHost` fetch mock 测试：成功返回 host、fetch 失败返回 ''

## 迁移顺序（每步可独立验证）

1. 新建 `useLangfuseHost.ts` → 不动调用方
2. 新建 `DrawerShell.tsx` + `LangfuseTraceBlock.tsx` → 不动调用方
3. 迁移指标抽屉（`EvalDetailPage:1655-1671`）→ 验证 trace 展示不变
4. 迁移场景交付抽屉（`ScenarioDeliveryView:754-760` + `DrawerState` 加 `traceId`/`langfuseProjectId` + `openDrawer:418` 补字段）→ 验证 trace 链接 + trace id 都展示
5. 迁移子步骤抽屉（`ScenarioStepDrilldown:457-486` + 删本地 `LangfuseLink` + hex 清零）→ 验证复制按钮、链接、trace id 都在
6. 替换 4 处 `langfuseHost` 重复拉取为 `useLangfuseHost` → 验证 `CrossTaskQuestionRuntime`、`RunComparePanel`、`RunSpanTreePanel`、`LangfuseTraceExplore` trace 链接不变
7. 迁移 3 个抽屉外壳为 `DrawerShell` → 验证视觉无变化

步骤 3-5 可并行；步骤 6-7 相互独立也可并行。建议 3-5 先于 6-7，核心诉求是 trace 展示统一。

## 不做（YAGNI）

- 后端数据结构统一为 `LangfuseLinks`（各场景差异是历史演进，强行统一引入回归风险）
- 过程一致性抽屉补 trace（checkpoint 模式 `langfuse=None`，走另一套，本次不扩）
- 抽屉内业务内容统一（各抽屉的评分/分析/IO 预览等保持现状，只统一 trace 展示与外壳）
