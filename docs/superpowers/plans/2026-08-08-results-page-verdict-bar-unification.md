# ResultsPage 维度卡片融合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把评估结果页"评估核心看板"从双层冗余（DimensionVerdictBar 4 大卡片 + 质量总分 4 小卡片）替换为评估历史详情页同款 `edp-verdict-bar`（总分 pill + 四维 chips 一行），消除重复，全站心智统一。

**Architecture:** 抽取详情页内联的 `scoreColor` 与 `VERDICT_BADGE`（含运行稳定性 status 映射）到共享 util，详情页改 import、DOM 不动；ResultsPage 删除下层质量总分 section + 上层 DimensionVerdictBar，替换为复用 `edp-verdict-bar` 系列类的内联渲染；DimensionVerdictBar 组件文件保留（EvalDetailModal 仍用）。

**Tech Stack:** React + TypeScript，复用 `frontend/src/index.css` 已有 `edp-*` 全局类，零新增 CSS。

## Global Constraints

- 遵循 onto-eval UCD 规范：颜色全 `var(--edp-*)`，字号全 `var(--edp-font-*)`，不硬编码 hex/px（`.status-*` 调色板例外）
- 评估历史详情页 `EvalDetailPage.tsx` 的 DOM 渲染不动，仅改 `scoreColor` 与 `VERDICT_BADGE` 的 import 来源
- `DimensionVerdictBar.tsx` 组件文件保留（`EvalDetailModal.tsx:1116` 仍引用），仅 ResultsPage 不再 import
- 零新增 CSS，全部复用 `index.css` 已定义的 `edp-verdict-bar` / `edp-score-pill` / `edp-vchip` / `edp-dot-*` / `edp-b-*` 类
- 大数据量性能：300 题单轮 / 10 题×10 轮×四维度场景下，verdict-bar 是常量级 DOM（1 pill + 4 chips），无列表渲染负担

---

## File Structure

| 文件 | 操作 | 职责 |
|---|---|---|
| `frontend/src/utils/verdictBadge.ts` | 新建 | `VERDICT_BADGE` 常量 + `resolveVerdictBadge(nav, data)` 函数（封装运行稳定性 perf status → verdict 映射）+ `scoreColor()` |
| `frontend/src/components/EvalDetailPage.tsx` | 修改 | 删除内联 `scoreColor`(:125) 与 `VERDICT_BADGE`(:255)，改从 `utils/verdictBadge` import；DOM 渲染不动 |
| `frontend/src/components/ResultsPage.tsx` | 修改 | 删除 `DimensionVerdictBar` import + 下层质量总分 section + 上层 `<DimensionVerdictBar>`，替换为内联 `edp-verdict-bar` 渲染 |
| `frontend/src/components/DimensionVerdictBar.tsx` | 不动 | EvalDetailModal 仍引用，保留原样 |

---

## Task 1: 抽取 verdictBadge 共享 util

**Files:**
- Create: `frontend/src/utils/verdictBadge.ts`

**Interfaces:**
- Consumes: `DimensionVerdictsPayload`（`frontend/src/types/dimensionVerdict.ts`）
- Produces:
  - `scoreColor(v: number | null): string`
  - `VERDICT_BADGE: Record<string, { label: string; badgeCls: string; dotCls: string }>`
  - `resolveVerdictBadge(nav: string, data: unknown): { label: string; badgeCls: string; dotCls: string }` — 封装运行稳定性 perf status 映射，调用方传入 `nav`（'quality'|'consistency'|'scenario'|'perf'）与维度 data 对象即可拿到 badge

- [ ] **Step 1: 创建 util 文件**

