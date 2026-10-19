# 以评估历史详情页为蓝本的 UCD 统一优化方案

> 蓝本：`frontend/src/components/EvalDetailPage.tsx` + `frontend/src/index.css:6347-6370` 设计 token 层
> 范围：`frontend/src/components` 下全部页面型与公共组件
> 原则：不动业务逻辑，仅统一视觉与样式写法

---

## 一、可行性结论

**可行，且收益高、风险可控。**

1. **详情页本身就是一套完整的 token 化设计系统。** `index.css:6347-6370` 已定义 22 个 `--edp-*` CSS 变量（颜色 / 圆角 / 阴影 / 尺寸 / 字体），详情页全部消费这些 token，没有硬编码。蓝本不是"贴在某个页面里的样式"，而是已经抽象好的设计层——可被任何页面复用。
2. **已有 3 个页面成功复用同一套 token**：`ProcessConsistencyView.tsx`(34 处)、`RuntimeStabilityView.tsx`(30 处)、`ScenarioDeliveryView.tsx`(35 处)。证明 token 层跨页面可用、无技术阻碍。
3. **公共组件 `PageHeader.tsx` 已存在**，注释明确写"全站 UCD 统一规范"——意图已就位，只差规则落地与推行。
4. **真正的工作量在"旧页面迁移"**，不在"设计系统搭建"。token 已就绪，无需重新设计。

---

## 二、详情页 UCD 基准

> 提取自 `EvalDetailPage.tsx` 与 `index.css:6347-6370`，作为全站统一对照标准。

### 2.1 字体层级（13px 基准）

| 层级 | 字号 | 字重 | 用途 | 来源 |
|------|------|------|------|------|
| 页面标题 h1 | `clamp(16px, 1.1vw+14px, 18px)` | 600 | 顶部 PageHeader 标题 | `index.css:919` |
| 区域标题 h2 | `17px` | 700 | 卡片区块主标题 | `index.css:6542` |
| 子区域 h3 | `14px` | 700 | 卡片内小节 | `EvalDetailPage.tsx:708` / `index.css:6555` |
| 抽屉标题 h4 | `12px` | 600 | 抽屉 / 弹层区块 | `index.css:6659` |
| 正文 | `13px` | 400 | 默认正文（全局基础） | `index.css:6377` |
| 辅助文字 | `12px` | 400 | 说明、描述 | `index.css:6598` |
| 次要文字 / 标签 | `11px` | 600 | Pill、Tag、徽章文字 | `HistoryPage.tsx:515` / `index.css:6601` |
| 微型文字 | `10px` | 700 | 矩阵表头、极小标记 | `index.css:6674` |
| 数值展示 | `20px` / `26px` | 700 | 大卡片指标数字 | `index.css:6618` / `6660` |

### 2.2 颜色体系（全部为 `var(--edp-*)` token）

| Token | 值 | 用途 |
|-------|----|------|
| `--edp-primary` / `-soft` | `#6b5ce7` / `#eee9ff` | 主色、主按钮、激活态 |
| `--edp-green` / `-soft` | `#22a06b` / `#e3f6ec` | 通过 / 稳定 / 成功 |
| `--edp-amber` / `-soft` | `#e6a817` / `#fdf3d8` | 需关注 / 部分通过 |
| `--edp-red` / `-soft` | `#dc3545` / `#fde2e4` | 失败 / 未通过 / 危险操作 |
| `--edp-blue` / `-soft` | `#1565c0` / `#e3f0fc` | 信息、LLM 标签 |
| `--edp-text` / `text2` / `text3` | `#2b2f3a` / `#5a607a` / `#9099b0` | 主 / 次 / 辅助文字三级 |
| `--edp-border` / `-soft` | `#e6e8f0` / `#eef0f6` | 边框 / 浅分割线 |
| `--edp-card` / `--edp-bg` | `#fff` / `#f7f8fc` | 卡片背景 / 页面背景 |

**状态色映射**（`HistoryPage.tsx:417-424` / `index.css:1957-1962`）：

| 状态 | 背景 | 文字 |
|------|------|------|
| pending | `#fff3cd` | `#856404` |
| processing | `#e3f2fd` | `#1565c0` |
| completed | `#d4edda` | `#155724` |
| failed | `#f8d7da` | `#721c24` |
| paused | `#e8eaf0` | `#555` |
| cancelled | `#f4e9e4` | `#6d4c41` |

