# -*- coding: utf-8 -*-
"""T5-1 + T5-2 测试用例：指标清单 API + 自定义指标 CRUD

通过源码结构验证 API 端点和逻辑，不依赖完整运行环境。

测试覆盖：
1. /metrics 端点存在且返回结构正确
2. /custom-metrics CRUD 端点存在
3. Pydantic 请求模型校验
4. _validate_custom_metric_fields 校验逻辑
5. 软删除引用检查逻辑
6. /custom-metrics/{id}/test 端点存在
7. 回归：现有端点不受影响
"""
import sys
import os
import inspect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_passed = 0
_failed = 0


def assert_true(desc: str, cond: bool):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [PASS] {desc}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc}")


def assert_eq(desc: str, actual, expected):
    global _passed, _failed
    if actual == expected:
        _passed += 1
        print(f"  [PASS] {desc} → {actual}")
    else:
        _failed += 1
        print(f"  [FAIL] {desc} → 期望 {expected!r}，实际 {actual!r}")


print("=" * 60)
print("T5-1 + T5-2 测试：指标清单 API + 自定义指标 CRUD")
print("=" * 60)

# --- 测试1：/metrics 端点 ---
print("\n[测试1] /metrics 端点")
import main as main_mod
src = inspect.getsource(main_mod)

assert_true("有 @app.get('/metrics') 装饰器", '@app.get("/metrics")' in src)
assert_true("list_metrics 函数定义", "async def list_metrics(" in src)
assert_true("返回 builtin 字段", '"builtin"' in src)
assert_true("返回 deepeval 字段", '"deepeval"' in src)
assert_true("返回 custom 字段", '"custom"' in src)
assert_true("返回 categories 字段", '"categories"' in src)
assert_true("跳过 faithfulness/answer_relevancy", 'if spec.key in ("faithfulness", "answer_relevancy")' in src)
assert_true("过滤运行时因子", 'inference_latency' in src and 'tokens_total' in src)

# --- 测试2：/custom-metrics CRUD 端点 ---
print("\n[测试2] /custom-metrics CRUD 端点")
assert_true("有 GET /custom-metrics", '@app.get("/custom-metrics")' in src)
assert_true("有 POST /custom-metrics", '@app.post("/custom-metrics"' in src)
assert_true("有 PUT /custom-metrics/{cm_id}", '@app.put("/custom-metrics/{cm_id}")' in src)
assert_true("有 DELETE /custom-metrics/{cm_id}", '@app.delete("/custom-metrics/{cm_id}")' in src)
assert_true("有 POST /custom-metrics/{cm_id}/test", '@app.post("/custom-metrics/{cm_id}/test")' in src)

# --- 测试3：Pydantic 请求模型 ---
print("\n[测试3] Pydantic 请求模型")
assert_true("CustomMetricCreateRequest 定义", "class CustomMetricCreateRequest" in src)
assert_true("CustomMetricUpdateRequest 定义", "class CustomMetricUpdateRequest" in src)
assert_true("CustomMetricTestRequest 定义", "class CustomMetricTestRequest" in src)

# 验证字段
from main import CustomMetricCreateRequest
cm_create_fields = CustomMetricCreateRequest.model_fields
assert_true("CreateRequest 有 name 字段", "name" in cm_create_fields)
assert_true("CreateRequest 有 kind 字段", "kind" in cm_create_fields)
assert_true("CreateRequest 有 criteria 字段", "criteria" in cm_create_fields)
assert_true("CreateRequest 有 dag 字段", "dag" in cm_create_fields)
assert_true("CreateRequest 有 code 字段", "code" in cm_create_fields)
assert_true("CreateRequest 有 threshold 字段", "threshold" in cm_create_fields)
assert_true("CreateRequest 有 multi_turn 字段", "multi_turn" in cm_create_fields)
assert_true("CreateRequest 有 params 字段", "params" in cm_create_fields)

# --- 测试4：_validate_custom_metric_fields 校验逻辑 ---
print("\n[测试4] 校验逻辑")
assert_true("_validate_custom_metric_fields 函数定义", "def _validate_custom_metric_fields(" in src)
assert_true("geval 校验 criteria 非空", 'kind == "geval"' in src and 'criteria' in src)
assert_true("dag 校验 dag 非空", 'kind == "dag"' in src and 'dag 不能为空' in src)
assert_true("code 校验 code 非空", 'kind == "code"' in src and 'code 不能为空' in src)
assert_true("kind pattern 校验", 'geval|dag|code' in src)

# 实际调用验证
from fastapi import HTTPException
validate = main_mod._validate_custom_metric_fields

# geval 正向
try:
    validate("geval", "回答是否准确", None, None)
    assert_true("geval 正向校验通过", True)
except HTTPException:
    assert_true("geval 正向校验通过", False)

# geval 反向
try:
    validate("geval", "", None, None)
    assert_true("geval 反向抛 422", False)
except HTTPException as e:
    assert_eq("geval 反向状态码", e.status_code, 422)

# dag 正向
try:
    validate("dag", None, {"nodes": []}, None)
    assert_true("dag 正向校验通过", True)
except HTTPException:
    assert_true("dag 正向校验通过", False)

# dag 反向
try:
    validate("dag", None, None, None)
    assert_true("dag 反向抛 422", False)
except HTTPException as e:
    assert_eq("dag 反向状态码", e.status_code, 422)

# code 正向
try:
    validate("code", None, None, "return 1.0")
    assert_true("code 正向校验通过", True)
except HTTPException:
    assert_true("code 正向校验通过", False)

# code 反向
try:
    validate("code", None, None, "")
    assert_true("code 反向抛 422", False)
except HTTPException as e:
    assert_eq("code 反向状态码", e.status_code, 422)

# --- 测试5：软删除引用检查 ---
print("\n[测试5] 软删除引用检查")
assert_true("检查 custom_metric_ids 引用", "custom_metric_ids.isnot(None)" in src)
assert_true("软删除 is_active=False", "cm.is_active = False" in src)
assert_true("引用冲突抛 409", "409" in src)

# --- 测试6：/custom-metrics/{id}/test ---
print("\n[测试6] /custom-metrics/{id}/test 端点")
assert_true("test_custom_metric 函数定义", "async def test_custom_metric(" in src)
assert_true("调用 _run_custom_metric", "_run_custom_metric" in src)
assert_true("返回 score 字段", '"score"' in src)
assert_true("返回 reason 字段", '"reason"' in src)

# --- 测试7：CustomMetric import ---
print("\n[测试7] CustomMetric import")
from database import CustomMetric
assert_true("CustomMetric 可从 database 导入", True)
from main import CustomMetric as CM2
assert_true("CustomMetric 可从 main 导入", True)

# --- 测试8：回归 — 现有端点不变 ---
print("\n[测试8] 回归 — 现有端点不变")
assert_true("/models 端点仍存在", '@app.get("/models")' in src)
assert_true("/evaluate 端点仍存在", '@app.post("/evaluate"' in src)
assert_true("/upload 端点仍存在", '@app.post("/upload"' in src)

# ===== 结果汇总 =====
print("\n" + "=" * 60)
print(f"测试结果：{_passed} 通过，{_failed} 失败")
print("=" * 60)
sys.exit(0 if _failed == 0 else 1)