```ts
// frontend/src/utils/verdictBadge.ts
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'

/** 分数着色：null→灰；≥0.8→绿；≥0.6→琥珀；<0.6→红 */
export function scoreColor(v: number | null): string {
  if (v == null) return 'var(--edp-text3)'
  return v >= 0.8 ? 'var(--edp-green)' : v >= 0.6 ? 'var(--edp-amber)' : 'var(--edp-red)'
}

/** 四维结论 status → 文字/颜色统一映射（达标绿·需关注琥珀·未达标红） */
export const VERDICT_BADGE: Record<string, { label: string; badgeCls: string; dotCls: string }> = {
  pass: { label: '达标', badgeCls: 'edp-b-pass', dotCls: 'edp-dot-pass' },
  warn: { label: '需关注', badgeCls: 'edp-b-warn', dotCls: 'edp-dot-warn' },
  fail: { label: '未达标', badgeCls: 'edp-b-fail', dotCls: 'edp-dot-fail' },
  na: { label: '—', badgeCls: 'edp-b-na', dotCls: 'edp-dot-na' },
  disabled: { label: '未启用', badgeCls: 'edp-b-na', dotCls: 'edp-dot-na' },
  descriptive: { label: '描述性', badgeCls: 'edp-b-na', dotCls: 'edp-dot-descriptive' },
}

/** 运行稳定性 performance.status → verdict status 映射 */
const PERF_TO_VERDICT: Record<string, string> = {
  within_target: 'pass',
  elevated: 'warn',
  severe: 'fail',
  na: 'na',
  disabled: 'disabled',
}

/**
 * 解析维度 data 对象，返回 badge。
 * 运行稳定性维度（nav === 'perf'）走 performance.status 映射，其它维度直接用 data.status。
 */
export function resolveVerdictBadge(
  nav: string,
  data: unknown,
): { label: string; badgeCls: string; dotCls: string } {
  if (nav === 'perf') {
    const rsData = data as DimensionVerdictsPayload['runtime_stability'] | undefined
    const perfStatus = (rsData?.performance?.status || 'na') as string
    const mapped = PERF_TO_VERDICT[perfStatus]
    if (mapped) return VERDICT_BADGE[mapped] || VERDICT_BADGE.na
  }
  const status = ((data as { status?: string } | null | undefined)?.status || 'na') as string
  return VERDICT_BADGE[status] || VERDICT_BADGE.na
}
```

- [ ] **Step 2: TypeScript 类型检查**

Run: `cd frontend && npx tsc --noEmit 2>&1 | grep -E "verdictBadge|error TS" | head -20`
Expected: 无 `verdictBadge.ts` 相关错误（其它文件预先存在的错误忽略）

- [ ] **Step 3: Commit**

```bash
git add frontend/src/utils/verdictBadge.ts
git commit -m "feat: 抽取 verdictBadge 共享 util（scoreColor + VERDICT_BADGE + resolveVerdictBadge）"
```

---

## Task 2: EvalDetailPage 改用共享 util

**Files:**
- Modify: `frontend/src/components/EvalDetailPage.tsx`（删除 :125-128 `scoreColor`、:254-262 `VERDICT_BADGE`，改 import；:1066-1099 chips 渲染段用 `resolveVerdictBadge` 替换内联映射逻辑）

**Interfaces:**
- Consumes: `scoreColor`, `resolveVerdictBadge` from `../utils/verdictBadge`
- Produces: 无（仅内部重构，对外行为不变）

- [ ] **Step 1: 添加 import**

在 `EvalDetailPage.tsx` 顶部 import 区（现有 `import { buildQualityWeightTooltip } from './qualityWeightTooltip'` 附近）加一行：

```ts
import { scoreColor, resolveVerdictBadge } from '../utils/verdictBadge'
```

- [ ] **Step 2: 删除内联 scoreColor**

删除 `EvalDetailPage.tsx:125-128`：

```ts
function scoreColor(v: number | null) {
  if (v == null) return 'var(--edp-text3)'
  return v >= 0.8 ? 'var(--edp-green)' : v >= 0.6 ? 'var(--edp-amber)' : 'var(--edp-red)'
}
```

（连同上方注释 `/** ... */` 若有，一并删。`runtimeColor` / `runtimeStatusLabel` / `metricSortIndex` 等相邻函数保留。）

- [ ] **Step 3: 删除内联 VERDICT_BADGE**

删除 `EvalDetailPage.tsx:254-262`（含上方注释 `// 四维结论 status → ...`）：

