# 评估结果页维度卡片融合方案：对齐详情页 verdict-bar 范式

| 项目 | 内容 |
|------|------|
| 文档名称 | ResultsPage 维度卡片融合设计 |
| 日期 | 2026-08-08 |
| 主题 | 评估结果页"评估核心看板"区域布局优化 |
| 关联 | 评估历史详情页 `EvalDetailPage.tsx:1041-1106`（对齐蓝本） |

---

## 1. 背景与问题

### 1.1 现状

评估结果页（`ResultsPage.tsx`）的"评估核心看板"区域当前是**双层冗余结构**：

**上层** — `DimensionVerdictBar` 组件，4 张大卡片：
- 质量指标（badge + `结果分 0.82`）
- 过程一致性（badge + `过程分 0.75`）
- 运行稳定性（badge + `多轮正确率 90%`）
- 场景交付（badge + `综合 0.78`）← 不参与总分

**下层** — "质量总分" section，4 张小卡片：
- 总得分 0.80 + 权重提示（结果权重/过程权重/稳定性权重）
- 结果分 0.82（正确性/相关性/忠诚度等）
- 过程得分 0.75（过程一致性）
- 稳定性得分 0.70（多轮一致性）

### 1.2 冗余根源

`结果分 / 过程分 / 稳定性` 三个子分在上层维度卡片（作为 scoreLine）和下层总分小卡片各出现一次，本质是同一组数据。

根源：ResultsPage 把详情页里**内联在 pill 里的 3 个子分**展开成了 3 张独立小卡片，又在上层维度条里再写一遍 `结果分`，导致同一组数出现两次。

### 1.3 与评估历史详情页的不一致

评估历史详情页（`EvalDetailPage.tsx:1041-1106`）已采用**紧凑单行范式** `edp-verdict-bar`：

```
[ (0.80 质量总分 结果0.82·过程0.75·稳定0.70) ]  [●评测指标 达标] [●过程一致性 达标] [●场景交付 达标] [●运行稳定性 达标]
```

- 左侧 `edp-score-pill`：大数字总分 + 子分内联小字（`结果0.82·过程0.75·稳定0.70`），权重信息走 title tooltip
- 右侧 `edp-verdict-chips`：4 个 chips，每个 = 圆点 + 维度名 + badge，点击切换页内 view

ResultsPage 与详情页呈现同一组数据却用两套结构，全站心智不统一。

---

## 2. 目标

1. **消除冗余**：`结果分/过程分/稳定性` 在页面中只出现一次
2. **对齐详情页**：ResultsPage 复用 `edp-verdict-bar` 范式（pill + chips），全站统一
3. **总分关系一目了然**：总分 = 结果分 + 过程分 + 稳定性 的加权关系通过 pill 的 tooltip 表达
4. **场景交付定位清晰**：不参与总分的事实仅靠 tooltip 说明，不做视觉特殊标记（与详情页四维 chips 样式一致）
5. **详情页不动**：本方案只改 ResultsPage，不修改 EvalDetailPage

---

## 3. 设计

### 3.1 目标布局

