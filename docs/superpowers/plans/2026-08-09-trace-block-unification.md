# Trace 展示公共模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽公共 `DrawerShell` + `LangfuseTraceBlock` + `useLangfuseHost`，统一指标/场景交付/子步骤抽屉的 trace 展示，治理 3 处 langfuseHost 重复拉取。

**Architecture:** 新建 3 个公共件，不动后端（`rep.langfuse` 已含 `trace_id`）。各抽屉迁移为引用公共件，删除本地 `LangfuseLink` 组件与内联 trace 展示。fallback URL 构造对齐后端 `langfuse_link_service.py:83-85` 的 `${host}/project/${pid}/traces/${id}` 格式。

**Tech Stack:** React 18 + TypeScript + Vite（前端无单元测试框架，只有 Playwright e2e；本计划用类型检查 + 手动 e2e 验证）；Python pytest（后端已有测试，但本次后端零改动）。

## Global Constraints

- 遵循 onto-eval-ucd 规范：颜色全 `var(--edp-*)`，字号全 `var(--edp-font-*)`，按钮用 `.edp-btn`，图标内联 SVG `strokeWidth="2"`，硬编码 hex 数 = 0（`.status-*` 调色板例外）
- 后端零改动（`rep.langfuse` dict 已含 `trace_id` / `langfuse_project_id` / `langfuse_host`，见 `backend/services/langfuse_link_service.py:73-88`）
- `LangfuseTraceExplore` 的 host 用法特殊（拼 `htmlPath` + 依赖 `enabled` 状态），**不纳入本次迁移**
- 性能：大数据量（300 题单轮、10 题×10 轮×4 维度）下抽屉渲染不退化——`useLangfuseHost` 的 fetch 只在组件挂载时触发一次，多抽屉同时打开时各自独立缓存（已用 `alive` flag 防竞态；后续可升级为全局单例，本次不做）

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `frontend/src/utils/useLangfuseHost.ts` | `useLangfuseHost` hook，封装 `/api/trace/health` 拉取 | 新建 |
| `frontend/src/components/DrawerShell.tsx` | 公共抽屉外壳（mask + 容器 + 头部 + 关闭 + 内容区） | 新建 |
| `frontend/src/components/LangfuseTraceBlock.tsx` | trace 展示块（链接 + trace id + 复制 + fallback） | 新建 |
| `frontend/src/components/EvalDetailPage.tsx` | 指标抽屉：替换 `1655-1671` 内联 trace + `1631-1638` 外壳 | 修改 |
| `frontend/src/components/ScenarioDeliveryView.tsx` | 场景交付抽屉：`DrawerState` 加字段 + `openDrawer:418` 补字段 + 替换 `754-760` trace + `639-646` 外壳 | 修改 |
| `frontend/src/components/ScenarioStepDrilldown.tsx` | 子步骤：替换 `457-486` 两处 trace + 删 `302-334` 本地 `LangfuseLink` + hex 清零 | 修改 |
| `frontend/src/components/CrossTaskQuestionRuntime.tsx` | 替换 `67-91` host 拉取为 `useLangfuseHost` | 修改 |
| `frontend/src/components/RunComparePanel.tsx` | 替换 `62-113` host 拉取为 `useLangfuseHost` | 修改 |
| `frontend/src/components/RunSpanTreePanel.tsx` | 替换 `93-149` host 拉取为 `useLangfuseHost` + `285-303` trace 展示为 `LangfuseTraceBlock` | 修改 |
| `frontend/src/components/ProcessConsistencyView.tsx` | `349-357` 外壳替换为 `DrawerShell`（无 trace，checkpoint 模式 `langfuse=None`） | 修改 |

---

### Task 1: `useLangfuseHost` hook

**Files:**
- Create: `frontend/src/utils/useLangfuseHost.ts`

**Interfaces:**
- Produces: `export function useLangfuseHost(): string` — 返回 host 字符串（已去尾斜杠），未加载完返回 `''`

- [ ] **Step 1: 创建 hook 文件**