```ts
const VERDICT_BADGE: Record<string, { label: string; badgeCls: string; dotCls: string }> = {
  pass: { label: '达标', badgeCls: 'edp-b-pass', dotCls: 'edp-dot-pass' },
  warn: { label: '需关注', badgeCls: 'edp-b-warn', dotCls: 'edp-dot-warn' },
  fail: { label: '未达标', badgeCls: 'edp-b-fail', dotCls: 'edp-dot-fail' },
  na: { label: '—', badgeCls: 'edp-b-na', dotCls: 'edp-dot-na' },
  disabled: { label: '未启用', badgeCls: 'edp-b-na', dotCls: 'edp-dot-na' },
  descriptive: { label: '描述性', badgeCls: 'edp-b-na', dotCls: 'edp-dot-descriptive' },
}
```

- [ ] **Step 4: chips 渲染段用 resolveVerdictBadge 替换内联映射**

找到 `EvalDetailPage.tsx:1072-1089` 这段（`.map(v => { const status = ...; let verdict = VERDICT_BADGE[status]...; if (v.nav === 'perf') {...} ...}`），替换为：

```tsx
              ].map(v => {
                const verdict = resolveVerdictBadge(v.nav, v.data)
                return (
                  <span key={v.nav} className={`edp-vchip ${view === v.nav ? 'active' : ''}`} onClick={() => {
                    if (v.nav === 'perf') setView('perf')
                    else if (v.nav === 'consistency') setView('consistency')
                    else if (v.nav === 'scenario') setView('scenario')
                    else { setView('metrics'); setSelCat(null) }
                  }}>
                    <span className={`edp-dot ${verdict.dotCls}`} />
                    <span className="edp-t">{v.label}</span>
                    <span className={`edp-vbadge ${verdict.badgeCls}`}>{verdict.label}</span>
```

注意：保留 `v.data` 的 `as` 类型断言若 TS 报错（原代码 `v.nav === 'perf'` 分支里有 `as DimensionVerdictsPayload['runtime_stability']`，`resolveVerdictBadge` 内部已处理类型，调用方无需再断言）。

- [ ] **Step 5: TypeScript 类型检查**

Run: `cd frontend && npx tsc --noEmit 2>&1 | grep "EvalDetailPage" | head -20`
Expected: 无新增错误（`EvalDetailPage.tsx:1640` 的 `semantic_role` 预先存在错误可忽略）

- [ ] **Step 6: 手动验证详情页无回归**

