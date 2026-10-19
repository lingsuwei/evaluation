---
name: langfuse-dataset-duplicate-name-auto-number
overview: Langfuse导入评测集时，若自动生成的默认名称与已有评测集重复，自动追加编号（如 langfuse_客服FAQ_2）；用户手动指定名称时仍报409错误。
todos:
  - id: add-unique-name-helper
    content: 在 dataset_io_service.py 中新增 ensure_unique_dataset_name 异步函数
    status: completed
  - id: apply-import-langfuse
    content: 在 /datasets/import/langfuse 单条导入端点中调用去重函数
    status: completed
    dependencies:
      - add-unique-name-helper
  - id: apply-merge-langfuse
    content: 在 /datasets/langfuse/merge 合并导入端点中，自动生成名称时调用去重函数
    status: completed
    dependencies:
      - add-unique-name-helper
---

## 用户需求

重复导入 Langfuse 同一个评测集时，eval 的评测集名称自动追加编号区分（如 `langfuse_客服FAQ` → `langfuse_客服FAQ_2` → `langfuse_客服FAQ_3`），当前会出现同名评测集无法区分的问题。

## 补充约定

- 自动生成的默认名称重复 → 自动追加编号
- 用户手动指定名称重复 → 保持现有 409 报错行为不变

## 影响范围

两个 Langfuse 导入端点：

1. `/datasets/import/langfuse`（单条导入）- 无重名检测
2. `/datasets/langfuse/merge`（合并导入）- 用户指定名称有 409 检测，自动生成名称无重名检测

## 技术方案

### 实现策略

在 `dataset_io_service.py` 中新增异步辅助函数 `ensure_unique_dataset_name(session, base_name)`，查询数据库中同名/同名_N 的 Dataset，自动计算下一个可用编号。两个 Langfuse 导入端点在生成默认名称后调用该函数。

### 核心逻辑

```python
async def ensure_unique_dataset_name(session, base_name: str) -> str:
    # 查询所有以 base_name 或 base_name_N 为名称的 Dataset
    # 用 LIKE 匹配: base_name, base_name_2, base_name_3, ...
    # 若无重名，返回 base_name
    # 若有重名，找到最大编号 N，返回 base_name_{N+1}
```

匹配策略：用 `Dataset.name == base_name` OR `Dataset.name.like(f"{base_name}_%")` 查出候选，然后精确过滤出 `base_name` 本身和 `base_name_{数字}` 的记录，取最大编号。

### 修改点

1. **`backend/services/dataset_io_service.py`** - 新增 `ensure_unique_dataset_name` 函数
2. **`backend/main.py`** 第 1795-1796 行 - `/datasets/import/langfuse` 中 `final_name` 生成后调用去重
3. **`backend/main.py`** 第 1940-1941 行 - `/datasets/langfuse/merge` 中仅当 `name` 为 None 时调用去重

### 目录结构

```
backend/
├── services/
│   └── dataset_io_service.py  # [MODIFY] 新增 ensure_unique_dataset_name 异步函数
└── main.py                    # [MODIFY] 两个 Langfuse 导入端点调用去重函数
```

### 实现细节

- `ensure_unique_dataset_name` 使用 `re.fullmatch` 精确匹配 `base_name` 和 `base_name_{数字}`，避免 `langfuse_A_B` 匹配到 `langfuse_A` 的编号
- 编号从 2 开始（第一个重复追加 `_2`）
- 合并端点中保持用户指定名称的 409 逻辑不变，仅在 `name is None`（自动生成名称）时调用去重