```typescript
// frontend/src/utils/useLangfuseHost.ts
import { useEffect, useState } from 'react'

const API_BASE = import.meta.env.VITE_API_BASE || '/api'

/**
 * 封装 /api/trace/health 拉取，返回 Langfuse host（已去尾斜杠）。
 * 未加载完或 fetch 失败返回 ''。
 * 调用方传入 traceId 即可构造 trace URL。
 */
export function useLangfuseHost(): string {
  const [host, setHost] = useState('')
  useEffect(() => {
    let alive = true
    fetch(`${API_BASE}/trace/health`)
      .then(r => (r.ok ? r.json() : null))
      .then(h => {
        if (alive && h?.host) setHost(String(h.host).replace(/\/$/, ''))
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])
  return host
}
```

- [ ] **Step 2: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误（如有，是 hook 签名问题，修正）

- [ ] **Step 3: Commit**

```bash
git add frontend/src/utils/useLangfuseHost.ts
git commit -m "feat: 新增 useLangfuseHost hook，封装 /api/trace/health 拉取"
```

---

### Task 2: `DrawerShell` 组件

**Files:**
- Create: `frontend/src/components/DrawerShell.tsx`

**Interfaces:**
- Produces:
  ```tsx
  export interface DrawerShellProps {
    title: React.ReactNode
    onClose: () => void
    children: React.ReactNode
    width?: number  // 默认 440
  }
  export function DrawerShell({ title, onClose, children, width }: DrawerShellProps): JSX.Element
  ```

- [ ] **Step 1: 创建组件文件**

```tsx
// frontend/src/components/DrawerShell.tsx
import { ReactNode } from 'react'

export interface DrawerShellProps {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  width?: number
}

export function DrawerShell({ title, onClose, children, width = 440 }: DrawerShellProps) {
  return (
    <>
      <div className="edp-drawer-mask show" onClick={onClose} />
      <div className="edp-drawer show" style={width !== 440 ? { width } : undefined}>
        <div className="edp-dh">
          <h3>{title}</h3>
          <span className="edp-x" onClick={onClose}>×</span>
        </div>
        <div className="edp-db">{children}</div>
      </div>
    </>
  )
}
```

- [ ] **Step 2: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/DrawerShell.tsx
git commit -m "feat: 新增 DrawerShell 公共抽屉外壳组件"
```

---

### Task 3: `LangfuseTraceBlock` 组件

**Files:**
- Create: `frontend/src/components/LangfuseTraceBlock.tsx`

**Interfaces:**
- Consumes: `useLangfuseHost` from Task 1
- Produces:
  ```tsx
  export interface LangfuseTraceBlockProps {
    traceUrl?: string | null
    traceId?: string | null
    observationUrl?: string | null
    langfuseProjectId?: string | null
    label?: string
    className?: string
  }
  export function LangfuseTraceBlock(props: LangfuseTraceBlockProps): JSX.Element | null
  ```

- [ ] **Step 1: 创建组件文件**

```tsx
// frontend/src/components/LangfuseTraceBlock.tsx
import { useState } from 'react'
import { useLangfuseHost } from '../utils/useLangfuseHost'

export interface LangfuseTraceBlockProps {
  traceUrl?: string | null
  traceId?: string | null
  observationUrl?: string | null
  langfuseProjectId?: string | null
  label?: string
  className?: string
}

export function LangfuseTraceBlock({
  traceUrl,
  traceId,
  observationUrl,
  langfuseProjectId,
  label = '在 Langfuse 中查看完整 Trace ↗',
  className = 'edp-sect',
}: LangfuseTraceBlockProps) {
  const host = useLangfuseHost()

  // fallback 构造，对齐后端 langfuse_link_service.py:83-85
  const fallbackUrl = traceId && host
    ? langfuseProjectId
      ? `${host}/project/${langfuseProjectId}/traces/${traceId}`
      : `${host}/trace/${traceId}`
    : null
  const finalUrl = traceUrl || fallbackUrl

  if (!finalUrl && !traceId) return null

  return (
    <div className={className}>
      <h4>🔗 Langfuse Trace</h4>
      {finalUrl ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', flexWrap: 'wrap' }}>
          <a
            href={observationUrl || finalUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="edp-btn"
            style={{ color: 'var(--edp-primary)' }}
          >
            {label}
          </a>
          <CopyButton text={observationUrl || finalUrl} label="复制链接" />
          {traceId && (
            <span style={{ color: 'var(--edp-text3)', fontSize: 'var(--edp-font-base)' }}>
              Trace ID: <code>{traceId}</code>
            </span>
          )}
          {traceId && <CopyButton text={traceId} label="复制 ID" />}
        </div>
      ) : (
        <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>
          Trace ID: <code>{traceId}</code>（Langfuse host 未配置，可复制此 ID 到 Langfuse 搜索）
          <CopyButton text={traceId || ''} label="复制 ID" />
        </div>
      )}
    </div>
  )
}