```
┌─ 评估核心看板 ───────────────────────────────────────────────────┐
│  section 描述：左侧展示质量指标分布，右侧展示质量总分与四维结论。  │
│                                                                  │
│  [ (0.80 质量总分 结果0.82·过程0.75·稳定0.70) ]  [●评测指标 达标] │
│  [●过程一致性 达标] [●场景交付 达标] [●运行稳定性 达标]           │
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────────────────────────┐ │
│  │ 质量指标雷达图    │  │ （pill + chips 已在上方，此列不再有  │ │
│  │                  │  │   独立总分 section）                  │ │
│  └──────────────────┘  └──────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 改动点

#### 3.2.1 删除下层"质量总分" section

删除 `ResultsPage.tsx` 中当前的 `qualityScore` section（含总得分/结果分/过程分/稳定性得分 4 张小卡片 + gates 提示）。

**gates 提示保留**：质量总分的 gate（安全门禁 cap 等）信息仍需展示，作为 verdict-bar 下方的一行 amber 提示保留。

#### 3.2.2 替换 DimensionVerdictBar 为详情页同款 verdict-bar

**当前**：`<DimensionVerdictBar data={dimensionVerdicts} onNavigateTab={...} />`（4 大卡片）

**改为**：直接渲染 `edp-verdict-bar` 结构（复用详情页同款 DOM + CSS 类）：

```tsx
<div className="edp-verdict-bar">
  {/* 左：质量总分 pill（子分内联） */}
  {qualityScore?.total_score != null && (
    <div className="edp-score-pill">
      <span className="edp-num" style={{ color: scoreColor(qualityScore.total_score) }}>
        {formatScore(qualityScore.total_score)}
      </span>
      <div style={{ display: 'flex', flexDirection: 'column', lineHeight: 1 }}>
        <span className="edp-lbl">质量总分</span>
        {qualityScore.result_score != null && (
          <span className="edp-lbl" style={{ color: 'var(--edp-text3)', fontSize: 10 }}
                title={buildQualityWeightTooltip({...})}>
            结果{formatScore(qualityScore.result_score)}
            {qualityScore.process_score != null ? `·过程${formatScore(qualityScore.process_score)}` : ''}
            {qualityScore.stability_score != null ? `·稳定${formatScore(qualityScore.stability_score)}` : ''}
          </span>
        )}
      </div>
    </div>
  )}

  {/* 右：四维 chips */}
  {dimensionVerdicts && (
    <div className="edp-verdict-chips">
      {[
        { nav: 'quality', label: '评测指标', data: dimensionVerdicts.quality },
        { nav: 'consistency', label: '过程一致性', data: dimensionVerdicts.process_consistency },
        { nav: 'scenario', label: '场景交付', data: dimensionVerdicts.scenario_delivery },
        { nav: 'perf', label: '运行稳定性', data: dimensionVerdicts.runtime_stability },
      ].map(v => (
        <span key={v.nav} className="edp-vchip"
              onClick={() => openDetailWindow(taskId, { section: v.nav })}>
          <span className={`edp-dot ${dotClsFor(v.data?.status)}`} />
          <span className="edp-t">{v.label}</span>
          <span className={`edp-vbadge ${badgeClsFor(v.data?.status)}`}>{labelFor(v.data?.status)}</span>
        </span>
      ))}
    </div>
  )}