### 2.3 图标风格

- **库**：内联 SVG（不依赖 lucide / react-icons），保证全站零外部图标依赖
- **线条**：`strokeWidth="2"`（导航箭头可用 2.5），`fill="none"`，`stroke="currentColor"`
- **尺寸**：标题图标 `20px`、按钮图标 `16-18px`、徽章图标 `14px`、加载图标 `20px`
- **颜色**：通过 `currentColor` 继承父元素，不硬编码

### 2.4 间距 / 圆角 / 阴影

| Token | 值 | 用途 |
|-------|----|------|
| `--edp-radius` | `10px` | 卡片主圆角 |
| 按钮圆角 | `6px`（小）/ `8px`（标准） | |
| 徽章圆角 | `4px`（小标签）/ `12px`（圆药丸） | |
| 卡片内边距 | `16px`（紧凑）/ `24px`（标准） | |
| `--edp-shadow` | `0 1px 3px rgba(40,50,90,.06)` | 卡片默认阴影 |
| `--edp-shadow-2` | `0 4px 14px rgba(40,50,90,.08)` | 悬浮 / 激活 |

### 2.5 卡片 / 按钮 / 徽章规范

- **卡片**：`.edp-cat-card`（`background: var(--edp-card); border: 1px solid var(--edp-border); border-radius: var(--edp-radius); box-shadow: var(--edp-shadow)`）
- **标准按钮**（次级）：`.edp-btn`（`font-size:12px; padding:5px 12px; border-radius:6px`）
- **主操作按钮**：`.edp-btn-export` 风格（主色描边 + soft 背景，hover 反色填充）
- **状态徽章**：`padding:4px 12px; border-radius:12px; font-size:12px`，soft 背景 + 实色文字
- **Tag**：`font-size:10px; padding:1px 6px; border-radius:4px; font-weight:600`

### 2.6 顶部 PageHeader 规范

- 高度：`46px`（`--edp-header-h`）
- 标题：`clamp(16px, 1.1vw+14px, 18px)`，字重 600，颜色 `var(--edp-text)`
- 标题图标：`20×20`，`strokeWidth=2`，`color: var(--edp-primary)`
- 描述：`13px`，`var(--edp-text2)`
- 必须使用公共组件 `<PageHeader>`，不自行实现标题区

---

## 三、现状差异诊断

> 基于对 `frontend/src/components` 全量扫描（53 个组件文件）。

### 3.1 量化扫描结果

| 维度 | 详情页（蓝本） | 其他页面现状 | 差异严重度 |
|------|----------------|--------------|-----------|
| 颜色写法 | 100% `var(--edp-*)` | 50 个文件共 **772 处硬编码 hex** | 高 |
| 样式写法 | className + token | 53 个文件共 **2196 处 `style={{}}`** 内联 | 高 |
| token 采用 | 全量采用 | 仅 4 文件采用（详情页 + 3 视图） | 高 |
| 图标库 | 内联 SVG, strokeWidth=2 | 仅 16 文件有 SVG；尺寸 16/18/20/24 混用 | 中 |
| 字号 | 13px 基准 + 清晰层级 | 多处 inline `fontSize: 12/13` 混用 | 中 |
| 卡片 | `.edp-cat-card` 统一 | `.card` 旧类 + 各自 inline 样式 | 中 |
| PageHeader | 已有公共组件 | 注释提到规则文件但**规则文件不存在** | 低（基础设施已就位） |

### 3.2 硬编码 hex 数量 Top 页面

| 页面 | 硬编码 hex 数 | inline style 数 |
|------|--------------|-----------------|
| `EvalDetailModal.tsx` | 99 | 168 |
| `RuntimePerformanceReport.tsx` | 74 | 132 |
| `ResultsPage.tsx` | 69 | 145 |
| `DatasetManager.tsx` | 62 | 242 |
| `SystemSettingsPage.tsx` | 39 | 113 |
| `ScenarioMatrixView.tsx` | 27 | 73 |
| `AgentManagePage.tsx` | 27 | 151 |
| `EvalPage.tsx` | 24 | 109 |
| `EvalDetailPage.tsx`（蓝本） | 24 | 77 |

> 注：详情页的 24 处 hex 主要在尚未完全迁移的局部区域，主体已 token 化。

### 3.3 典型反面案例