function CopyButton({ text, label }: { text: string; label: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      className="edp-btn"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1500)
        })
      }}
    >
      {copied ? '已复制' : label}
    </button>
  )
}
```

- [ ] **Step 2: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/LangfuseTraceBlock.tsx
git commit -m "feat: 新增 LangfuseTraceBlock 公共 trace 展示块"
```

---

### Task 4: 迁移指标抽屉

**Files:**
- Modify: `frontend/src/components/EvalDetailPage.tsx:1631-1638`（外壳）+ `1655-1671`（trace 内联）

**Interfaces:**
- Consumes: `DrawerShell` from Task 2, `LangfuseTraceBlock` from Task 3

- [ ] **Step 1: 在文件顶部添加 import**

在 `EvalDetailPage.tsx` 现有 import 区添加：

```tsx
import { DrawerShell } from './DrawerShell'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'
```

- [ ] **Step 2: 定位 `DrawerContent` 函数内的抽屉外壳**

当前 `EvalDetailPage.tsx:1625` 附近的结构（`DrawerContent` 函数返回的 `<div className="edp-drawer-mask show">...`）需要替换。先读这段确认行号：

Run: 读 `frontend/src/components/EvalDetailPage.tsx` 第 1549-1600 行，找到 `<div className="edp-drawer-mask show"` 的确切位置。

- [ ] **Step 3: 替换外壳 + trace 内联**

把 `DrawerContent` 返回的 `<div className="edp-drawer-mask show"> ... <div className="edp-drawer show"> ... </div></div>` 整体改为用 `<DrawerShell title={...} onClose={onClose}>` 包裹。title 用原 `<h3>` 的内容（含题目/轮次信息）。

把 `1655-1671` 的内联 trace 展示（两个 `{result.langfuse_trace_url && ...}` / `{!result.langfuse_trace_url && result.trace_id && ...}` 块）替换为：

```tsx
<LangfuseTraceBlock
  traceUrl={result.langfuse_trace_url}
  traceId={result.trace_id}
  langfuseProjectId={result.langfuse_project_id}
/>
```

- [ ] **Step 4: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 5: 手动 e2e 验证**

