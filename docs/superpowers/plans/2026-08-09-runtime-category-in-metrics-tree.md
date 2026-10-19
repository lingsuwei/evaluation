# 左树追加"运行效能"指标类别 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 评估详情页左侧"评测指标"分组下追加"运行效能"节点；右侧"运行时指标"卡片标题改为"运行效能"并联动展开。

**Architecture:** 单文件前端改动（`EvalDetailPage.tsx`）。在 `groupedQualityMetrics.map` 之后追加条件渲染块，复用 `edp-cat-row` 类与现有 `edp-grp-runtime` 配色；右侧 `<details>` 卡片标题文案改名并加 `open` 联动到 `selCat === 'runtime'`。

**Tech Stack:** React + TypeScript，onto-eval UCD token 体系（`edp-*` 语义类）

## Global Constraints

- 一切视觉值走 `--edp-*` token 或语义类，禁止硬编码 hex —— **例外**：`edp-grp-runtime` 已有先例用 `#00897b`（`index.css:6713`），本次左树节点沿用同色保持一致，**不新增 token**
- 图标内联 SVG，`strokeWidth="2"` / `2.5`，`fill="none"`，`stroke="currentColor"`
- `selCat` 取新值 `'runtime'`，与质量类别（如 `'safety'`、`'correctness'`）不冲突
- 大数据量考虑（CLAUDE.md 准则）：左树仅多一个静态节点，无性能影响

---

## File Structure

仅修改一个文件：

- **Modify:** `frontend/src/components/EvalDetailPage.tsx`
  - 左树"评测指标"分组（`1122-1145` 行）：在 `groupedQualityMetrics.map` 后追加运行效能节点
  - 右侧运行效能卡片（`1276-1297` 行）：标题改名 + `open` 联动

**不改动：** `index.css`、`runtimeMetrics` 的 `useMemo`、工具函数、后端、其他组件

---

## Task 1: 左树追加"运行效能"节点 + 右侧标题改名与联动

**Files:**
- Modify: `frontend/src/components/EvalDetailPage.tsx:1144`（追加左树节点）
- Modify: `frontend/src/components/EvalDetailPage.tsx:1277`（`<details>` 加 `open` 联动）
- Modify: `frontend/src/components/EvalDetailPage.tsx:1281`（标题文案改名）

**Interfaces:**
- Consumes:
  - `runtimeMetrics: MetricSummary[]`（`EvalDetailPage.tsx:519` 已定义）
  - `view: string`、`selCat: string | null`、`setView`、`setSelCat`、`setSelQ`（组件 state，已存在）
- Produces: 无（纯展示层改动，无新导出）

**说明：** 此任务无单元测试可写——`EvalDetailPage` 是纯前端展示组件，项目无前端组件单测基建（`package.json` 仅有 `vite build` + `playwright e2e`）。验证靠 build 通过 + 手动/视觉确认。TDD 不适用于此场景，跳过"写失败测试"步骤。

- [ ] **Step 1: 在 `groupedQualityMetrics.map` 之后、`</details>` 之前追加运行效能节点**

定位 `frontend/src/components/EvalDetailPage.tsx`，找到 `1144` 行 `})}` （`groupedQualityMetrics.map` 的闭合）与 `1145` 行 `</details>`（"评测指标"分组的闭合）。

在 `})}` 与 `</details>` 之间插入：

```tsx
            {runtimeMetrics.length > 0 && (() => {
              const scored = runtimeMetrics.filter(m => m.avg_score != null)
              const pr = scored.length ? scored.reduce((a, m) => a + (m.pass_rate ?? 0), 0) / scored.length : null
              return (
                <div
                  className={`edp-cat-row ${view === 'metrics' && selCat === 'runtime' ? 'active' : ''}`}
                  onClick={() => { setView('metrics'); setSelQ(null); setSelCat('runtime') }}
                >
                  <span className="edp-grp-tag" style={{ background: '#00897b' }}>RT</span>
                  <span className="edp-n">运行效能</span>
                  <span className="edp-mini">通过<b>{pr != null ? pr.toFixed(0) + '%' : '—'}</b>·共{runtimeMetrics.length}项</span>
                </div>
              )
            })()}
```

要点：
- `selCat` 取 `'runtime'` 作为特殊选中值
- 通过率算法与质量节点一致：`Σ(pass_rate) / scored.length`
- 配色 `#00897b` 与右侧 `edp-grp-runtime` 一致
- mini 文案格式：`通过{pr}%·共{N}项`（与质量节点 `·` 分隔风格对齐）

- [ ] **Step 2: 右侧 `<details>` 卡片加 `open` 联动**

定位 `1277` 行：
```tsx
                  <details className="edp-cat-card">
```

改为：
```tsx
                  <details className="edp-cat-card" open={selCat === 'runtime' || undefined}>
```

- [ ] **Step 3: 右侧卡片标题"运行时指标" → "运行效能"**

定位 `1281` 行：
```tsx
                    <span className="edp-cat-title">运行时指标</span>
```

改为：
```tsx
                    <span className="edp-cat-title">运行效能</span>
```

- [ ] **Step 4: Typecheck 验证**

Run: `cd frontend && npx tsc --noEmit 2>&1 | grep "EvalDetailPage"`
Expected: 仅 `EvalDetailPage.tsx(1653,48): error TS2339: Property 'semantic_role' does not exist on type 'SpanTreeNode'`（pre-existing，与本次改动无关），**不新增**任何 `1144`/`1277`/`1281` 附近的错误。

- [ ] **Step 5: Build 验证**

Run: `cd frontend && npm run build`
Expected: build 成功（vite 产出 `dist/`），无新增 warning/error。

- [ ] **Step 6: 手动验证（测试环境 http://122.51.48.170:45500/）**

打开一个**含运行时指标**的评估任务详情页：
1. 左侧"评测指标"分组下应出现"运行效能"节点（RT 标签 + 青绿色），mini 显示 `通过{pr}%·共{N}项`
2. 点击左树"运行效能"节点 → 节点高亮（active 态左侧 3px 主色边）+ 右侧"运行效能"卡片自动展开 + 滚动定位
3. 右侧卡片标题应为"运行效能"（不再是"运行时指标"）
4. 点击其他质量类别节点 → "运行效能"节点取消高亮，右侧对应质量卡片展开（`selCat='runtime'` 不会误触质量卡片）

打开一个**无运行时指标**的任务详情页：
5. 左树"评测指标"分组下**不出现**"运行效能"节点
6. 右侧无运行效能卡片（与现状一致）

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/EvalDetailPage.tsx
git commit -m "feat: 左树评测指标追加运行效能节点 + 右侧卡片标题改名"
```

- [ ] **Step 8: Push**

```bash
git push origin dpeval
```

---

## Self-Review 结果

**1. Spec coverage:**
- spec 改动 1（左树追加节点）→ Task 1 Step 1 ✓
- spec 改动 2（右侧标题改名）→ Task 1 Step 3 ✓
- spec 改动 2（`open` 联动）→ Task 1 Step 2 ✓
- spec "不改"项（`edp-cnt`、表格列结构、配色）→ Global Constraints / 说明已声明 ✓

**2. Placeholder scan:** 无 TBD/TODO，所有步骤含具体代码或具体命令 ✓

**3. Type consistency:** `selCat === 'runtime'` 在 Step 1（设置）、Step 2（读取）一致；`runtimeMetrics` 类型沿用 `MetricSummary[]`（519 行已定义）✓