`UploadPage.tsx`——全部 inline style，硬编码 `#666` / `#dc3545` / `#f0f4ff` / `#ddd` / `#f8f9fa`，图标 16/18/20/24 四种尺寸混用，是详情页的对立面。作为用户进入评估流程的入口页，风格断层最明显。

### 3.4 已对齐蓝本的页面（仅需微调）

- `ProcessConsistencyView.tsx`（34 处 token 使用）
- `RuntimeStabilityView.tsx`（30 处）
- `ScenarioDeliveryView.tsx`（35 处）

这 3 个视图已采用 edp token，仅需核对图标尺寸与字号层级是否完全对齐蓝本。

---

## 四、优化方案（分层推进，不动业务逻辑）

### 第 0 层：补齐设计层基础设施（先行，1 次性）

1. **新建 `.cursor/rules/onto-eval-ucd-page-header.mdc`**——把 `PageHeader.tsx` 注释里承诺的规则文件补齐，明确：
   - 所有页面顶部必须用 `<PageHeader>` 组件，不自行实现标题区
   - 标题图标用内联 SVG `20×20 strokeWidth=2`
   - 描述文字 `var(--edp-text2)`，13px
2. **在 `index.css` 顶层扩充 token**（补齐蓝本里尚未抽象的）：
   - `--edp-radius-sm: 6px` / `--edp-radius-pill: 12px`
   - `--edp-space-1..6: 4/8/12/16/24/32px`
   - `--edp-font-xs/sm/base/md/lg: 10/11/12/13/14/17px`
   - 让后续迁移有名字可叫，不再写魔法数字
3. **新增 3 个全局语义类**（替代散落的 inline style）：
   - `.ucd-card` = 等价于 `.edp-cat-card`，作为全站卡片默认类
   - `.ucd-muted` = `color: var(--edp-text2); font-size: 13px`（替代 `style={{color:'#666'}}`）
   - `.ucd-hint` = `color: var(--edp-text3); font-size: 12px`（替代辅助说明）

### 第 1 层：公共组件先行（高杠杆）

| 组件 | 当前问题 | 改造动作 |
|------|---------|---------|
| `PageHeader.tsx` | 规则文件缺失、icon 由调用方传入无规范 | 补规则文件；接收 `icon` 时强制 20×20/strokeWidth=2 |
| `Tooltip.tsx` | inline style | 改用 `var(--edp-text3)` + `.ucd-hint` |
| `HelpTip.tsx` | inline style | 同上 |

公共组件改完后，下游页面自动继承风格，**杠杆最高**。

### 第 2 层：页面型组件迁移（按优先级排序）

#### P0 — 高曝光页面优先（用户高频进入）

1. `EvalPage.tsx`——评估主入口，24 处 hex，109 处 inline style
2. `HistoryPage.tsx`——历史列表，与详情页直接相邻，风格断层最明显
3. `UploadPage.tsx`——上传入口，反面案例，改造后效果对比最强
4. `ResultsPage.tsx`——69 处 hex，结果页核心

#### P1 — 管理类页面

5. `DatasetManagePage.tsx` + `DatasetManager.tsx`（合计 66 处 hex）
6. `AgentManagePage.tsx`（27 处 hex）
7. `OntologyPackagePage.tsx`（8 处）
8. `SystemSettingsPage.tsx`（39 处）

#### P2 — 报告 / 监控类

9. `ReportArchivePage.tsx`（16 处）
10. `TraceMonitorPage.tsx`（11 处 hex）
11. `RuntimePerformanceReport.tsx`（74 处）
12. `EvalDetailModal.tsx`（99 处 hex——详情弹窗，应与详情页同风格）

#### P3 — 已基本对齐的视图（仅微调）

- `ProcessConsistencyView` / `RuntimeStabilityView` / `ScenarioDeliveryView`
- 核对图标尺寸与字号层级是否完全对齐蓝本

### 第 3 层：每个页面的统一改造动作（标准化清单）

每个页面迁移时执行同一套动作：