启动前端 dev server，打开评估历史详情页，点击题目明细表分数单元格打开指标抽屉：
- 抽屉外壳、标题、关闭按钮正常
- 有 trace_url 时：显示链接 + Trace ID + 复制按钮
- 仅 trace_id 无 trace_url 时：显示 Trace ID + fallback 提示 + 复制 ID
- 点链接跳转 Langfuse 正常

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/EvalDetailPage.tsx
git commit -m "refactor: 指标抽屉改用 DrawerShell + LangfuseTraceBlock"
```

---

### Task 5: 迁移场景交付抽屉

**Files:**
- Modify: `frontend/src/components/ScenarioDeliveryView.tsx:128-139`（`DrawerState` 接口）+ `418`（`openDrawer` 补字段）+ `630-775`（`ScenarioDrawer` 组件）

**Interfaces:**
- Consumes: `DrawerShell` from Task 2, `LangfuseTraceBlock` from Task 3

- [ ] **Step 1: 添加 import**

```tsx
import { DrawerShell } from './DrawerShell'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'
```

- [ ] **Step 2: 扩展 `DrawerState` 接口**

在 `DrawerState`（约 `128-139` 行）加两个字段：

```tsx
interface DrawerState {
  qLabel: string
  qNumber: number | null
  runIndex: number
  stepKey: string
  stepDetail: ScenarioStepDetail | null
  stepsDetail: ScenarioStepsDetailPayload | null
  runComposite: number
  agentRunId: string
  traceUrl?: string | null
  traceId?: string | null          // 新增
  langfuseProjectId?: string | null  // 新增
  stepColumns: StepColumn[]
}
```

同时扩展 `RunRef` 接口（约 `25-30` 行）与 `QuestionData` 的 `langfuse` 字段（约 `60` 行），补 `trace_id` / `langfuse_project_id`：

```tsx
interface RunRef {
  agent_run_id: string
  run_index: number
  composite_score: number
  steps?: ScenarioStepDetail[]
  langfuse?: { trace_url?: string | null; trace_id?: string | null; langfuse_project_id?: string | null } | null
}
```

- [ ] **Step 3: `openDrawer` 补字段**

`openDrawer`（约 `400-421` 行）的 `setDrawer({...})` 补两个字段：

```tsx
setDrawer({
  qLabel: q.label || qid,
  qNumber: q.question_id != null ? q.question_id + 1 : null,
  runIndex,
  stepKey,
  stepDetail,
  stepsDetail: rd || null,
  runComposite: run.composite_score,
  agentRunId: run.agent_run_id,
  traceUrl: rd?.langfuse?.trace_url || cell.langfuse?.trace_url || null,
  traceId: rd?.langfuse?.trace_id || cell.langfuse?.trace_id || null,           // 新增
  langfuseProjectId: rd?.langfuse?.langfuse_project_id || cell.langfuse?.langfuse_project_id || null,  // 新增
  stepColumns: q.step_columns || stepCols,
})
```

- [ ] **Step 4: 重构 `ScenarioDrawer` 组件**

把 `ScenarioDrawer`（约 `630-775` 行）的外壳部分（`639-646` 的 `<div className="edp-drawer-mask show">` + `.edp-drawer` + `.edp-dh` + `.edp-db` 开头）替换为 `<DrawerShell title={...} onClose={onClose}>`。title 用原 `<h3>` 的内容。

把 `754-760` 的 Langfuse 链接块替换为：

```tsx
<LangfuseTraceBlock
  traceUrl={traceUrl}
  traceId={traceId}
  langfuseProjectId={langfuseProjectId}
/>
```

（`traceId` / `langfuseProjectId` 从 `drawer` 解构出来，与 `traceUrl` 并列）

- [ ] **Step 5: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 6: 手动 e2e 验证**

打开评估历史详情 → 场景交付 tab，点击矩阵单元格打开抽屉：
- 单步骤抽屉：trace 链接 + Trace ID + 复制按钮都显示
- 综合分抽屉（stepKey='overall'）：trace 同样显示
- 点链接跳转 Langfuse 正常
- checkpoint 模式任务（过程一致性）：`langfuse=null`，抽屉不渲染 trace 块（符合预期）

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/ScenarioDeliveryView.tsx
git commit -m "refactor: 场景交付抽屉改用 DrawerShell + LangfuseTraceBlock，补 trace_id 字段"
```

---

### Task 6: 迁移子步骤抽屉

**Files:**
- Modify: `frontend/src/components/ScenarioStepDrilldown.tsx:302-334`（删本地 `LangfuseLink`）+ `457-486`（两处 trace 展示）+ `416,419,452,464,475`（hex 清零）

**Interfaces:**
- Consumes: `LangfuseTraceBlock` from Task 3

- [ ] **Step 1: 添加 import**

```tsx
import { LangfuseTraceBlock } from './LangfuseTraceBlock'
```

- [ ] **Step 2: 删除本地 `LangfuseLink` 组件**

删除 `302-334` 行整个 `function LangfuseLink({...}) {...}` 函数。

- [ ] **Step 3: 替换 `457-486` 的 trace 展示**

当前 `457-467` 是 `{runTraceUrl ? <LangfuseLink .../> : <span>暂无 trace_id...</span>}`，`470-486` 是另一个 `{runTraceUrl && <div>Trace 链接：<a>...</a></div>}`。两处重复，整体替换为单个：

```tsx
<LangfuseTraceBlock
  traceUrl={data.langfuse?.trace_url ?? null}
  traceId={data.langfuse?.trace_id ?? null}
  langfuseProjectId={data.langfuse?.langfuse_project_id ?? null}
  observationUrl={expandedStep?.trace_binding?.observation_url ?? null}
/>
```

放在原 `470-486` 的位置（`<div style={{ marginBottom: 12 ...}}>` 那块之上或替换之）。注意 `runTraceUrl` 局部变量（`423` 行）若不再被引用则删除。