打开评估历史详情页（`http://122.51.48.170:45500/` 进入任一已完成任务的详情），确认：
- 顶部 verdict-bar 的 pill 大数字着色正常（绿/琥珀/红）
- 4 个 chips 的圆点颜色 + badge 文字与改造前一致
- 运行稳定性 chip 的 badge 仍根据 performance.status 正确显示（达标/需关注/未达标）
- chips 点击仍能切换页内 view

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/EvalDetailPage.tsx
git commit -m "refactor: EvalDetailPage 改用共享 verdictBadge util，DOM 渲染不变"
```

---

## Task 3: ResultsPage 替换为 verdict-bar

**Files:**
- Modify: `frontend/src/components/ResultsPage.tsx`

**Interfaces:**
- Consumes:
  - `scoreColor`, `resolveVerdictBadge` from `../utils/verdictBadge`
  - `buildQualityWeightTooltip` from `./qualityWeightTooltip`（已有 import）
  - `openDetailWindow` from `../utils/detailWindow`（已有 import）
  - `formatScore`（本文件 :156 已定义）
  - `qualityScore` / `dimensionVerdicts` / `isOfflineEvaluation` state
- Produces: 无（仅 UI 重构）

- [ ] **Step 1: 删除 DimensionVerdictBar import**

删除 `ResultsPage.tsx:14`：

```ts
import { DimensionVerdictBar } from './DimensionVerdictBar'
```

并删除 `ResultsPage.tsx:15` 的 `DimensionVerdictBar` 类型 import（若有）：

```ts
import type { DimensionTabId, DimensionVerdictsPayload } from '../types/dimensionVerdict'
```

保留 `DimensionVerdictsPayload`（仍用于 state 类型），改为只 import 类型：

```ts
import type { DimensionVerdictsPayload } from '../types/dimensionVerdict'
```

（若原行已是 `import type`，保留不动；删除 `DimensionTabId` 若不再使用——`DimensionTabId` 原本只在 `DimensionVerdictBar` 的 `onNavigateTab` 回调签名里用，ResultsPage 不再需要。）

- [ ] **Step 2: 添加 verdictBadge import**

在 import 区加：

```ts
import { scoreColor, resolveVerdictBadge } from '../utils/verdictBadge'
```

- [ ] **Step 3: 删除下层"质量总分" section**

找到 `ResultsPage.tsx` 中当前的 `qualityScore` section（`{qualityScore && (` 开始，到对应 `</section>` `)}` 结束的整段——含 `<h4>质量总分</h4>` + 4 张小卡片 grid + gates 提示）。整段删除。

**保留 gates 提示**：gates 提示（`qualityScore.gates?.length > 0` 的 amber 提示行）从原 section 中提取出来，放到新 verdict-bar 下方。具体位置见 Step 5。

- [ ] **Step 4: 删除上层 DimensionVerdictBar，替换为 verdict-bar**

找到 `{dimensionVerdicts && (` 包裹的 `<DimensionVerdictBar ... />` 整段，替换为：

```tsx
          {(qualityScore || dimensionVerdicts) && (
            <div className="edp-verdict-bar">
              {qualityScore?.total_score != null && (
                <div className="edp-score-pill">
                  <span className="edp-num" style={{ color: scoreColor(qualityScore.total_score) }}>
                    {formatScore(qualityScore.total_score)}
                  </span>
                  <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1 }}>
                    <span className="edp-lbl">质量总分</span>
                    {qualityScore.result_score != null && (
                      <span
                        className="edp-lbl"
                        style={{ color: 'var(--edp-text3)', fontSize: 10 }}
                        title={buildQualityWeightTooltip({
                          result_weight: qualityScore.result_weight,
                          process_weight: qualityScore.process_weight,
                          stability_weight: qualityScore.stability_weight,
                          isOffline: isOfflineEvaluation,
                        })}
                      >
                        结果{formatScore(qualityScore.result_score)}
                        {!isOfflineEvaluation && qualityScore.process_score != null
                          ? `·过程${formatScore(qualityScore.process_score)}`
                          : ''}
                        {!isOfflineEvaluation && qualityScore.stability_score != null
                          ? `·稳定${formatScore(qualityScore.stability_score)}`
                          : ''}
                      </span>
                    )}
                  </div>
                </div>
              )}
              {dimensionVerdicts && (
                <div className="edp-verdict-chips">
                  {([
                    { nav: 'quality', label: '评测指标', data: dimensionVerdicts.quality },
                    ...(isOfflineEvaluation ? [] : dimensionVerdicts.process_consistency
                      ? [{ nav: 'consistency', label: '过程一致性', data: dimensionVerdicts.process_consistency }]
                      : []),
                    ...(dimensionVerdicts.scenario_delivery
                      ? [{ nav: 'scenario', label: '场景交付', data: dimensionVerdicts.scenario_delivery }]
                      : []),
                    ...(isOfflineEvaluation ? [] : dimensionVerdicts.runtime_stability
                      ? [{ nav: 'perf', label: '运行稳定性', data: dimensionVerdicts.runtime_stability }]
                      : []),
                  ] as const).map(v => {
                    const verdict = resolveVerdictBadge(v.nav, v.data)
                    const perfView = v.nav === 'perf'
                      ? (dimensionVerdicts.inference_rounds > 1 ? 'stability' : 'task')
                      : undefined
                    return (
                      <span
                        key={v.nav}
                        className="edp-vchip"
                        style={{ cursor: 'pointer' }}
                        onClick={() => openDetailWindow(taskId, { section: v.nav, perfView })}
                      >
                        <span className={`edp-dot ${verdict.dotCls}`} />
                        <span className="edp-t">{v.label}</span>
                        <span className={`edp-vbadge ${verdict.badgeCls}`}>{verdict.label}</span>
                      </span>
                    )
                  })}
                </div>
              )}
            </div>
          )}
          {(qualityScore?.gates?.length ?? 0) > 0 && (
            <div style={{
              marginTop: 10,
              fontSize: 'var(--edp-font-base)',
              color: 'var(--edp-amber)',
              background: 'var(--edp-amber-soft)',
              padding: '8px 10px',
              borderRadius: 'var(--edp-radius-sm)',
            }}>
              {qualityScore!.gates!.map(gate => gate.reason || `${gate.type} cap=${gate.cap}`).join('；')}
            </div>
          )}
