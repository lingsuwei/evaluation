# 评估详情页：左树追加"运行效能"指标类别

## 背景与动机

运行时指标（如时延、Token 数、吞吐）原先只在右侧内容区底部以独立卡片"运行时指标"展示，左侧导航树"评测指标"分组下看不到它们。用户希望左树能统一呈现所有指标类别，便于快速跳转。

## 目标

1. 左侧导航树"评测指标"分组下追加"运行效能"节点，与质量类别节点同级同款
2. 右侧内容区"运行时指标"卡片标题改为"运行效能"

## 非目标

- 不改 `edp-cnt` 计数（仍为 `qualityMetrics.length`，运行时指标不属于质量指标语义）
- 不改运行效能表格列结构（指标/平均值/阈值/通过率）
- 不改 `edp-grp-runtime` 配色（`#00897b` 青绿）

## 实现位置

文件：`frontend/src/components/EvalDetailPage.tsx`

### 改动 1：左树追加运行效能节点

在 `groupedQualityMetrics.map(...)` 渲染循环**之后**、`</details>` 关闭"评测指标"分组**之前**（当前 `1144` 行附近），追加：

```tsx
{runtimeMetrics.length > 0 && (() => {
  const scored = runtimeMetrics.filter(m => m.avg_score != null)
  const pr = scored.length
    ? scored.reduce((a, m) => a + (m.pass_rate ?? 0), 0) / scored.length
    : null
  return (
    <div
      className={`edp-cat-row ${view === 'metrics' && selCat === 'runtime' ? 'active' : ''}`}
      onClick={() => { setView('metrics'); setSelQ(null); setSelCat('runtime') }}
    >
      <span className="edp-grp-tag" style={{ background: '#00897b' }}>RT</span>
      <span className="edp-n">运行效能</span>
      <span className="edp-mini">
        通过<b>{pr != null ? pr.toFixed(0) + '%' : '—'}</b>·共{runtimeMetrics.length}项
      </span>
    </div>
  )
})()}
```

要点：
- `selCat` 取新值 `'runtime'` 作为特殊选中值，与质量类别（如 `'safety'`、`'correctness'`）互不冲突
- 通过率算法与质量节点一致：`Σ(pass_rate) / scored.length`
- 配色硬编码 `#00897b`：与现有 `edp-grp-runtime` 类一致，**不新增 token**（已有先例）

### 改动 2：右侧"运行时指标"标题改"运行效能" + 联动展开

`1281` 行：
```diff
- <span className="edp-cat-title">运行时指标</span>
+ <span className="edp-cat-title">运行效能</span>
```

`1277` 行 `<details>` 标签加 `open` 联动：
```diff
- <details className="edp-cat-card">
+ <details className="edp-cat-card" open={selCat === 'runtime' || undefined}>
```

从左树点击"运行效能"进入时，右侧该卡片自动展开（与质量类别卡片 `openByDefault` 行为一致）。

## 不需改动

- `runtimeMetrics` 的 `useMemo`（519 行）数据来源不变
- `isRuntimeMetric`、`runtimeColor`、`formatDuration` 等工具函数不变
- `index.css` 不动（`edp-cat-row`、`edp-grp-tag`、`edp-grp-runtime` 已就绪）

## 风险与验证

- **`selCat='runtime'` 不会误触质量类别卡片**：`groupedQualityMetrics` 不含 `'runtime'` 类别，其卡片 `openByDefault` 判断 `selCat === g.cat` 自然为 false
- **点击"运行效能"后切换到其他维度再回来**：`setSelCat('runtime')` 持久保留，切回 `view='metrics'` 时仍高亮
- **大数据量**：左树仅多一个静态节点，无性能影响
- **验证**：
  - 有运行时指标的任务：左树显示"运行效能"节点 + 右侧卡片标题"运行效能" + 点击左树节点高亮并展开右侧卡片
  - 无运行时指标的任务：左树不显示该节点 + 右侧无卡片（与现状一致）