- [ ] **Step 4: hex 清零**

替换以下行：
- `416`: `color: '#666'` → `color: 'var(--edp-text3)'`
- `419`: `color: '#dc3545'` → `color: 'var(--edp-red)'`
- `452`: `color: '#666'` → `color: 'var(--edp-text3)'`
- `464`: `color: '#856404'` → `color: 'var(--edp-amber)'`
- `475`: `background: '#f0f4ff'` → `background: 'var(--edp-blue-soft)'`

- [ ] **Step 5: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 6: 手动 e2e 验证**

打开跨任务对比页，进入子步骤下钻：
- 整题 Trace 链接 + Trace ID + 复制按钮显示
- 点链接跳转 Langfuse 正常
- 无 `runTraceUrl` 但有 `trace_id` 时走 fallback 构造（需 host 已加载）
- hex 颜色已全部替换为 token

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/ScenarioStepDrilldown.tsx
git commit -m "refactor: 子步骤抽屉改用 LangfuseTraceBlock，删本地 LangfuseLink，hex 清零"
```

---

### Task 7: 替换 `CrossTaskQuestionRuntime` host 拉取

**Files:**
- Modify: `frontend/src/components/CrossTaskQuestionRuntime.tsx:67-91`

**Interfaces:**
- Consumes: `useLangfuseHost` from Task 1

- [ ] **Step 1: 添加 import**

```tsx
import { useLangfuseHost } from '../utils/useLangfuseHost'
```

- [ ] **Step 2: 替换 host 拉取**

删除 `67-80` 行的 `const [langfuseHost, setLangfuseHost] = useState('')` + `useEffect(...)` 块，替换为：

```tsx
const langfuseHost = useLangfuseHost()
```

- [ ] **Step 3: 修正 `traceUrl` 构造**

当前 `90-91`：

```tsx
const traceUrl = (traceId?: string | null) =>
  traceId && langfuseHost ? `${langfuseHost}/trace/${traceId}` : null
```

这处漏了 project 段。但这处 `traceUrl` 是给 `CrossTaskQuestionRuntime` 内部用的，**调用方没有传 `langfuseProjectId`**——保持原签名不变，只把构造改为用 `useLangfuseHost` 返回的 host（实际就是把 `langfuseHost` 来源从本地 state 改为 hook）。**不改 URL 模板**（这处的 project 段缺失是已有 bug，但修复需要后端返回 project_id 到这个组件，超出本次范围；只在 `LangfuseTraceBlock` 内修，其它组件保持现状避免引入回归）。

即：`traceUrl` 函数本身不动，只是它依赖的 `langfuseHost` 现在来自 hook。

- [ ] **Step 4: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 5: 手动验证**

打开跨任务问题运行时对比页，trace 链接跳转正常（与迁移前行为一致）。

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/CrossTaskQuestionRuntime.tsx
git commit -m "refactor: CrossTaskQuestionRuntime 改用 useLangfuseHost"
```

---

### Task 8: 替换 `RunComparePanel` host 拉取

**Files:**
- Modify: `frontend/src/components/RunComparePanel.tsx:62-113`

**Interfaces:**
- Consumes: `useLangfuseHost` from Task 1

- [ ] **Step 1: 添加 import**

```tsx
import { useLangfuseHost } from '../utils/useLangfuseHost'
```

- [ ] **Step 2: 替换 host 拉取**

删除 `62` 行 `const [langfuseHost, setLangfuseHost] = useState('')`。

在 `68-96` 的 `useEffect`（load 函数）里，`74-84` 行的 `Promise.all` 原本同时 fetch span-trees 和 trace/health。现在 trace/health 由 hook 管，所以改为只 fetch span-trees：

```tsx
useEffect(() => {
  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      setData(null)
      const treeRes = await fetch(`${API_BASE}/comparisons/${comparisonId}/span-trees`)
      if (!treeRes.ok) throw new Error('加载 Span 树失败')
      const json: DualTreeResponse = await treeRes.json()
      setData(json)
      if (!initialAggregateKey && json.left_timeline?.length) {
        const diffKey = json.only_right_aggregate_keys[0] || json.only_left_aggregate_keys[0]
        setSelectedKey(diffKey || json.left_timeline[0].aggregate_key || null)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }
  void load()
}, [comparisonId, initialAggregateKey])

const langfuseHost = useLangfuseHost()
```