- [ ] 所有 `style={{ color: '#xxx' }}` → 替换为 `var(--edp-text)` / `text2` / `text3` 或语义类
- [ ] 所有 `style={{ background: '#xxx' }}` → `var(--edp-card)` / `--edp-bg` / 状态 soft 色
- [ ] 所有 `style={{ border: '1px solid #ddd' }}` → `var(--edp-border)`
- [ ] 所有 `style={{ fontSize: N }}` → 蓝本字号层级中最近的一档
- [ ] 所有 `<div className="card">` → `<div className="ucd-card">`（或保留 `.card` 但统一其样式到 edp 标准）
- [ ] 所有内联 SVG 核对 `strokeWidth="2"` + 尺寸归一到 14/16/18/20 之一
- [ ] 顶部标题区改用 `<PageHeader>` 组件
- [ ] 状态徽章统一用 `.status-*` 蓝本类，不自行实现
- [ ] 按钮统一用 `.edp-btn` / `.edp-btn-export`，不混用 `.btn`/`.btn-primary` 旧类（或把旧类样式对齐到 edp 标准）

### 第 4 层：旧全局类逐步退役

`.card` / `.btn` / `.btn-primary` 这些旧类目前与 edp 类并存。建议：

- **阶段一**：把 `.card` / `.btn` 的样式定义直接对齐到 edp 标准（即修改 `index.css` 里这两个类的实现，让所有用旧类的页面自动升级）
- **阶段二**：迁移完成后逐步把旧类重命名为 edp 类，最终只保留一套

这一步**改动小、影响面大**——改 2 个 CSS 类定义就能让 50 个页面的卡片和按钮风格瞬间统一，是性价比最高的一招。

---

## 五、风险与应对

| 风险 | 应对 |
|------|------|
| 迁移面广，一次性 PR 过大 | 按 P0→P3 分批，每批一个 PR；先做第 0 层和第 4 层（改 CSS 类定义）作为"一次性拉升全局基线" |
| inline style 数量多（2196 处） | 不求一次全清，优先清颜色 / 字号类 inline；间距类 inline 可保留过渡 |
| 视觉回归不可见 | 每个高曝光页面改造前后截图对比；`frontend/e2e` 目录已有截图基线可复用 |
| `EvalDetailModal` 与 `EvalDetailPage` 风格应一致但现状差 99 处 hex | 列为 P2 但与详情页同批评审，确保弹层与页面视觉连贯 |

---

## 六、推荐启动顺序

1. **第 0 层 + 第 4 层**（建规则文件 + 改 `.card`/`.btn` CSS 定义）—— 一次性拉升全局基线，1 个 PR
2. **第 1 层**（PageHeader / Tooltip / HelpTip）—— 公共组件对齐，1 个 PR
3. **P0 页面**（EvalPage / HistoryPage / UploadPage / ResultsPage）—— 每页 1 个 PR
4. P1 / P2 / P3 按节奏推进

---

## 七、验收标准

每个页面迁移完成后，应满足：

- [ ] 硬编码 hex 数量为 0（或仅保留无法 token 化的特殊场景，需注释说明）
- [ ] inline `style={{}}` 数量较改造前下降 ≥ 70%（颜色 / 字号类必须清零）
- [ ] 所有内联 SVG `strokeWidth="2"`，尺寸 ∈ {14, 16, 18, 20}
- [ ] 顶部标题区使用 `<PageHeader>` 组件
- [ ] 卡片使用 `.ucd-card` 或 `.edp-cat-card`
- [ ] 按钮使用 `.edp-btn` / `.edp-btn-export`（或对齐后的 `.btn` 旧类）
- [ ] 状态徽章使用 `.status-*` 蓝本类
- [ ] 改造前后截图对比无视觉回归（业务功能不变）

---

## 附：蓝本关键文件索引

| 文件 | 行号 | 内容 |
|------|------|------|
| `frontend/src/components/EvalDetailPage.tsx` | — | 详情页主体（蓝本） |
| `frontend/src/components/HistoryPage.tsx` | 417-424, 515 | 状态色映射、徽章规范 |
| `frontend/src/components/PageHeader.tsx` | — | 公共标题组件 |
| `frontend/src/index.css` | 6347-6370 | `--edp-*` 设计 token 定义 |
| `frontend/src/index.css` | 893-938 | PageHeader 样式 |
| `frontend/src/index.css` | 6410-6423 | 详情页顶部栏样式 |
| `frontend/src/index.css` | 6430-6434 | `.edp-btn` 按钮样式 |
| `frontend/src/index.css` | 6580-6581 | `.edp-cat-card` 卡片样式 |
| `frontend/src/index.css` | 1950-1981 | 状态徽章 / 类型徽章样式 |