</div>
```

#### 3.2.3 headline 丢弃

`DimensionVerdictBar` 大卡片里的 `headline`（一句话结论）不再展示。headline 信息只在评估历史详情页呈现，ResultsPage 保持和详情页 chips 同款的信息密度（点 + 名 + badge）。

#### 3.2.4 chips 点击行为

chips 点击 = `openDetailWindow(taskId, { section: tabId })`，在新窗口打开评估详情页对应 Tab。与当前 `DimensionVerdictBar` 的 `onNavigateTab` 回调行为一致，只是 DOM 形态从大卡片换成 chip。

**运行稳定性 chip 的 perfView 参数**：当前 `DimensionVerdictBar` 对运行稳定性维度会根据 `inference_rounds > 1` 传 `{ perfView: 'stability' }` 或 `{ perfView: 'task' }`。新 chips 保持该逻辑——`openDetailWindow(taskId, { section: 'perf', perfView: ... })`。

`openDetailWindow`（`frontend/src/utils/detailWindow.ts:15`）的 `extra` 参数类型为 `Record<string, string | number | null | undefined>`，已支持任意键透传，`perfView` 可直接传，无需扩展。

#### 3.2.5 section 描述更新

当前：`'左侧展示质量指标分布，右侧展示质量总分（结果分 + 过程一致性过程分）。'`

改为：`'左侧展示质量指标分布，右侧展示质量总分与四维评估结论。'`

#### 3.2.6 scoreColor 抽取

详情页 pill 的大数字用 `scoreColor(total_score)` 着色（绿/琥珀/红）。`scoreColor` 当前内联在 `EvalDetailPage.tsx:125`：

```ts
function scoreColor(v: number | null) {
  if (v == null) return 'var(--edp-text3)'
  return v >= 0.8 ? 'var(--edp-green)' : v >= 0.6 ? 'var(--edp-amber)' : 'var(--edp-red)'
}
```

抽取到共享 util（如 `frontend/src/utils/scoreColor.ts`），详情页改为 import，ResultsPage 同样 import。详情页仅改 import 来源，DOM 渲染不动。

#### 3.2.7 DimensionVerdictBar 组件处置

替换后 `DimensionVerdictBar` 在 ResultsPage 不再使用。检查其他引用：
- 若仅 ResultsPage 引用 → 删除组件文件 + 其类型依赖
- 若别处仍用 → 保留，仅 ResultsPage 不再引用

#### 3.2.8 离线评估模式

离线评估（`isOfflineEvaluation = true`）时：
- 当前：不展示过程得分、不展示运行稳定性维度
- 新版：pill 子分只显示 `结果分`（不显示过程/稳定）；chips 条只展示 `评测指标` + `场景交付`（不展示过程一致性、运行稳定性）

与详情页离线模式行为对齐。

### 3.3 复用的 CSS 类（已存在于 index.css，全局可用）

| 类 | 定义位置 | 用途 |
|---|---|---|
| `.edp-verdict-bar` | `index.css:6561` | 总分 pill + chips 一行容器 |
| `.edp-score-pill` | `index.css:6545` | 总分药丸 |
| `.edp-score-pill .edp-num` | `index.css:6546` | 大数字总分 |
| `.edp-score-pill .edp-lbl` | `index.css:6547` | 标签小字 |
| `.edp-verdict-chips` | `index.css:6568` | chips 条容器 |
| `.edp-vchip` | `index.css:6569` | 单个 chip |
| `.edp-dot` / `-pass/-warn/-fail/-na/-descriptive` | `index.css:6577-6581` | 状态圆点 |
| `.edp-vbadge` | `index.css:6585` | 状态徽章 |
| `.edp-vchip .edp-t` | `index.css:6582` | chip 维度名 |

**零新增 CSS**——所有样式复用详情页已有类。

### 3.4 VERDICT_BADGE 映射

详情页 `EvalDetailPage.tsx:254-262` 已定义 `VERDICT_BADGE` 映射（status → label/badgeCls/dotCls）。ResultsPage 需用同一套映射：

- 方案：从 EvalDetailPage 抽取 `VERDICT_BADGE` 常量 + 运行稳定性特殊处理逻辑到共享 util（如 `frontend/src/utils/verdictBadge.ts`），详情页与 ResultsPage 共用
- 运行稳定性 chip 的 badge 需特殊处理（`performance.status` 走 `performanceStatusLabel/Color`，非 `verdictStatusColor`）——抽 util 时一并封装
- 详情页仅改 import 来源，DOM 渲染不动（与 scoreColor 抽取同样的"蓝本不动"原则）

---

## 4. 数据流

无后端改动。所有数据源不变：
- `qualityScore`（含 total/result/process/stability_score + weights + gates）
- `dimensionVerdicts`（含 quality/process_consistency/scenario_delivery/runtime_stability）
- `isOfflineEvaluation`

只是前端渲染结构改变。

---

## 5. 不改动项

- 评估历史详情页 `EvalDetailPage.tsx` 不动
- 雷达图（左侧）不动
- 下方的"运行时明细表""场景交付明细""评测摘要"等其它 section 不动
- 后端 API 不动
- `DimensionVerdictBar` 组件若别处仍引用则保留；仅 ResultsPage 不再用

---

## 6. 验证

- TypeScript 类型检查通过（`npx tsc --noEmit`）
- 评估结果页：
  - 总分 pill 显示 total_score + 子分内联 + 权重 tooltip
  - 4 个 chips（在线模式）/ 2 个 chips（离线模式）显示正确 badge
  - chips 点击打开详情页对应 Tab
  - gates 提示仍展示
  - 雷达图正常
- 与评估历史详情页顶部 verdict-bar 视觉一致（同款 pill + chips）