`traceUrl` 函数（`110-113`）不动，依赖的 `langfuseHost` 现在来自 hook。

- [ ] **Step 3: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 4: 手动验证**

打开运行对比面板，trace 链接跳转正常。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/RunComparePanel.tsx
git commit -m "refactor: RunComparePanel 改用 useLangfuseHost，移除内联 health fetch"
```

---

### Task 9: 替换 `RunSpanTreePanel` host 拉取 + 迁移 trace 展示

**Files:**
- Modify: `frontend/src/components/RunSpanTreePanel.tsx:93-149`（host 拉取）+ `285-303`（trace 展示）

**Interfaces:**
- Consumes: `useLangfuseHost` from Task 1, `LangfuseTraceBlock` from Task 3

- [ ] **Step 1: 添加 import**

```tsx
import { useLangfuseHost } from '../utils/useLangfuseHost'
import { LangfuseTraceBlock } from './LangfuseTraceBlock'
```

- [ ] **Step 2: 替换 host 拉取**

删除 `93` 行 `const [langfuseHost, setLangfuseHost] = useState('')`。

`95-132` 的 `useEffect`（load 函数）里，`103-107` 的 `Promise.all` 原本 fetch 三个（span-tree / checkpoints / trace/health）。改为只 fetch 前两个：

```tsx
const [treeRes, cpRes] = await Promise.all([
  fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/span-tree`),
  fetch(`${API_BASE}/runs/${encodeURIComponent(runId)}/checkpoints`).catch(() => null),
])
```

删除 `117-120` 的 `if (healthRes?.ok) { ... setLangfuseHost(...) }` 块。

在 `useEffect` 之后添加：

```tsx
const langfuseHost = useLangfuseHost()
```

- [ ] **Step 3: 删除本地 `traceUrl` 函数 + 替换 `285-303` trace 展示**

删除 `146-149` 的 `const traceUrl = (traceId?: string | null) => {...}` 函数。

`285-303` 的：

```tsx
<div style={{ marginTop: 8, display: 'flex', gap: 12, alignItems: 'center' }}>
  {traceUrl(data.trace_id) ? (
    <a href={traceUrl(data.trace_id)!} ... style={{ color: '#667eea' }}>在 Langfuse 打开 Trace</a>
  ) : (
    <span style={{ color: '#888' }}>Langfuse 未配置或未关联 Trace...</span>
  )}
  {data.trace_id ? <code style={{ fontSize: 11, color: '#999' }}>{data.trace_id}</code> : null}
</div>
```

替换为：

```tsx
<LangfuseTraceBlock
  traceUrl={null}
  traceId={data.trace_id}
  langfuseProjectId={data.langfuse_project_id}
  label="在 Langfuse 打开 Trace"
/>
```

（`data.langfuse_project_id` 若类型里没有，需在 `SpanTreeResponse` 的 data 类型里补可选字段 `langfuse_project_id?: string | null`——后端 span-tree 接口若不返回此字段，则 fallback 构造走无 project 段路径，与原行为一致）

- [ ] **Step 4: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 5: 手动验证**

打开运行 Span 树面板：
- 有 trace_id 时：显示链接 + Trace ID + 复制按钮
- 无 trace_id 时：不渲染 trace 块（原行为是显示提示文案"Langfuse 未配置..."，现在改为不渲染——与其它抽屉一致，符合 spec 的"都空返回 null"）

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/RunSpanTreePanel.tsx
git commit -m "refactor: RunSpanTreePanel 改用 useLangfuseHost + LangfuseTraceBlock，hex 清零"
```

---

### Task 10: 迁移过程一致性抽屉外壳

**Files:**
- Modify: `frontend/src/components/ProcessConsistencyView.tsx:349-357`

**Interfaces:**
- Consumes: `DrawerShell` from Task 2

**说明：** 过程一致性抽屉无 trace 数据（checkpoint 模式 `langfuse=None`），只迁移外壳，不加 `LangfuseTraceBlock`。

- [ ] **Step 1: 添加 import**

```tsx
import { DrawerShell } from './DrawerShell'
```

- [ ] **Step 2: 替换外壳**

`349-357` 的：

```tsx
return (
  <>
    <div className="edp-drawer-mask show" onClick={onClose} />
    <div className="edp-drawer show">
      <div className="edp-dh">
        <h3>链条分析 · Q{questionId + 1} R{runIndex + 1}</h3>
        <span className="edp-x" onClick={onClose}>×</span>
      </div>
      <div className="edp-db">
```

替换为：

```tsx
return (
  <DrawerShell
    title={<>链条分析 · Q{questionId + 1} R{runIndex + 1}</>}
    onClose={onClose}
  >
```

并删除文件末尾对应的 `</div></div></>` 闭合标签（原 `.edp-db` / `.edp-drawer` / fragment 的闭合）。

- [ ] **Step 3: 验证类型检查通过**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增错误

- [ ] **Step 4: 手动验证**

打开评估历史详情 → 过程一致性 tab，点击单元格打开抽屉：外壳、标题、关闭正常，内容区不变。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/ProcessConsistencyView.tsx
git commit -m "refactor: 过程一致性抽屉改用 DrawerShell"
```

---

### Task 11: 全量验证 + 收尾

**Files:**
- 无修改，仅验证

- [ ] **Step 1: 全量类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 零错误

- [ ] **Step 2: 全量 e2e 验证**

启动前端 dev server，依次验证：
- 指标抽屉：trace 链接 + Trace ID + 复制按钮 + fallback
- 场景交付抽屉（单步骤 + 综合分）：trace 链接 + Trace ID + 复制
- 子步骤下钻：trace 链接 + Trace ID + 复制 + hex 已清零
- 跨任务问题运行时：trace 链接正常
- 运行对比面板：trace 链接正常
- 运行 Span 树面板：trace 链接 + Trace ID + 复制
- 过程一致性抽屉：外壳正常，无 trace 块

- [ ] **Step 3: 大数据量验证**

按 CLAUDE.md 开发准则，用 300 题单轮任务打开场景交付抽屉，确认渲染无退化（抽屉打开延迟与迁移前持平）。

- [ ] **Step 4: 全量提交**

若前面各 Task 已分别 commit，此步无需额外提交。仅 `git log --oneline` 确认 11 个 commit 都在。

Run: `git log --oneline -12`
Expected: 看到 Task 1-10 的 commit + 本 plan 的 commit

---

## Self-Review

**1. Spec coverage:**
- spec "新建文件" 3 个 → Task 1/2/3 ✓
- spec "各抽屉调用方式" 4 行 → Task 4（指标）/5（场景交付）/6（子步骤）/10（过程一致性外壳）✓
- spec "替换 4 处 langfuseHost 重复" → Task 7/8/9（CrossTaskQuestionRuntime / RunComparePanel / RunSpanTreePanel）。spec 写"4 处"但 LangfuseTraceExplore 不纳入（Global Constraints 已说明），实际 3 处 ✓
- spec "硬编码 hex 清零" → Task 6（子步骤）+ Task 9（RunSpanTreePanel `#667eea`/`#888`/`#999` 通过替换为 `LangfuseTraceBlock` 消除）✓
- spec "测试" → 前端无单测框架，改为 `tsc --noEmit` + 手动 e2e，每 Task 都有验证步骤 ✓
- spec "迁移顺序" 7 步 → Task 1-10 对齐（Task 1-3 新建，4-6 迁移抽屉，7-9 治理 host，10 外壳统一，11 收尾）✓

**2. Placeholder scan:**
- 无 TBD/TODO
- 每个代码步骤都有完整代码块
- "手动 e2e 验证"步骤有具体操作指引

**3. Type consistency:**
- `useLangfuseHost(): string` — Task 1 定义，Task 3/7/8/9 消费 ✓
- `DrawerShellProps { title, onClose, children, width? }` — Task 2 定义，Task 4/5/10 消费 ✓
- `LangfuseTraceBlockProps { traceUrl, traceId, observationUrl, langfuseProjectId, label, className }` — Task 3 定义，Task 4/5/6/9 消费 ✓
- `DrawerState` 的 `traceId` / `langfuseProjectId` 字段 — Task 5 Step 2 添加，Task 5 Step 4 消费 ✓

无类型不一致。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-09-trace-block-unification.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
