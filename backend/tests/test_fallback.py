"""快速验证 _fetch_answer_from_langfuse_with_retry 修复内容"""
import sys, asyncio, inspect
sys.path.insert(0, ".")
from services.inference_service import _fetch_answer_from_langfuse_with_retry

sig = inspect.signature(_fetch_answer_from_langfuse_with_retry)
print(f"参数: {list(sig.parameters.keys())}")
print(f"max_attempts default: {sig.parameters['max_attempts'].default}")

src = inspect.getsource(_fetch_answer_from_langfuse_with_retry)
checks = [
    ("empty_retries_since_fetch", "空 output 计数器"),
    ("empty_retries_since_fetch == 1", "首次空 30s 分支"),
    ("30s 后再试", "首次空日志"),
    ("5min 后再试", "再次空日志"),
    ("FIRST_DELAY_SEC", "5min 延时常量"),
    ("全程 404：5min + 4×30s = 7min", "docstring 404 场景"),
    ("首次空 + 后续仍空：5min + 30s + 3×5min ≈ 20.5min", "docstring 空 output 场景"),
]
all_ok = True
for keyword, label in checks:
    ok = keyword in src
    status = "✅" if ok else "❌"
    print(f"  {status} {label}")
    if not ok:
        all_ok = False

# 渲染器测试
from services.inference_service import _render_langfuse_output_as_text
result = _render_langfuse_output_as_text("hello world")
print(f"\n_render_langfuse_output_as_text('hello world') = '{result}' (len={len(result)})")

sys.exit(0 if all_ok else 1)