```

- [ ] **Step 5: 更新 section 描述**

找到 `results-section-desc` 段（`ResultsPage.tsx` 内 `<p className="results-section-desc">`），将在线模式的描述：

```ts
'左侧展示质量指标分布，右侧展示质量总分（结果分 + 过程一致性过程分）。'
```

改为：

```ts
'左侧展示质量指标分布，右侧展示质量总分与四维评估结论。'
```

- [ ] **Step 6: 清理 showFinalCharts 条件**

`showFinalCharts` 的渲染条件（`:583`）当前含 `(!isOfflineEvaluation && Object.keys(runtimeSummary).length > 0)`——这是为已删除的运行时效率 section 服务的。改为：

```ts
{showFinalCharts && (chartData || qualityScore || dimensionVerdicts) && (
```

（`dimensionVerdicts` 替代 `runtimeSummary` 作为是否渲染看板的判据之一。）

- [ ] **Step 7: 检查 DimensionTabId 残留引用**

Run: `cd frontend && grep -n "DimensionTabId" src/components/ResultsPage.tsx`
Expected: 无匹配（若仍有，说明别处还在用，保留对应 import；若无，确认 Step 1 已删该 import）

- [ ] **Step 8: TypeScript 类型检查**

Run: `cd frontend && npx tsc --noEmit 2>&1 | grep "ResultsPage" | head -20`
Expected: 无 `ResultsPage.tsx` 相关错误

- [ ] **Step 9: 手动验证**

打开评估结果页（`http://122.51.48.170:45500/` 进入任一已完成任务的结果页），确认：
- 顶部"评估核心看板"显示一行：左 pill（总分大数字 + 子分内联小字）+ 右 4 个 chips
- pill 大数字着色（绿/琥珀/红）与权重 tooltip 正确
- 4 个 chips 圆点 + badge 正确（在线模式 4 个，离线模式 2 个：评测指标 + 场景交付）
- chips 点击在新窗口打开详情页对应 Tab
- gates 提示仍展示（若有 gate）
- 雷达图正常
- 运行稳定性 chip 的 perfView 透传：多轮任务点击跳详情页稳定性视图，单轮跳 task 视图
- 与评估历史详情页顶部 verdict-bar 视觉一致

- [ ] **Step 10: Commit**

```bash
git add frontend/src/components/ResultsPage.tsx
git commit -m "feat: ResultsPage 维度卡片融合为 verdict-bar，对齐详情页范式"
```

---

## Self-Review

**Spec coverage:**
- 3.2.1 删除下层质量总分 section → Task 3 Step 3 ✓
- 3.2.2 替换 DimensionVerdictBar 为 verdict-bar → Task 3 Step 4 ✓
- 3.2.3 headline 丢弃 → Task 3 Step 4 的新 chips 不含 headline 字段 ✓
- 3.2.4 chips 点击 + perfView 透传 → Task 3 Step 4 的 onClick ✓（openDetailWindow 已支持透传）
- 3.2.5 section 描述更新 → Task 3 Step 5 ✓
- 3.2.6 scoreColor 抽取 → Task 1 + Task 2 Step 2 + Task 3 Step 2 import ✓
- 3.2.7 DimensionVerdictBar 处置（保留文件）→ File Structure 已注明不动 ✓
- 3.2.8 离线模式 → Task 3 Step 4 的 chips 数组用 `isOfflineEvaluation` 过滤 ✓ + pill 子分用 `!isOfflineEvaluation` 守卫 ✓
- 3.3 CSS 类复用 → 全部 `edp-*` 类已在 index.css 确认 ✓
- 3.4 VERDICT_BADGE 抽取 → Task 1 `resolveVerdictBadge` + Task 2 改 import ✓

**Placeholder scan:** 无 TBD/TODO，所有代码块完整。

**Type consistency:** `resolveVerdictBadge(nav, data)` 在 Task 1 定义、Task 2 / Task 3 调用签名一致；`scoreColor` 同理。`DimensionVerdictsPayload` import 路径统一从 `../types/dimensionVerdict`。
